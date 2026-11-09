import subprocess
import threading
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3, USLT

from conftest import call, make_client, make_track
from oasis import playlists as oasis_playlists
from oasis.transcode import target as transcode_target
from oasis.ytm import YouTubeMusic, song


def item(video_id, title, artist="Mixer", album="Mix Album"):
    return {"videoId": video_id, "title": title, "artists": [{"name": artist}], "album": {"name": album, "id": "MPREb_mix"},
            "duration_seconds": 180, "thumbnails": [{"url": "https://img/m=w60-h60"}]}


class FakeYtm:
    def __init__(self, cache):
        self.cache, self.fetched = cache, []
        self.current = {"RDTMAK_super": ["m1", "m2"], "RDTMAK_mix1": ["m2", "m3"], "PLmine": ["m5"], "LM": ["m6"]}

    def client(self, auth):
        return "client:" + auth

    def account_name(self, client):
        return "Test Account"

    def playlists(self, client):
        return [{"id": id, "title": {"RDTMAK_super": "My Supermix", "RDTMAK_mix1": "My Mix 1", "PLmine": "Road trip", "PLsaved": "Saved",
                                     "PLlater": "Later", "LM": "Liked Music"}[id], "description": "For you",
                 "cover": "https://img/cover"} for id in self.current]

    def playlist_tracks(self, client, playlist_id):
        return [song(item(v, f"Song {v}")) for v in self.current[playlist_id]]

    def cached(self, video_id):
        return None

    def fetch(self, video_id):
        self.fetched.append(video_id)

    def match(self, track):
        return "vid-" + track["title"]

    def lyrics(self, video_id):
        return {"source": "YouTube Music", "lines": [(0, "first"), (1500, "second")], "synced": True} if video_id == "vid-Two" else None

    def search(self, query, limit):
        return []

    def album(self, browse_id):  # a soundtrack: each song names its singer, the album page Various Artists
        return [song(item(v, f"Song {v}")) | {"track_no": n, "year": 2024, "album_by": "Various Artists"} for n, v in enumerate(["m1", "m6"], 1)]


@pytest.fixture
def ytm(tmp_path):
    return FakeYtm(tmp_path)


@pytest.fixture
def client(library, tmp_path, ytm):
    with make_client(library, tmp_path / "oasis.db", ytm) as c:
        yield c


def playlists(client, **params):
    return {p["name"]: p for p in call(client, "getPlaylists", oasis="1", **params)["playlists"]["playlist"]}


def library_titles(client):
    return {s["title"] for s in call(client, "search3", query="")["searchResult3"]["song"]}


def link(client):
    assert call(client, "oasisLinkYouTubeMusic", cookie="SID=1; __Secure-3PAPISID=abc")["youtubeMusic"] == {"account": "Test Account"}


def test_mixes_are_read_only_playlists_in_the_library(client, ytm):
    link(client)
    client.app.state.oasis.mixes.refresh("admin")
    mixes = playlists(client)
    assert set(mixes) == {"My Supermix", "My Mix 1", "Liked Music"} and {m["owner"] for m in mixes.values()} == {"YouTube Music"}  # Road trip waits for a tick
    assert mixes["My Supermix"]["oasisMix"] is True and "oasisMix" not in mixes["Liked Music"]
    assert mixes["My Supermix"]["youtubeId"] == "RDTMAK_super" and mixes["Liked Music"]["youtubeId"] == "LM"
    assert not {"oasisMix", "youtubeId"} & {k for p in call(client, "getPlaylists")["playlists"]["playlist"] for k in p}  # plain clients
    entries = call(client, "getPlaylist", id=mixes["My Supermix"]["id"])["playlist"]["entry"]
    assert [e["title"] for e in entries] == ["Song m1", "Song m2"]
    titles = library_titles(client)
    assert "Song m6" in titles and not {"Song m1", "Song m2", "Song m3", "Song m5"} & titles  # mixes stay playlists; Liked Music counts
    assert "Mix Album" in [a["name"] for a in call(client, "getAlbumList2", type="alphabeticalByName", size=50)["albumList2"]["album"]]
    assert ytm.fetched == []  # mixes don't download
    cover = mixes["My Supermix"]["coverArt"]
    assert cover.startswith("pl-")
    assert mixes["My Mix 1"]["readonly"] is True
    uncovered = call(client, "createPlaylist", name="Bare")["playlist"]["id"]
    assert client.get("/rest/getCoverArt", params={"u": "admin", "p": "secret", "id": f"pl-{uncovered}"}).status_code == 404
    call(client, "deletePlaylist", id=uncovered)
    assert call(client, "updatePlaylist", playlistId=mixes["My Mix 1"]["id"], name="mine")["error"]["code"] == 50  # not authorized
    call(client, "oasisUnlinkYouTubeMusic")
    assert playlists(client) == {}
    assert call(client, "deletePlaylist", id=mixes["My Mix 1"]["id"])["error"]["code"] == 70


def test_mix_refresh_follows_youtube(client, ytm):
    api = client.app.state.oasis
    link(client)
    api.mixes.refresh("admin")
    ytm.current = {"RDTMAK_super": ["m1", "m4"]}
    api.mixes.refresh("admin")
    assert set(playlists(client)) == {"My Supermix"}
    titles = library_titles(client)
    assert not {"Song m2", "Song m3", "Song m4", "Song m6"} & titles  # m6 left with Liked Music
    assert [e["title"] for e in call(client, "getPlaylist", id=playlists(client)["My Supermix"]["id"])["playlist"]["entry"]] == ["Song m1", "Song m4"]


def test_starred_mix_track_downloads(client, ytm):
    link(client)
    client.app.state.oasis.mixes.refresh("admin")
    call(client, "star", id="yt-m1")
    assert ytm.fetched == ["m1"] and "Song m1" in library_titles(client)


def test_songs_in_the_library_only_through_a_mix_leave_it(client):
    link(client)
    oasis = client.app.state.oasis
    oasis.mixes.refresh("admin")
    db = oasis.connect()
    db.execute("update track set library = 1 where source = 'ytm'")  # as the old rule left them
    oasis_playlists.demix(db)
    assert {r[0] for r in db.execute("select ref from track where source = 'ytm' and library = 1")} == {"m6"}  # Road trip is hidden, unread


def test_a_streaming_songs_album_artist_is_its_artist_till_its_album_page_names_another(client, ytm):
    link(client)
    oasis = client.app.state.oasis
    oasis.mixes.refresh("admin")
    song = next(s for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == "Song m6")
    assert song["displayAlbumArtist"] == "Mixer" and song["albumArtists"] == [{"id": song["artistId"], "name": "Mixer"}]
    assert "track" not in song and "year" not in song
    assert oasis.complete_next_album() is not None  # as the album fill worker reads it
    song = next(s for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == "Song m6")
    assert song["displayAlbumArtist"] == "Various Artists" and (song["track"], song["year"]) == (2, 2024)  # never the album's title
    album = call(client, "getAlbum", id=song["albumId"])["album"]
    assert album["artist"] == "Various Artists" and album["year"] == 2024
    assert "Various Artists" not in {a["name"] for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"]}
    assert oasis.complete_next_album() is None  # each album page is read once
    oasis.mixes.refresh("admin")  # seen again in a playlist, which carries no year: what the album page gave stays
    song = next(s for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == "Song m6")
    assert (song["displayAlbumArtist"], song["year"]) == ("Various Artists", 2024)


def save(client, *shown, new="hide"):
    ids = {p["name"]: p["id"] for p in call(client, "getPlaylists", oasis="1")["playlists"]["playlist"]}
    db = client.app.state.oasis.connect()
    ids |= {r["name"]: str(r["id"]) for r in db.execute("select id, name from playlist where mix is not null")}
    client.post("/login", data={"name": "admin", "password": "secret"})
    return client.post("/account/youtube/playlists", data={"new": new, **{f"show-{ids[n]}": "on" for n in shown}}).text


def test_library_playlists_show_as_ticked_and_their_songs_follow(client, ytm):
    link(client)
    oasis = client.app.state.oasis
    oasis.mixes.refresh("admin")
    client.post("/login", data={"name": "admin", "password": "secret"})
    page = client.get("/").text
    assert "Road trip" in page and "Liked Music" in page and "My Supermix" not in page.split("Playlists shown in your apps")[1].split("</form>")[0]
    assert "Playlists saved. Newly shown ones are loading." in save(client, "Liked Music", "Road trip")
    oasis.mixes.refresh("admin")  # as the thread the save starts does
    assert "Road trip" in playlists(client) and "Song m5" in library_titles(client)
    save(client, "Liked Music")  # unticked: gone from apps, its songs from the library at once
    assert "Road trip" not in playlists(client) and "Song m5" not in library_titles(client)
    hidden = oasis.connect().execute("select id from playlist where name = 'Road trip'").fetchone()[0]
    assert call(client, "getPlaylist", id=hidden)["error"]["code"] == 70
    save(client)  # Liked Music hidden too: its songs stay in the library
    assert "Liked Music" not in playlists(client) and "Song m6" in library_titles(client)


def test_playlists_added_later_follow_the_choice(client, ytm):
    link(client)
    oasis = client.app.state.oasis
    oasis.mixes.refresh("admin")
    ytm.current["PLsaved"] = ["m7"]
    oasis.mixes.refresh("admin")
    assert "Saved" not in playlists(client) and "Song m7" not in library_titles(client)  # opt in: hidden till ticked
    save(client, "Liked Music", new="show")
    ytm.current["PLlater"] = ["m8"]
    oasis.mixes.refresh("admin")
    assert "Later" in playlists(client) and "Song m8" in library_titles(client) and "Saved" not in playlists(client)  # opt out: shown till unticked


def test_an_album_pages_own_artist_is_its_songs_album_artist():
    page = {"title": "Hazbin Hotel (Original Soundtrack, Pt. 1)", "year": "2024", "artists": [{"name": "Various Artists"}], "thumbnails": [],
            "tracks": [{"videoId": "v1", "title": "Happy Day in Hell", "artists": [{"name": "Erika Henningsen"}], "trackNumber": 1}]}
    ytm = object.__new__(YouTubeMusic)
    ytm.idle, ytm.calls = [SimpleNamespace(get_album=lambda browse_id: page)], threading.Semaphore()
    (found,) = ytm.album("MPREb_hh")
    assert (found["album_by"], found["album_artist"], found["track_no"], found["year"]) == ("Various Artists", "Erika Henningsen", 1, 2024)


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


def test_playlists_list_mixes_first_in_youtube_order(client, ytm):
    names = {"RDTMAK5uy_lfhhWWw9v71CPrR7MRMHgZzbH6Vku9iJc": "Mroczna moc", "RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50": "Bunt",
             "RDTMAK5uy_zzOther": "Chill Mix", "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY": "My Supermix",
             "RDTMAK5uy_nilrsVWxrKskY0ZUpVZ3zpB0u4LwWTVJ4": "Replay Mix", "LM": "Liked Music", "PLa": "aaa"}
    ytm.current = {id: ["m1"] for id in names}
    ytm.playlists = lambda client: [{"id": id, "title": name, "description": None, "cover": None} for id, name in names.items()]
    link(client)
    client.app.state.oasis.connect().execute("update user set ytm_new_shown = 1")  # library playlists shown as they come
    client.app.state.oasis.mixes.refresh("admin")
    call(client, "createPlaylist", name="Zebra")
    assert list(playlists(client)) == ["My Supermix", "Bunt", "Mroczna moc", "Replay Mix", "Chill Mix", "Liked Music", "aaa", "Zebra"]
