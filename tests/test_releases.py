import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from oasis import download, releases
from oasis.db import connect
from oasis.download import info_hash, source
from oasis.releases import find, queries, rank, reject, target
from test_download import db, downloads, files, lidarr, tb, torbox  # noqa: F401 (fixtures)


def want(title, *names, year="", others=(), discs=1, upgrade=False, native=()):
    return {"artist": names[0], "names": list(names), "title": title, "year": year, "others": list(others), "discs": discs,
            "upgrade": upgrade, "native": list(native)}


def result(title, hash="", seeders=5, size=400e6, **extra):
    return {"title": title, "infoHash": hash, "seeders": seeders, "size": size, "guid": f"guid-{title}", "indexerId": 1, **extra}


CREEPY = want("アンサンブル・プレイ", "Creepy Nuts", "クリーピーナッツ", year="2022", others=["クリープ・ショー", "Case", "LEGION"])
RAMMSTEIN = want("[Rammstein]", "Rammstein", year="2019", others=["Herzeleid", "Sehnsucht", "Mutter", "Reise, Reise", "Rosenrot", "Zeit"])


@pytest.mark.parametrize("name, why", [
    ("[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC 48kHz/24bit]", None),
    ("Creepy Nuts - アンサンブル・プレイ (BBE VIVA) [FLAC 48kHz／24bit]", None),
    ("クリーピーナッツ「アンサンブル・プレイ」[MP3 320K]", None),  # the artist's name in Japanese
    ("[2025.03.10]LEGION Instrumentals／Creepy Nuts[FLAC 48kHz 24bit]", "no title"),
    ("Creepy Nuts - INDIES COMPLETE - アンサンブル・プレイ [FLAC]", "bundle"),
    ("Creepy Nuts 「アンサンブル・プレイ」(インスト) [FLAC]", "variant: インスト"),
    ("[2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・フレイ」[FLAC]", "no title"),  # one kana mark off is another word
])
def test_names_in_any_script_match_the_artist_and_title(name, why):
    assert reject(result(name), CREEPY) == why


@pytest.mark.parametrize("name, why", [
    ("Rammstein - Rammstein (2019) [FLAC]", None),
    ("Rammstein - Rammstein [FLAC]", None),                       # the title right after the artist
    ("Rammstein - Mutter (2001) [FLAC]", "no title"),             # the artist's name alone isn't the self-titled album
    ("VA - A Tribute To Rammstein (2023) [FLAC]", "no title"),
    ("Rammstein - Live aus Berlin Rammstein 2019", "variant: live"),
    ("Rammstein - Rammstein (2019) [FLAC] {Deluxe Edition}", "variant: deluxe"),
    ("(Industrial) [LP][24/96] Rammstein - 2LP Herzeleid; LP Rammstein, LP Rosenrot - 1995-2019, FLAC (tracks+.cue)", "bundle: also Herzeleid"),
    ("Rammstein - Discography Rammstein 1995-2019 [FLAC]", "bundle"),
    ("Rammstein - (2019)Rammstein (Special Edition)[FLAC]", None),
    ("Rammstein - This Is Rammstein (2019) Mp3 320kbps Songs", "self-titled: the title isn't right after the artist"),
])
def test_a_bracketed_self_titled_album_needs_its_title_apart_from_the_artist(name, why):
    assert reject(result(name), RAMMSTEIN) == why


def test_variants_bundles_videos_and_sizes_are_not_the_album():
    meteora = want("Meteora", "Linkin Park", year="2003", others=["Hybrid Theory", "Minutes to Midnight"])
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]"), meteora) is None
    assert reject(result("Linkin Park - Minutes to Midnight (Tour Edition) (2007) FLAC"), want("Minutes to Midnight", "Linkin Park")) == "variant: touredition"
    assert reject(result("Linkin Park - Meteora 20th Anniversary Edition (2023) FLAC"), meteora) == "variant: anniversary"
    assert reject(result("Linkin Park - Hybrid Theory + Meteora [FLAC]"), meteora) == "bundle: also Hybrid Theory"
    assert reject(result("David Hasselhoff - Open Your Eyes - 2019, FLAC"), want("Open Your Eyes", "David Hasselhoff", others=["David"])) is None
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]", size=6e9), meteora) == "size"
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]", size=6e9), {**meteora, "discs": 2}) is None
    # An anniversary edition wanted isn't the plain album.
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]"), want("Meteora (20th Anniversary Edition)", "Linkin Park")) == "not the variant: anniversary"
    party = want("The Hunting Party", "Linkin Park", year="2014")
    assert reject(result("Linkin Park - The Hunting Party Acapellas & Instrumentals"), party) == "variant: acapellas"
    assert reject(result("Linkin Park - The Hunting Party (2014) [FLAC 16-44] remastered"), party) is None
    assert reject(result("Linkin Park - The Hunting Party (Live in Texas) 2014"), party) == "variant: live"
    assert reject(result("Rammstein - Feuer Frei! [2002, Tanz-Metal, Industrial, DVD5]"), want("Feuer Frei!", "Rammstein")) == "video"
    assert reject(result("Linkin Park - Meteora - 2003, FLAC (image+.cue), lossless"), meteora) == "disc image"
    assert reject(result("Linkin Park - Meteora - 2003, FLAC (tracks+.cue), lossless"), meteora) is None
    assert reject(result("Jeris Johnson   Dragonborn (2024) Mp3 320kbps [PMEDIA] ⭐️", size=100e6), want("Dragonborn", "Jeris Johnson")) is None
    assert reject(result("Metallica - Hardwired To Self Destruct (2016)"), want("Hardwired…To Self-Destruct", "Metallica")) is None
    assert reject(result("Hunters - Arachne (2019)"), want("Arachne", "Hunter")) == "no artist"  # whole words only


def test_a_short_title_needs_the_year_the_artist_right_before_it_or_quotes():
    iv = want("iv", "SawanoHiroyuki[nZk]", year="2021")
    assert reject(result("SawanoHiroyuki[nZk] - iv [FLAC]"), iv) is None
    assert reject(result("[2021.03.10] SawanoHiroyuki[nZk] 4thアルバム「iV」[FLAC]"), iv) is None
    assert reject(result("SawanoHiroyuki[nZk] - R∃/MEMBER (feat. iv) [FLAC]"), iv) == "title too short: no year"


def test_rank_puts_lossless_first_then_cached_then_seeders_and_drops_uncached_seedless():
    flac, flac_cached = result("A - B [FLAC]", "aa", seeders=50), result("A - B [FLAC 24bit]", "bb", seeders=0)
    mp3_cached, v0 = result("A - B [MP3 320]", "cc", seeders=0), result("A - B [V0]", "dd", seeders=90)
    dead, twin = result("A - B [FLAC] dead", "ee", seeders=0), result("A - B [FLAC] other indexer", "aa", seeders=3)
    picks = rank([v0, dead, mp3_cached, flac, twin, flac_cached], {"bb", "cc"})
    assert [r["infoHash"] for r in picks] == ["bb", "aa", "cc", "dd"] and picks[1] is flac
    assert [r["infoHash"] for r in rank([v0, mp3_cached, flac], {"cc"}, upgrade=True)] == ["aa"]  # here lossy already: lossless only


def test_queries_drop_brackets_and_try_the_album_alone_and_the_native_names():
    assert queries(RAMMSTEIN) == ["Rammstein Rammstein", "Rammstein"]  # 'Rammstein' is 9 characters: distinctive
    assert queries(want("Case", "Creepy Nuts", "クリーピーナッツ", native=["クリーピーナッツ"])) == ["Creepy Nuts Case", "クリーピーナッツ Case"]
    assert queries(want("Living Things", "Linkin Park")) == ["Linkin Park Living Things", "Living Things"]


class Prowlarr:
    def __init__(self, results):
        self.results, self.asked = results, []

    def search(self, query):
        self.asked.append(query)
        return self.results.get(query, [])


def test_find_stops_at_the_first_query_with_a_usable_release_and_skips_blocked_ones():
    creepy = {**CREEPY, "native": ["クリーピーナッツ"]}
    prowlarr = Prowlarr({"Creepy Nuts アンサンブル・プレイ": [result("Creepy Nuts - アンサンブル・プレイ [FLAC]", "aa", seeders=0),
                                                         result("Creepy Nuts - Case [FLAC]", "bb")],
                         "アンサンブル・プレイ": [result("Creepy Nuts - アンサンブル・プレイ [MP3]", "cc", seeders=0)],
                         "クリーピーナッツ アンサンブル・プレイ": [result("クリーピーナッツ アンサンブル・プレイ [FLAC]", "dd"),
                                                       result("クリーピーナッツ アンサンブル・プレイ [MP3]", "ee")]})
    assert [r["infoHash"] for r in find(prowlarr, creepy, set(), lambda hashes: set())] == ["dd", "ee"]
    assert prowlarr.asked == ["Creepy Nuts アンサンブル・プレイ", "アンサンブル・プレイ", "クリーピーナッツ アンサンブル・プレイ"]
    prowlarr.asked = []
    assert [r["infoHash"] for r in find(prowlarr, creepy, set(), lambda hashes: {"aa"} & set(hashes))] == ["aa"]  # cached: seeders don't matter
    assert prowlarr.asked == ["Creepy Nuts アンサンブル・プレイ"]
    assert [r["infoHash"] for r in find(prowlarr, creepy, {"dd"}, lambda hashes: set())] == ["ee"]
    assert find(Prowlarr({}), creepy, set(), lambda hashes: set()) == []


def test_target_reads_the_artists_names_from_musicbrainz_and_the_other_albums_from_lidarr(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(releases.musicbrainz, "get", lambda url, db, keep: asked.append(url) or {"name": "Creepy Nuts", "aliases": [
        {"name": "クリーピーナッツ", "type": "Artist name"}, {"name": "CN", "type": "Artist name"}, {"name": "creepynuts", "type": "Search hint"},
        {"name": "Case", "type": "Artist name"}]})  # a former name that is an album's title

    class Lidarr:
        def albums_of(self, artist_id):
            return [{"id": 1, "title": "アンサンブル・プレイ", "albumType": "Album"}, {"id": 2, "title": "Case", "albumType": "Album"},
                    {"id": 3, "title": "眠れ", "albumType": "Single"}]
    album = {"id": 1, "artistId": 9, "title": "アンサンブル・プレイ", "releaseDate": "2022-09-07T00:00:00Z",
             "artist": {"artistName": "Creepy Nuts", "foreignArtistId": "mb-cn"}, "releases": [{"mediumCount": 2, "monitored": True}],
             "statistics": {"trackFileCount": 0}}
    found = target(connect(tmp_path / "oasis.db"), Lidarr(), album)
    assert asked == ["https://musicbrainz.org/ws/2/artist/mb-cn?inc=aliases&fmt=json"]
    assert found == {"artist": "Creepy Nuts", "names": ["Creepy Nuts", "クリーピーナッツ"], "native": ["クリーピーナッツ"], "title": "アンサンブル・プレイ",
                     "year": "2022", "discs": 2, "others": ["Case"], "upgrade": False}


class Proxy(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(301)
        self.send_header("Location", "magnet:?xt=urn:btih:abcd&dn=x")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_prowlarrs_proxied_magnet_link_is_followed_to_the_magnet():
    server = HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert source({"magnetUrl": f"http://127.0.0.1:{server.server_port}/1/download?apikey=k", "infoHash": "abcd"}) == \
            {"magnet": "magnet:?xt=urn:btih:abcd&dn=x"}
    finally:
        server.shutdown()
    assert source({"magnetUrl": "magnet:?xt=urn:btih:ef"}) == {"magnet": "magnet:?xt=urn:btih:ef"}


def test_with_prowlarr_the_search_is_oasiss_own_and_lidarr_lists_nothing(downloads, lidarr, torbox, db, tmp_path, monkeypatch):
    monkeypatch.setattr(releases.musicbrainz, "get", lambda url, db, keep: {"name": "Artist", "aliases": []})
    lidarr.albums_of = lambda artist_id: []
    lidarr.releases = lambda album_id: pytest.fail("Lidarr's search isn't used")
    downloads.prowlarr = Prowlarr({"Artist Album": [result("Artist - Album [FLAC]", "abcd", seeders=0, magnetUrl="magnet:?xt=urn:btih:abcd"),
                                                    result("Artist - Album [MP3]", "ef01", seeders=0, magnetUrl="magnet:?xt=urn:btih:ef01")]})
    torbox.have = {"ef01"}
    assert downloads.search(db, 5)
    assert torbox.added == [("Artist - Album [MP3]", "magnet:?xt=urn:btih:ef01", None)]  # the seedless FLAC nobody cached is no use
    assert db.execute("select hash, album_id, state from download").fetchone()[:] == ("ef01", 5, "added")


def test_a_release_listed_without_its_hash_is_skipped_when_it_failed_before(downloads, lidarr, torbox, db, monkeypatch):
    info = b"d6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces0:e"
    torrent = b"d8:announce3:url4:info" + info + b"e"
    assert info_hash({"torrent": torrent}) == hashlib.sha1(info).hexdigest() and info_hash({"torrent": b"<html>"}) == ""
    assert info_hash({"magnet": "magnet:?xt=urn:btih:" + "AB" * 20 + "&dn=x"}) == "ab" * 20
    monkeypatch.setattr(releases.musicbrainz, "get", lambda url, db, keep: {"name": "Artist", "aliases": []})
    lidarr.albums_of = lambda artist_id: []
    monkeypatch.setattr(download, "source", lambda release: {"torrent": torrent} if release["guid"] == "guid-Artist - Album [FLAC]"
                        else {"magnet": release["magnetUrl"]})
    db.execute("insert into release_block values (?, 'dead', 0)", (hashlib.sha1(info).hexdigest(),))
    downloads.prowlarr = Prowlarr({"Artist Album": [result("Artist - Album [FLAC]", seeders=9, downloadUrl="http://prowlarr/1/download"),
                                                    result("Artist - Album [MP3]", "ef01", magnetUrl="magnet:?xt=urn:btih:ef01")]})
    assert downloads.search(db, 5)
    assert torbox.added == [("Artist - Album [MP3]", "magnet:?xt=urn:btih:ef01", None)]


LP = {"artist": "Linkin Park", "names": ["Linkin Park"], "native": [], "albums": ["Hybrid Theory", "Hybrid Theory", "Meteora", "Minutes to Midnight", "One More Light"]}
LP_GROUPS = [{"id": "g-ht-ep", "title": "Hybrid Theory", "first-release-date": "1999"}, {"id": "g-ht", "title": "Hybrid Theory", "first-release-date": "2000"},
             {"id": "g-met", "title": "Meteora", "first-release-date": "2003"}, {"id": "g-mtm", "title": "Minutes to Midnight", "first-release-date": "2007"},
             {"id": "g-oml", "title": "One More Light", "first-release-date": "2017"}, {"id": "g-one", "title": "One", "first-release-date": "2017"}]
SONGS = {"g-ht-ep": ["Carousel", "Technique"], "g-ht": ["Papercut", "One Step Closer", "In the End"], "g-met": ["Foreword", "Don't Stay"],
         "g-mtm": ["Wake", "Given Up", "Leave Out All the Rest"], "g-oml": ["Nobody Can Save Me", "Heavy"], "g-one": ["One"]}


def editions(rg):
    return [{"status": "Official", "media": [{"position": 1, "tracks": [{"id": f"{rg}-{n}", "position": n + 1, "title": t, "length": 200000, "recording": {}}
                                                                        for n, t in enumerate(SONGS[rg])]}]}]


def listing(folders: dict) -> list[dict]:
    return [{"name": f"LP Discography/{folder}/{name}", "size": 1} for folder, names in folders.items() for name in names]


DISCOGRAPHY = listing({
    "Linkin Park - Hybrid Theory (2000) [FLAC]": ["01. Papercut.flac", "02. One Step Closer.flac", "03. In the End.flac", "cover.jpg", "rip.log"],
    "Linkin Park - Hybrid Theory (2000) [MP3]": ["01. Papercut.mp3", "02. One Step Closer.mp3", "03. In the End.mp3"],
    "2003 - Meteora": ["01 Foreword.flac"],  # a track short
    "2003 - Meteora (Live)": ["01 Foreword.flac", "02 Don't Stay.flac"],
    "Minutes to Midnight (2007)/CD1": ["01 Wake.flac", "02 Given Up.flac"],
    "Minutes to Midnight (2007)/CD2": ["01 Leave Out All the Rest.flac"],
    "Minutes to Midnight (2007)": ["folder.jpg"],
    "One More Light (2017)": ["01 Nobody Can Save Me.flac", "02 Heavy.flac"],
    "Hybrid Theory (image+cue)": ["Hybrid Theory.flac", "Hybrid Theory.cue"],
})


def test_a_bundles_file_list_gives_the_wanted_albums_each_folder_wholly_holds():
    wanted = {"g-ht", "g-ht-ep", "g-met", "g-mtm", "g-one"}  # One More Light is here already
    found = releases.targets(DISCOGRAPHY, LP, LP_GROUPS, wanted, editions)
    assert set(found) == {"g-ht", "g-mtm"}  # not the EP (another year), the partial or live Meteora, or One (inside One More Light)
    assert found["g-ht"] == [f"LP Discography/Linkin Park - Hybrid Theory (2000) [FLAC]/{n}" for n in ("01. Papercut.flac", "02. One Step Closer.flac",
                                                                                                       "03. In the End.flac", "cover.jpg")]
    assert sorted(found["g-mtm"]) == sorted(f"LP Discography/Minutes to Midnight (2007)/{n}" for n in ("CD1/01 Wake.flac", "CD1/02 Given Up.flac",
                                                                                                        "CD2/01 Leave Out All the Rest.flac", "folder.jpg"))
    # Without a year, one title of two release groups is neither.
    assert releases.targets(listing({"Hybrid Theory": ["01 Papercut.flac", "02 One Step Closer.flac", "03 In the End.flac"]}),
                            LP, LP_GROUPS, wanted, editions) == {}


def test_a_bundle_is_the_artists_and_says_so_or_holds_two_albums():
    assert releases.is_bundle(result("Linkin Park - Discography (2000-2017) [FLAC]"), LP)
    assert releases.is_bundle(result("Linkin Park - Hybrid Theory + Meteora [FLAC]"), LP)
    assert not releases.is_bundle(result("Linkin Park - Meteora [FLAC]"), LP)
    assert not releases.is_bundle(result("Pink Floyd - Discography [FLAC]"), LP)
    assert not releases.is_bundle(result("Linkin Park - Discography [DVD] 1080p"), LP)


def test_only_cached_bundles_with_wanted_albums_are_picked_most_albums_first():
    prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "aa"), result("Linkin Park - Discography [MP3]", "bb"),
                                                     result("Linkin Park - Complete [FLAC]", "cc", seeders=900), result("Linkin Park - Meteora", "dd")],
                         "Linkin Park": [result("Linkin Park - Discography [FLAC]", "aa"), result("Linkin Park - Box Set", "ee")]})
    listed = {"aa": DISCOGRAPHY, "bb": listing({"Hybrid Theory (2000)": ["01 Papercut.mp3", "02 One Step Closer.mp3", "03 In the End.mp3"]}),
              "ee": listing({"Live (2005)": ["01 Papercut.flac"]})}
    asked = []
    cached = lambda hashes: asked.append(hashes) or {h: listed[h] for h in hashes if h in listed}
    picks = releases.bundles(prowlarr, LP, LP_GROUPS, {"g-ht", "g-mtm"}, {"bb"}, cached, editions)
    assert [(r["infoHash"], sorted(t)) for r, t in picks] == [("aa", ["g-ht", "g-mtm"])]  # cc isn't cached; bb is blocked; ee holds nothing wanted
    assert prowlarr.asked == ["Linkin Park discography", "Linkin Park"] and sorted(asked[0]) == ["aa", "cc", "ee"]  # dd isn't a bundle


def test_a_cached_bundle_fetches_only_its_wanted_albums_and_imports_each(downloads, lidarr, torbox, db, tmp_path, monkeypatch):
    class Own:
        def place(self, folder, group):
            placed[group] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
            if group in failing:
                raise LookupError("no edition matches")
            return tmp_path / "music" / group

    placed, failing, downloads.own = {}, {"g-mtm"}, Own()
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {"abcd": DISCOGRAPHY} if "abcd" in hashes else {}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht", "g-mtm", "g-met"})
    row = db.execute("select * from download").fetchone()
    assert (row["hash"], row["album_mbid"], row["state"]) == ("abcd", None, "added") and downloads.groups(db) == {"g-ht", "g-mtm"}

    listed = files(tmp_path, *(f["name"] for f in DISCOGRAPHY))
    torbox.list = [{**tb(71, "abcd", progress=1, active=False), "download_state": "completed", "download_finished": True, "download_present": True,
                    "files": listed}]
    downloads.cycle()
    ids = {f["name"].split("/", 1)[1]: f["id"] for f in listed}
    fetched = [*(f"Linkin Park - Hybrid Theory (2000) [FLAC]/{n}" for n in ("01. Papercut.flac", "02. One Step Closer.flac", "03. In the End.flac", "cover.jpg")),
               *(f"Minutes to Midnight (2007)/{n}" for n in ("CD1/01 Wake.flac", "CD1/02 Given Up.flac", "CD2/01 Leave Out All the Rest.flac", "folder.jpg"))]
    assert sorted(torbox.links) == sorted(ids[n] for n in fetched)  # not the MP3 copy, the partial album or the log
    assert placed == {"g-ht": ["01. Papercut.flac", "02. One Step Closer.flac", "03. In the End.flac", "cover.jpg"],
                      "g-mtm": ["CD1/01 Wake.flac", "CD1/02 Given Up.flac", "CD2/01 Leave Out All the Rest.flac", "folder.jpg"]}
    assert dict(db.execute("select rg, state from download_target").fetchall()) == {"g-ht": "done", "g-mtm": "failed"}
    assert db.execute("select state from download").fetchone()[0] == "done" and torbox.deleted == [71] and not (tmp_path / "dl/abcd").exists()
    assert lidarr.rescanned == [str(tmp_path / "music/g-ht")] and downloads.groups(db) == set() and downloads.scans == [1]

    # None of a bundle's albums imports: it fails and is blocked.
    failing.add("g-ht")
    downloads.prowlarr.results["Linkin Park discography"][0].update(infoHash="bcde", magnetUrl="magnet:?xt=urn:btih:bcde")
    torbox.cached_files = lambda hashes: {"bcde": DISCOGRAPHY}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht", "g-mtm"})
    torbox.list = [{**torbox.list[0], "id": 72, "hash": "bcde"}]
    downloads.cycle()
    assert db.execute("select state, error from download where hash = 'bcde'").fetchone()[:] == ("failed", "import failed: no album of the bundle matched")
    assert "bcde" in downloads.blocked(db)
