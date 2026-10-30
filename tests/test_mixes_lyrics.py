import subprocess

import pytest
from mutagen.id3 import ID3, USLT

from conftest import call, make_client, make_track
from oasis.api import transcode_target
from oasis.lyrics import Lyrics
from oasis.ytm import track_row


def item(video_id, title, artist="Mixer", album="Mix Album"):
    return {"videoId": video_id, "title": title, "artists": [{"name": artist}], "album": {"name": album, "id": "MPREb_mix"},
            "duration_seconds": 180, "thumbnails": [{"url": "https://img/m=w60-h60"}]}


class FakeYtm:
    def __init__(self, cache):
        self.cache, self.fetched = cache, []
        self.current = {"RDTMAK_super": ["m1", "m2"], "RDTMAK_mix1": ["m2", "m3"]}

    def client(self, auth):
        return "client:" + auth

    def account_name(self, client):
        return "Test Account"

    def mixes(self, client):
        return [{"id": id, "title": {"RDTMAK_super": "My Supermix", "RDTMAK_mix1": "My Mix 1"}[id], "description": "For you",
                 "cover": "https://img/cover"} for id in self.current]

    def mix_tracks(self, client, playlist_id):
        return [track_row(item(v, f"Song {v}")) for v in self.current[playlist_id]]

    def cached(self, video_id):
        return None

    def fetch(self, video_id):
        self.fetched.append(video_id)

    def match(self, track):
        return "vid-" + track["title"]

    def lyrics(self, video_id):
        return Lyrics("YouTube Music", [(0, "first"), (1500, "second")], True) if video_id == "vid-Two" else None

    def search(self, query, limit):
        return []


@pytest.fixture
def ytm(tmp_path):
    return FakeYtm(tmp_path)


@pytest.fixture
def client(library, tmp_path, ytm):
    with make_client(library, tmp_path / "oasis.db", ytm) as c:
        yield c


def playlists(client):
    return {p["name"]: p for p in call(client, "getPlaylists")["playlists"]["playlist"]}


def library_titles(client):
    return {s["title"] for s in call(client, "search3", query="")["searchResult3"]["song"]}


def link(client):
    assert call(client, "oasisLinkYouTubeMusic", cookie="SID=1; __Secure-3PAPISID=abc")["youtubeMusic"] == {"account": "Test Account"}


def test_mixes_are_read_only_playlists_in_the_library(client, ytm):
    link(client)
    client.app.state.api._refresh_mixes("admin")
    mixes = playlists(client)
    assert set(mixes) == {"My Supermix", "My Mix 1"} and {m["owner"] for m in mixes.values()} == {"YouTube Music"}
    entries = call(client, "getPlaylist", id=mixes["My Supermix"]["id"])["playlist"]["entry"]
    assert [e["title"] for e in entries] == ["Song m1", "Song m2"]
    assert {"Song m1", "Song m2", "Song m3"} <= library_titles(client)
    assert "Mix Album" in [a["name"] for a in call(client, "getAlbumList2", type="alphabeticalByName", size=50)["albumList2"]["album"]]
    assert ytm.fetched == []  # mixes don't download
    cover = mixes["My Supermix"]["coverArt"]
    assert cover.startswith("pl-")
    assert call(client, "updatePlaylist", playlistId=mixes["My Mix 1"]["id"], name="mine")["error"]["code"] == 70
    call(client, "oasisUnlinkYouTubeMusic")
    assert playlists(client) == {}
    assert call(client, "deletePlaylist", id=mixes["My Mix 1"]["id"])["error"]["code"] == 70


def test_mix_refresh_follows_youtube(client, ytm):
    api = client.app.state.api
    link(client)
    api._refresh_mixes("admin")
    ytm.current = {"RDTMAK_super": ["m1", "m4"]}
    api._refresh_mixes("admin")
    assert set(playlists(client)) == {"My Supermix"}
    titles = library_titles(client)
    assert "Song m4" in titles and "Song m2" not in titles and "Song m3" not in titles


def test_starred_mix_track_downloads(client, ytm):
    link(client)
    client.app.state.api._refresh_mixes("admin")
    call(client, "star", id="yt-m1")
    assert ytm.fetched == ["m1"]


def lyric_lines(client, id):
    return call(client, "getLyricsBySongId", id=id)["lyricsList"]


def song_id(client, title):
    return next(s["id"] for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == title)


def test_lyrics_from_youtube_music_are_synced(client):
    lyrics = lyric_lines(client, song_id(client, "Two"))["structuredLyrics"][0]
    assert lyrics["synced"] is True and lyrics["line"] == [{"start": 0, "value": "first"}, {"start": 1500, "value": "second"}]
    assert lyric_lines(client, song_id(client, "Song A")) == {}
    assert "songLyrics" in [e["name"] for e in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]]


def test_local_lyrics_sidecar_and_embedded(tmp_path, ytm):
    root = tmp_path / "music"
    make_track(root / "A/B/lrc.mp3", title="Lrc", artist="A", album="B")
    (root / "A/B/lrc.lrc").write_text("[ar:A]\n[00:01.50]hello\n[00:00.20][00:03.00]again\n")
    make_track(root / "A/B/tag.mp3", title="Tag", artist="A", album="B")
    tags = ID3(root / "A/B/tag.mp3")
    tags.add(USLT(encoding=3, lang="eng", desc="", text="line one\nline two\n"))
    tags.save()
    with make_client(root, tmp_path / "oasis.db", ytm) as client:
        lrc = lyric_lines(client, song_id(client, "Lrc"))["structuredLyrics"][0]
        assert lrc["synced"] and [(l["start"], l["value"]) for l in lrc["line"]] == [(200, "again"), (1500, "hello"), (3000, "again")]
        tag = lyric_lines(client, song_id(client, "Tag"))["structuredLyrics"][0]
        assert not tag["synced"] and tag["line"] == [{"value": "line one"}, {"value": "line two"}]
        plain = call(client, "getLyrics", artist="A", title="Tag")["lyrics"]
        assert plain["value"] == "line one\nline two"


def test_transcode_choice():
    assert transcode_target("opus", 0, "flac", 900) == ("opus", 192)
    assert transcode_target("mp3", 0, "mp3", 320) is None
    assert transcode_target(None, 96, "flac", 900) == ("opus", 96)
    assert transcode_target(None, 0, "flac", 900) is None
    assert transcode_target("raw", 64, "flac", 900) is None


def test_transcoded_stream_decodes(client, tmp_path):
    response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": song_id(client, "One"), "format": "opus"})
    assert response.status_code == 200 and response.headers["content-type"] == "audio/webm"
    out = tmp_path / "out.webm"
    out.write_bytes(response.content)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "opus"
