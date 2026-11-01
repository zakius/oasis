from pathlib import Path

import pytest

from conftest import call, make_client
from oasis import ytm as ytm_module
from oasis.db import connect
from oasis.ytm import Stream, track_row

SEARCH_ITEM = {
    "videoId": "vid1", "title": "Remote Song", "artists": [{"name": "Gamma"}, {"name": "Delta"}],
    "album": {"name": "Far Away"}, "duration_seconds": 200,
    "thumbnails": [{"url": "https://img/x=w60-h60-l90-rj"}, {"url": "https://img/x=w120-h120-l90-rj"}],
}
# Same song as the local "Beta - One" (2 s long), as YouTube Music would list it.
DUPLICATE = {"videoId": "vid2", "title": "one", "artists": [{"name": "Beta"}], "album": None, "duration_seconds": 3}


class FakeYtm:
    account = None

    def __init__(self, cache: Path):
        self.cache, self.fetched, self.direct = cache, [], False

    def search(self, query, limit):
        return [track_row(SEARCH_ITEM), track_row(DUPLICATE)][:limit]

    def match(self, track):
        return None

    def resolve(self, video_id):
        if not self.direct:
            raise LookupError("no direct m4a audio")
        return Stream("https://googlevideo/" + video_id, "audio/mp4", 10, 0)

    def forget(self, video_id):
        pass

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


def test_a_search_isnt_locked_out_by_writes_landing_while_youtube_music_answers(client, ytm, tmp_path, monkeypatch):
    def search(query, limit):  # the mix refresh writes meanwhile
        other = connect(tmp_path / "oasis.db")
        other.execute("insert into playlist(name, owner, created, changed) values ('mix', 'admin', 0, 0)")
        other.close()
        return [track_row(SEARCH_ITEM)]

    monkeypatch.setattr(ytm, "search", search)
    found = call(client, "search3", query="e")["searchResult3"]
    assert "yt-vid1" in {s["id"] for s in found["song"]} and found["album"]


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


def test_stream_without_direct_url_downloads_once_then_serves_cache(client, ytm):
    call(client, "search3", query="remote")
    for _ in range(2):
        response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": "yt-vid1"})
        assert response.status_code == 200 and response.content == b"audio:vid1"
    assert ytm.fetched == ["vid1"]


AUDIO = b"0123456789"


@pytest.mark.parametrize("range_header, status, body, content_range", [
    (None, 200, AUDIO, None),
    ("bytes=2-5", 206, b"2345", "bytes 2-5/10"),
    ("bytes=7-", 206, b"789", "bytes 7-9/10"),
    ("bytes=-3", 206, b"789", "bytes 7-9/10"),
    ("bytes=4-99", 206, b"456789", "bytes 4-9/10"),
])
def test_uncached_stream_proxies_ranges_while_cache_fills(client, ytm, monkeypatch, range_header, status, body, content_range):
    ytm.direct = True
    monkeypatch.setattr(ytm_module, "read_range", lambda url, start, end: iter([AUDIO[start:end + 1]]))
    call(client, "search3", query="remote")
    headers = {"Range": range_header} if range_header else {}
    response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": "yt-vid1"}, headers=headers)
    assert response.status_code == status and response.content == body
    assert response.headers.get("content-range") == content_range
    assert response.headers["content-length"] == str(len(body))


def test_unsatisfiable_range(client, ytm, monkeypatch):
    ytm.direct = True
    call(client, "search3", query="remote")
    response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": "yt-vid1"}, headers={"Range": "bytes=50-"})
    assert response.status_code == 416 and response.headers["content-range"] == "bytes */10"


def test_songs_carry_their_youtube_id(client, ytm, monkeypatch):
    call(client, "search3", query="remote")
    assert call(client, "getSong", id="yt-vid1")["song"]["youtubeId"] == "vid1"
    local = next(s for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == "Song A")
    assert "youtubeId" not in local
    asked = []
    monkeypatch.setattr(ytm, "match", lambda track: asked.append(track["title"]) or "vidA", raising=False)
    assert call(client, "getSong", id=local["id"])["song"]["youtubeId"] == "vidA"
    monkeypatch.setattr(ytm, "match", lambda track: asked.append(track["title"]) or None, raising=False)
    other = next(s for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == "Song B")
    assert "youtubeId" not in call(client, "getSong", id=other["id"])["song"]
    call(client, "getSong", id=other["id"])
    assert asked == ["Song A", "Song B"]  # a missing match is looked up once


def test_home_items_ytmusicapi_cant_parse_are_skipped(tmp_path):
    class Client:
        def get_home(self, limit):
            return [{"contents": [None, {"playlistId": "RDTMAK1", "title": "My Supermix"}]}, None, {"contents": None}]

        def get_library_playlists(self, limit):
            return [None, {"playlistId": "LM", "title": "Liked Music"}]
    found = ytm_module.YouTubeMusic(tmp_path).playlists(Client())
    assert [p["title"] for p in found] == ["My Supermix", "Liked Music"]


def test_mix_tracks_come_from_the_playlist_not_its_watch_queue(tmp_path):
    # YouTube's watch queue of a themed My Mix holds another mix's songs; the playlist itself holds the right ones.
    def track(video_id):
        return {**SEARCH_ITEM, "videoId": video_id}

    class Client:
        fails = False

        def get_playlist(self, playlist_id, limit=None):
            if self.fails:
                raise KeyError("contents")
            return {"tracks": [track("right")]}

        def get_watch_playlist(self, playlistId, limit):
            return {"tracks": [track("wrong")]}

    music, client = ytm_module.YouTubeMusic(tmp_path), Client()
    assert [r["ref"] for r in music.playlist_tracks(client, "RDTMAK5uy_mix")] == ["right"]
    client.fails = True
    assert [r["ref"] for r in music.playlist_tracks(client, "RDTMAK5uy_mix")] == ["wrong"]  # still better than nothing
    with pytest.raises(KeyError):
        music.playlist_tracks(client, "PLsaved")
