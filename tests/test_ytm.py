from pathlib import Path

import pytest

from conftest import call, make_client
from oasis.ytm import track_row

SEARCH_ITEM = {
    "videoId": "vid1", "title": "Remote Song", "artists": [{"name": "Gamma"}, {"name": "Delta"}],
    "album": {"name": "Far Away"}, "duration_seconds": 200,
    "thumbnails": [{"url": "https://img/x=w60-h60-l90-rj"}, {"url": "https://img/x=w120-h120-l90-rj"}],
}
# Same song as the local "Beta - One" (2 s long), as YouTube Music would list it.
DUPLICATE = {"videoId": "vid2", "title": "one", "artists": [{"name": "Beta"}], "album": None, "duration_seconds": 3}


class FakeYtm:
    def __init__(self, cache: Path):
        self.cache, self.fetched = cache, []

    def search(self, query, limit):
        return [track_row(SEARCH_ITEM), track_row(DUPLICATE)][:limit]

    def cached(self, video_id):
        path = self.cache / f"{video_id}.m4a"
        return path if path.exists() else None

    def fetch(self, video_id):
        self.fetched.append(video_id)
        path = self.cache / f"{video_id}.m4a"
        path.write_bytes(b"audio:" + video_id.encode())
        return path


@pytest.fixture
def ytm(tmp_path):
    return FakeYtm(tmp_path)


@pytest.fixture
def client(library, tmp_path, ytm):
    with make_client(library, tmp_path / "oasis.db", ytm) as c:
        yield c


def library_titles(client):
    return {s["title"] for s in call(client, "search3", query="")["searchResult3"]["song"]}


def test_track_row_from_search_and_watch_items():
    row = track_row(SEARCH_ITEM)
    assert row["id"] == "yt-vid1" and row["artist"] == "Gamma, Delta" and row["album_artist"] == "Gamma"
    assert row["cover"] == "https://img/x=w544-h544-l90-rj" and row["duration"] == 200 and row["library"] == 0
    watch = track_row({"videoId": "w", "title": "T", "artists": [{"name": "A"}], "length": "1:02:03", "year": "2020",
                       "thumbnail": [{"url": "https://t"}]})
    assert watch["duration"] == 3723 and watch["year"] == 2020 and watch["album"] == "T"


def test_search_adds_streaming_results_after_local_and_merges_duplicates(client):
    songs = call(client, "search3", query="one")["searchResult3"]["song"]
    assert [s["title"] for s in songs] == ["One", "Remote Song"]
    assert songs[1]["id"] == "yt-vid1" and "path" not in songs[1]


def test_streaming_tracks_join_library_only_while_starred(client, ytm):
    call(client, "search3", query="remote")
    assert "Remote Song" not in library_titles(client)
    call(client, "star", id="yt-vid1")
    assert "Remote Song" in library_titles(client)
    assert "Gamma" in {a["name"] for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"]}
    call(client, "unstar", id="yt-vid1")
    assert "Remote Song" not in library_titles(client)


def test_playlist_membership_keeps_streaming_track_in_library(client):
    call(client, "search3", query="remote")
    playlist = call(client, "createPlaylist", name="P", songId="yt-vid1")["playlist"]
    assert "Remote Song" in library_titles(client)
    call(client, "deletePlaylist", id=playlist["id"])
    assert "Remote Song" not in library_titles(client)


def test_stream_downloads_once_then_serves_cache(client, ytm):
    call(client, "search3", query="remote")
    for _ in range(2):
        response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": "yt-vid1"})
        assert response.status_code == 200 and response.content == b"audio:vid1"
    assert ytm.fetched == ["vid1"]
