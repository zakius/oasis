import io
import json
import threading
import time
import urllib.parse

import pytest

from conftest import call, make_client
from test_accounts import sign_in
from oasis import acquire, musicbrainz
from oasis.acquire import Acquire, artist_names
from oasis.db import connect
from oasis.download import Downloads


def group(id, title, kind="Album", date="2001", secondary=(), artist=("mb-beta", "Beta")):
    return {"id": id, "title": title, "primary-type": kind, "secondary-types": list(secondary), "first-release-date": date,
            "artist-credit": [{"name": artist[1], "artist": {"id": artist[0], "name": artist[1]}}]}


class MusicBrainz:
    """Answers acquire's requests like MusicBrainz: catalogues by artist, editions' songs by release group (None: no
    official edition), searches by query."""

    def __init__(self):
        self.catalogues, self.songs, self.artists, self.found, self.releases, self.asked, self.info = {}, {}, [], [], {}, [], {}
        self.pseudo = {}  # artist -> its pseudo-releases

    def __call__(self, url, db=None, keep=0):
        self.asked.append(url)
        path, query = url.removeprefix(acquire.MB).split("?", 1)
        q = dict(urllib.parse.parse_qsl(query))
        if path == "release-group" and "artist" in q:
            groups = self.catalogues.get(q["artist"], [])
            return {"release-groups": groups[int(q["offset"]):][:100], "release-group-count": len(groups)}
        if path == "release" and "artist" in q:
            found = self.pseudo.get(q["artist"], [])
            return {"releases": found[int(q["offset"]):][:100], "release-count": len(found)}
        if path == "release":
            songs = self.songs.get(q["release-group"])
            video = {"format": "DVD-Video", "tracks": [{"title": "Live"}]}  # the smallest edition, but not the album
            return {"releases": [] if songs is None else [{"media": [{"tracks": [{"title": s} for s in songs]}]}, {"media": [video]},
                                                          {"media": [{"tracks": [{"title": s} for s in songs + ["Bonus"]]}] * 2 + [video]}]}
        if path.startswith("release-group/"):
            return next(g for gs in self.catalogues.values() for g in gs if g["id"] == path.split("/")[1])
        if path.startswith("release/"):
            return {"release-group": {"id": self.releases[path.split("/")[1]]}}
        if path == "artist":
            return {"artists": self.artists}
        if path == "release-group":
            return {"release-groups": self.found}
        if path.startswith("artist/"):  # aliases, for the search
            return self.info.get(path.split("/")[1], {"name": "Beta", "aliases": []})
        raise AssertionError(url)


class FakeDownloads:
    def __init__(self):
        self.active, self.found, self.result, self.free = set(), [], None, True

    def groups(self, db):
        return set(self.active)

    def room(self):
        return self.free

    def find(self, db, want, group):
        self.found.append(group)
        self.wants = getattr(self, "wants", []) + [want]
        return self.result

    def bundle(self, db, want, groups, wanted):
        self.found.append(("bundle", want["artist"], sorted(wanted)))
        return self.result


@pytest.fixture
def mb(monkeypatch):
    fake = MusicBrainz()
    monkeypatch.setattr(musicbrainz, "get", fake)
    return fake


@pytest.fixture
def client(library, tmp_path, mb):
    with make_client(library, tmp_path / "oasis.db") as c:
        c.app.state.api.library = Acquire(c.app.state.api._db, FakeDownloads())
        yield c


def sync(client):
    client.app.state.api.library.reconcile()


def artist_id(client, name):
    return next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == name)["id"]


def album_of(client, title):
    return next(a for a in call(client, "getAlbumList2", type="alphabeticalByName", size=50)["albumList2"]["album"] if a["name"] == title)


def wants(client) -> dict:
    db = client.app.state.api._db()
    return {r["title"]: (r["state"], r["reason"], r["error"]) for r in db.execute("select * from want_album")}


def follow_beta(client, mb):
    db = client.app.state.api._db()
    db.execute("update track set mb_release_group = 'g-first' where album = 'First'")
    db.execute("insert into mb_artist(mbid, artist_id, name) values ('mb-beta', ?, 'Beta')", (artist_id(client, "Beta"),))
    mb.catalogues["mb-beta"] = [
        group("g-first", "First", date="2001"), group("g-second", "Second", date="2003"),
        group("g-hits", "Greatest Hits", date="2005"), group("g-again", "Again", date="2006"),
        group("g-live", "Live at Home", date="2007", secondary=["Live"]), group("g-ost", "Film", date="2008", secondary=["Soundtrack"]),
        group("g-one", "One", "Single", "2001"), group("g-new", "New Song", "Single", "2009"), group("g-ep", "Promo", "EP", "2010"),
        group("g-deluxe", "Second (Deluxe)", date="2011")]
    mb.songs.update({"g-first": ["One", "Two"], "g-second": ["Three", "Four", "Five"], "g-hits": ["New"], "g-again": ["One", "Three", "Four", "Six"],
                     "g-ost": ["Theme"], "g-one": ["One (Radio Edit)"], "g-new": ["New", "One - Live"], "g-ep": None, "g-deluxe": ["Three"]})
    call(client, "star", artistId=artist_id(client, "Beta"))
    sync(client)


def test_a_followed_artists_catalogue_leaves_out_repackages_and_singles_with_nothing_new(client, mb):
    follow_beta(client, mb)
    assert wants(client) == {
        "First": ("have", "followed", None),  # its 2 tracks are here, as many as the smallest edition has
        "Second": ("missing", "followed", None), "Film": ("missing", "followed", "searched, no release found yet"),  # newest first
        "Greatest Hits": ("skipped", "followed", "a repackage"),
        "Again": ("skipped", "followed", "most of its songs are on earlier releases"),  # 3 of 4
        "One": ("skipped", "followed", "its songs are on the artist's albums"),
        "New Song": ("missing", "followed", None),  # a song on no album or EP kept
        "Promo": ("skipped", "followed", "no official release"),
        "Second (Deluxe)": ("skipped", "followed", "a repackage")}  # live albums aren't in the catalogue
    db = client.app.state.api._db()
    assert db.execute("select tracks, discs from mb_group where rg = 'g-second'").fetchone()[:] == (3, 2)
    assert db.execute("select state, catalogued is not null from lidarr_item where kind = 'artist'").fetchone()[:] == ("followed", 1)

    db.execute("update track set mb_release_group = 'g-hits' where album = 'Second'")  # a repackage with files isn't left out
    mb.catalogues["mb-beta"] = mb.catalogues["mb-beta"][1:]  # nor is what the catalogue lost kept
    db.execute("update lidarr_item set catalogued = 0")
    sync(client)
    assert wants(client)["Greatest Hits"] == ("have", "followed", None) and "First" not in wants(client)


def test_have_counts_library_tracks_and_maps_files_tagged_without_their_release_group(client, mb):
    follow_beta(client, mb)
    db = client.app.state.api._db()
    db.execute("update track set mb_release_group = null, mb_release = 'r-first' where album = 'First'")
    db.execute("update track set mb_release_group = 'g-second', mb_track = 'same' where album = 'Second'")  # 2 files of one track
    mb.releases["r-first"] = "g-first"
    sync(client)
    assert wants(client)["First"][0] == "have" and wants(client)["Second"][0] == "missing"
    assert {r[0] for r in db.execute("select mb_release_group from track where album = 'First'")} == {"g-first"}


def test_missing_albums_are_searched_in_order_and_again_ever_further_apart(client, mb, monkeypatch):
    follow_beta(client, mb)
    acq, now = client.app.state.api.library, [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    def search():
        now[0] += acquire.SEARCH_GAP
        sync(client)
        return acq.downloads.found[-1] if acq.downloads.found else None
    assert acq.downloads.found == ["g-ost"]  # newest album first; singles after albums and EPs
    assert search() == "g-second"
    assert search() == "g-new"
    assert search() == "g-new" and len(acq.downloads.found) == 3  # nothing due: no search
    db = client.app.state.api._db()
    row = db.execute("select * from want_album where rg = 'g-ost'").fetchone()
    assert (row["tries"], row["next_search"] - row["searched"], row["error"]) == (1, 6 * 3600, "searched, no release found yet")
    assert db.execute("select next_search - searched from want_album where rg = 'g-new'").fetchone()[0] == 7 * 86400
    now[0] += 6 * 3600
    assert search() == "g-ost"
    assert db.execute("select next_search - searched from want_album where rg = 'g-ost'").fetchone()[0] == 12 * 3600
    acq.downloads.free = False
    now[0] += 7 * 86400
    assert search() == "g-ost" and len(acq.downloads.found) == 4  # TorBox full: nothing starts
    acq.downloads.free, acq.downloads.result = True, False
    search()
    assert db.execute("select tries, next_search - ? from want_album where rg = ?", (now[0], acq.downloads.found[-1])).fetchone()[:] \
        in [(1, acquire.RETRY_FULL), (2, acquire.RETRY_FULL)]  # no free slot: soon again, not counted as a try
    acq.downloads.result = True
    sent = search()
    acq.downloads.active.add(sent)
    sync(client)
    assert wants(client)[db.execute("select title from want_album where rg = ?", (sent,)).fetchone()[0]][0] == "downloading"
    want = acq.downloads.wants[-1]
    assert want["artist"] == "Beta" and want["names"] == ["Beta"] and "Greatest Hits" in want["others"] and want["upgrade"] is False


def test_a_search_knows_the_albums_other_titles_from_its_aliases_and_pseudo_releases(client, mb):
    follow_beta(client, mb)
    second = next(g for g in mb.catalogues["mb-beta"] if g["id"] == "g-second")
    second["aliases"] = [{"name": "Zweite", "type": "Release group name"}, {"name": "2nd", "type": "Search hint"}]
    mb.pseudo["mb-beta"] = [{"title": t, "release-group": {"id": rg}} for t, rg in
                            [("Sekando", "g-second"), ("Second (Instrumental)", "g-second"), ("Film", "g-second"), ("Sekando", "g-second"),
                             ("Fuirumu", "g-ost"), ("Elsewhere", "g-unknown")]]
    db = client.app.state.api._db()
    assert acquire.other_titles(db, "mb-beta") == {"g-second": ["Zweite", "Sekando"], "g-ost": ["Fuirumu"]}
    row = db.execute("select * from want_album where rg = 'g-second'").fetchone()
    assert client.app.state.api.library.target(db, row)["other_titles"] == ["Zweite", "Sekando"]


def test_an_artist_with_gaps_gets_a_weekly_bundle_search_in_an_album_searchs_place(client, mb, monkeypatch):
    follow_beta(client, mb)
    acq, now = client.app.state.api.library, [time.time()]
    acq.bundles, acq.downloads.found = True, []
    acq.downloads.active.add("g-second")  # downloading on its own
    monkeypatch.setattr(time, "time", lambda: now[0])
    now[0] += acquire.SEARCH_GAP
    sync(client)
    assert acq.downloads.found == [("bundle", "Beta", ["g-new", "g-ost"])]  # Film was searched, for nothing yet
    now[0] += acquire.SEARCH_GAP
    sync(client)
    assert len(acq.downloads.found) == 2 and acq.downloads.found[1][0] != "bundle"  # an album search: the bundle one is weekly
    now[0] += acquire.BUNDLE_AGAIN
    sync(client)
    assert acq.downloads.found[2][0] == "bundle"


def test_collaboration_credits_split_into_artist_names():
    assert artist_names("SawanoHiroyuki[nZk]:mizuki") == ["SawanoHiroyuki[nZk]:mizuki", "SawanoHiroyuki[nZk]", "SawanoHiroyuki", "mizuki"]
    assert artist_names("Falling In Reverse, Corey Taylor, Serj Tankian")[:2] == ["Falling In Reverse, Corey Taylor, Serj Tankian", "Falling In Reverse"]
    assert artist_names("A feat. B")[1:] == ["A", "B"] and artist_names("X × Y")[1:] == ["X", "Y"]


def test_a_new_star_resolves_through_musicbrainz_search_exact_matches_only(client, mb):
    db = client.app.state.api._db()
    db.execute("insert into ytm_artist(artist_id, name, fetched) values ('ar-yt', 'Kenshi Yonezu', 0)")
    db.execute("insert into ytm_release values ('al-yt', 'ar-yt', 'MPRE', 'STRAY SHEEP', 2020, 'Album', null)")
    mb.artists = [{"id": "mb-other", "name": "Kenshi Yonezu Tribute", "aliases": []},
                  {"id": "mb-kenshi", "name": "米津玄師", "aliases": [{"name": "Kenshi Yonezu"}]}]
    mb.found = [group("g-live", "STRAY SHEEP (Live)", artist=("mb-kenshi", "米津玄師")), group("g-ss", "Stray Sheep", artist=("mb-kenshi", "米津玄師"))]
    mb.catalogues["mb-kenshi"] = mb.found
    mb.songs["g-ss"] = ["感電", "Lemon"]
    call(client, "star", albumId="al-yt")
    sync(client)
    assert any("arid%3Amb-kenshi" in u for u in mb.asked) and not any("arid%3Amb-other" in u for u in mb.asked)
    assert wants(client) == {"Stray Sheep": ("missing", "starred", "searched, no release found yet")}  # searched at once: starred first
    item = db.execute("select state, mbid, error from lidarr_item").fetchone()
    assert item[:] == ("wanted", "g-ss", "waiting for its search")
    sync(client)
    assert db.execute("select error from lidarr_item").fetchone()[0] == "searched, no release found yet"
    assert db.execute("select artist from want_album").fetchone()[0] == "米津玄師"

    mb.artists, mb.found = [], []  # an artist MusicBrainz doesn't know: failed, looked up again later
    call(client, "star", artistId=artist_id(client, "The Alpha"))
    sync(client)
    row = db.execute("select state, error, retry_at > ? from lidarr_item where kind = 'artist'", (time.time(),)).fetchone()
    assert row[:] == ("failed", "no MusicBrainz artist named The Alpha", 1)

    call(client, "unstar", albumId="al-yt")
    sync(client)
    assert wants(client) == {}


def test_unfollowing_drops_the_catalogue_but_keeps_starred_albums_and_unstarring_gives_them_back(client, mb):
    follow_beta(client, mb)
    db = client.app.state.api._db()
    db.execute("insert into lidarr_item(kind, ref, state, mbid, updated) values ('album', 'al-x', 'wanted', 'g-hits', 0)")
    db.execute("insert into wanted values ('admin', 'album', 'al-x')")
    sync(client)
    assert wants(client)["Greatest Hits"] == ("missing", "starred", None)  # starred: never left out
    call(client, "unstar", artistId=artist_id(client, "Beta"))
    sync(client)
    assert list(wants(client)) == ["Greatest Hits"]
    call(client, "star", artistId=artist_id(client, "Beta"))
    sync(client)
    db.execute("delete from wanted where ref = 'al-x'")
    sync(client)
    sync(client)
    assert wants(client)["Greatest Hits"] == ("skipped", "followed", "a repackage")  # back in the catalogue, left out again


def test_what_lidarr_searched_and_looked_for_still_counts_once_it_is_gone(client, mb):
    db = client.app.state.api._db()
    searched = time.time() - 3600
    db.executemany("insert into catalogue_search(album_id, searched, mbid) values (?, ?, ?)",
                   [(12, searched, "g-second"), (14, 0, "g-again"), (15, 0, "g-one")])  # as carried over from Lidarr
    follow_beta(client, mb)
    row = db.execute("select * from want_album where rg = 'g-second'").fetchone()
    assert (row["searched"], row["tries"], row["next_search"]) == (searched, 1, searched + acquire.RESEARCH)
    assert db.execute("select searched from want_album where rg = 'g-ost'").fetchone()[0] is not None  # never searched: first
    assert wants(client)["Again"] == ("missing", "followed", None)  # Lidarr looked for it: not left out
    assert wants(client)["One"][0] == "skipped"  # singles go by the singles rule


def test_catalogue_reads_keep_musicbrainzs_pace(tmp_path, monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append(time.monotonic())
        assert request.get_header("User-agent") == musicbrainz.USER_AGENT
        if "release-group?artist" in request.full_url:
            return io.BytesIO(json.dumps({"release-groups": [group(f"g{i}", f"T{i}") for i in range(2)], "release-group-count": 2}).encode())
        return io.BytesIO(json.dumps({"releases": [{"media": [{"tracks": [{"title": "x"}]}]}]}).encode())

    monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
    connect(tmp_path / "oasis.db").execute("insert into lidarr_item(kind, ref, state, mbid, updated) values ('artist', 'ar-b', 'followed', 'mb-beta', 0)")
    acq = Acquire(lambda: connect(tmp_path / "oasis.db"), FakeDownloads())
    read = lambda db: acq.catalogue(db, db.execute("select * from lidarr_item").fetchone())
    reader = threading.Thread(target=lambda: read(connect(tmp_path / "oasis.db")))
    other = threading.Thread(target=lambda: musicbrainz.get("https://musicbrainz.org/ws/2/release/r?fmt=json"))
    reader.start(), other.start()
    reader.join(), other.join()
    gaps = [b - a for a, b in zip(sorted(sent), sorted(sent)[1:])]
    assert len(sent) == 4 and min(gaps) >= 1.0


def test_downloads_failing_make_the_album_due_again(tmp_path):
    db = connect(tmp_path / "oasis.db")
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state, next_search) values ('g', 'a', 'T', 'followed', 'downloading', 9e9)")
    db.execute("insert into download(hash, album_id, album_mbid, title, state, created, updated) values ('h', 0, 'g', 'T', 'added', 0, 0)")

    class TorBox:
        def delete_torrent(self, id):
            pass
    made = Downloads(lambda: connect(tmp_path / "oasis.db"), TorBox, None, None, str(tmp_path / "dl"))
    assert made.groups(db) == {"g"}
    made.fail(db, db.execute("select * from download").fetchone(), "dead torrent")
    assert db.execute("select next_search, error from want_album").fetchone()[:] == (None, "download failed: dead torrent") and made.groups(db) == set()


KENSHI, STRAY, LEMON, BOOTLEG = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", \
    "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"


def kenshi(mb):
    """An artist only MusicBrainz has, found by an alias in another script."""
    credit = (KENSHI, "米津玄師")
    mb.artists = [{"id": KENSHI, "name": "米津玄師", "type": "Person", "country": "JP", "life-span": {"begin": "1991-03-10"},
                   "disambiguation": "J-pop", "aliases": [{"name": "Kenshi Yonezu"}]}]
    mb.info[KENSHI] = {**mb.artists[0], "aliases": []}
    mb.catalogues[KENSHI] = [group(STRAY, "STRAY SHEEP", date="2020-08-05", artist=credit), group(LEMON, "Lemon", "Single", "2018", artist=credit),
                             group(BOOTLEG, "BOOTLEG", date="2017", artist=credit)]
    mb.found = mb.catalogues[KENSHI][:1]
    mb.songs.update({STRAY: ["感電", "Lemon"], LEMON: ["Lemon"], BOOTLEG: ["灰色と青"]})


def test_musicbrainz_searches_are_cached_and_their_words_escaped(tmp_path, monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append(request.full_url)
        return io.BytesIO(json.dumps({"artists": [{"id": KENSHI, "name": "米津玄師"}]}).encode())
    monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
    db = connect(tmp_path / "oasis.db")
    assert acquire.find(db, "artist", 'Kenshi "Yonezu":') == acquire.find(db, "artist", "kenshi yonezu") == [{"id": KENSHI, "name": "米津玄師"}]
    assert sent == [acquire.MB + "artist?query=kenshi%20yonezu&limit=25&fmt=json"]  # once
    assert acquire.find(db, "album", "?!") == [] and len(sent) == 1


def test_the_dashboard_finds_follows_and_wants_any_artist_or_album(client, mb):
    kenshi(mb)
    sign_in(client)
    found = client.get("/music", params={"q": "Kenshi Yonezu"}).text
    assert f'href="/music/artist/{KENSHI}">米津玄師</a>' in found and "Person · JP · 1991 · J-pop" in found
    page = client.get(f"/music/artist/{KENSHI}").text
    assert page.index("STRAY SHEEP") < page.index("<h2>Singles</h2>") < page.index("Lemon") and "Follow</button>" in page
    page = client.post("/music/want", data={"kind": "artist", "mbid": KENSHI, "on": "1", "next": f"/music/artist/{KENSHI}"}).text
    assert "Followed" in page and "<b>followed</b>" in page and "Unfollow</button>" in page
    sync(client)
    assert wants(client) == {"STRAY SHEEP": ("missing", "followed", "searched, no release found yet"), "Lemon": ("skipped", "followed",
                             "its songs are on the artist's albums"), "BOOTLEG": ("missing", "followed", None)}
    page = client.post("/music/want", data={"kind": "album", "mbid": LEMON, "on": "1", "next": f"/music/artist/{KENSHI}"}).text
    assert "Wanted: it is searched for" in page
    sync(client)
    assert wants(client)["Lemon"] == ("missing", "starred", None)  # wanting is starring: never left out
    db = client.app.state.api._db()
    assert [tuple(r) for r in db.execute("select user, kind, ref from wanted order by kind")] == [("admin", "album", "al-mb-" + LEMON), ("admin", "artist", "ar-mb-" + KENSHI)]
    assert db.execute("select starred is not null from stat where id = ?", ("al-mb-" + LEMON,)).fetchone()[0] == 1  # as clients see it
    found = client.get("/music", params={"q": "stray sheep", "kind": "album"}).text
    assert f'— <a href="/music/artist/{KENSHI}">米津玄師</a>' in found and "Album · 2020" in found and "<b>wanted</b>" in found
    client.post("/music/want", data={"kind": "album", "mbid": LEMON, "next": "/music"})
    client.post("/music/want", data={"kind": "artist", "mbid": KENSHI, "next": "https://elsewhere"})
    sync(client)
    assert wants(client) == {} and db.execute("select count(*) from wanted").fetchone()[0] == 0
    assert "MusicBrainz has no such artist" in client.get("/music/artist/nope").text


def test_the_wanted_page_shows_each_albums_state_and_searches_blocks_or_gives_up(client, mb, monkeypatch):
    kenshi(mb)
    sign_in(client)
    api, now = client.app.state.api, time.time()
    client.post("/music/want", data={"kind": "artist", "mbid": KENSHI, "on": "1"})
    client.post("/music/want", data={"kind": "album", "mbid": LEMON, "on": "1"})
    sync(client)
    db = api._db()
    db.execute("update want_album set error = 'download failed: dead torrent' where rg = ?", (LEMON,))
    db.execute("update want_album set searched = ?, next_search = ? where rg = ?", (now, now + 3600, STRAY))
    db.execute("insert into download(hash, album_id, album_mbid, title, state, created, updated) values ('h', 0, ?, 'B', 'downloading', ?, ?)",
               (BOOTLEG, now, now))
    downloads = Downloads(api._db, None, None, None, "dl")
    downloads.client, downloads.seen["h"] = type("TorBox", (), {"delete_torrent": lambda self, id: None})(), (0.42, now)
    api.downloads = downloads
    api.library.downloads.active.add(BOOTLEG)
    sync(client)
    page = client.get("/music/wanted").text
    assert page.index("Needs a look") < page.index("Lemon") < page.index("Downloading") < page.index("BOOTLEG") \
        < page.index("Not found yet") < page.index("STRAY SHEEP")  # what needs a look first
    assert "42%" in page and "download failed: dead torrent" in page and "searched 0 min ago" in page
    filtered = client.get("/music/wanted", params={"type": "Single", "q": "stray"}).text
    assert "STRAY SHEEP" not in filtered or "Lemon" not in filtered
    library = client.get("/music/library").text
    assert "米津玄師" in library and "Lemon" in library and "wanted" in library and "downloading" in library

    assert "searched next" in client.post(f"/music/wanted/{STRAY}/search").text
    monkeypatch.setattr(api.library, "search", lambda db, row: setattr(api.library, "searched", row["rg"]))
    api.library.last_search = 0  # a search gap later
    sync(client)
    assert api.library.searched == STRAY  # before the starred album

    assert "Blocked that release" in client.post(f"/music/wanted/{BOOTLEG}/block").text
    assert db.execute("select state from download").fetchone()[0] == "failed" and db.execute("select 1 from release_block where hash = 'h'").fetchone()
    assert "Nothing is downloading" in client.post(f"/music/wanted/{BOOTLEG}/block").text
    api.library.downloads.active.clear()

    assert "Gave up on 米津玄師 – BOOTLEG" in client.post(f"/music/wanted/{BOOTLEG}/give-up").text
    client.post(f"/music/wanted/{LEMON}/give-up")
    assert "Gave up on 1 album" in client.post("/music/wanted/give-up", data={"rgs": STRAY}).text
    db.execute("delete from given_up where rg = ?", (STRAY,))  # wanted again
    db.execute("update lidarr_item set catalogued = 0")
    sync(client)
    sync(client)
    assert {t: w[:2] for t, w in wants(client).items()} == {"STRAY SHEEP": ("missing", "followed"), "Lemon": ("skipped", "followed"),
                                                            "BOOTLEG": ("skipped", "followed")}
    assert wants(client)["BOOTLEG"][2] == "given up" and db.execute("select count(*) from wanted where kind = 'album'").fetchone()[0] == 0
    client.post("/music/want", data={"kind": "album", "mbid": BOOTLEG, "on": "1"})
    sync(client)
    assert wants(client)["BOOTLEG"][:2] == ("missing", "starred")  # wanted again after all


def test_search3_offers_musicbrainz_artists_and_albums_that_starring_wants_and_that_keep_their_ids_once_here(client, mb):
    kenshi(mb)
    mb.artists.append({"id": "mb-beta", "name": "Beta"})  # in the library: left out
    client.app.state.api._db().execute("insert into mb_artist(mbid, artist_id, name) values ('mb-beta', ?, 'Beta')", (artist_id(client, "Beta"),))
    found = call(client, "search3", query="Kenshi Yonezu")["searchResult3"]
    assert [a["id"] for a in found["artist"]] == ["ar-mb-" + KENSHI] and [a["id"] for a in found["album"]] == ["al-mb-" + STRAY]
    db = client.app.state.api._db()
    db.execute("update track set album_artist = 'Kenshi Yonezu' where album = 'Second'")  # by another name: left out
    assert call(client, "search3", query="Kenshi Yonezu")["searchResult3"]["artist"][0]["name"] == "Kenshi Yonezu"
    assert len(call(client, "search3", query="Kenshi Yonezu")["searchResult3"]["artist"]) == 1
    db.execute("update track set album_artist = 'The Alpha' where album = 'Second'")
    assert found["album"][0]["artistId"] == "ar-mb-" + KENSHI and found["album"][0]["year"] == 2020
    asked = len(mb.asked)
    assert call(client, "search3", query="Ke")["searchResult3"]["artist"] == [] and len(mb.asked) == asked  # too short to ask

    artist = call(client, "getArtist", id="ar-mb-" + KENSHI)["artist"]
    assert artist["name"] == "米津玄師" and [a["name"] for a in artist["album"]] == ["BOOTLEG", "Lemon", "STRAY SHEEP"]
    call(client, "star", albumId="al-mb-" + STRAY)
    call(client, "star", artistId="ar-mb-" + KENSHI)
    sync(client)
    album = call(client, "getAlbum", id="al-mb-" + STRAY)["album"]
    assert album["song"] == [] and album["oasisWanted"] == "wanted" and album["starred"]
    assert wants(client)["STRAY SHEEP"][:2] == ("missing", "starred") and wants(client)["BOOTLEG"][1] == "followed"
    for method, key in (("getStarred2", "starred2"), ("getStarred", "starred")):
        starred = call(client, method)[key]
        assert [(a["id"], a["name"]) for a in starred["artist"]] == [("ar-mb-" + KENSHI, "米津玄師")]
        assert [(a["id"], a["name"], a["artistId"], a["oasisWanted"]) for a in starred["album"]] == \
               [("al-mb-" + STRAY, "STRAY SHEEP", "ar-mb-" + KENSHI, "wanted")] and starred["album"][0]["starred"]

    # it arrives
    db.execute("update track set mb_release_group = ? where album = 'First'", (STRAY,))
    beta = artist_id(client, "Beta")
    db.execute("insert into mb_artist(mbid, artist_id, name) values (?, ?, '米津玄師')", (KENSHI, beta))
    sync(client)
    local = album_of(client, "First")["id"]
    assert [r[0] for r in db.execute("select ref from wanted order by kind")] == [local, beta] and wants(client)["STRAY SHEEP"][0] == "have"
    assert call(client, "getAlbum", id="al-mb-" + STRAY)["album"]["id"] == local and call(client, "getArtist", id="ar-mb-" + KENSHI)["artist"]["id"] == beta
    assert album_of(client, "First")["starred"] and call(client, "search3", query="Kenshi Yonezu")["searchResult3"]["artist"] == []
    call(client, "unstar", albumId="al-mb-" + STRAY)
    assert db.execute("select count(*) from wanted where kind = 'album'").fetchone()[0] == 0
