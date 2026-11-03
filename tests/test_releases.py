import hashlib
import json
import shutil
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from oasis import download, releases
from oasis.download import info_hash, source
from oasis.quality import LossySource
from oasis.releases import find, queries, rank, reject
from test_download import WANT, Prowlarr, db, downloads, files, finished, result, tb, torbox  # noqa: F401 (fixtures)


def want(title, *names, year="", others=(), discs=1, upgrade=False, native=()):
    return {"artist": names[0], "names": list(names), "title": title, "year": year, "others": list(others), "discs": discs,
            "upgrade": upgrade, "native": list(native), "other_titles": []}


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


ON_ITS_OWN = "the artist's name isn't on its own"


@pytest.mark.parametrize("name, artists, why", [
    ("Hunter - Imperium (2013) [FLAC]", ["Hunter"], None),
    ("Hunter (Poland) - Imperium (2013) [FLAC]", ["Hunter"], None),
    ("[2013] Hunter - Imperium [FLAC]", ["Hunter"], None),
    ("Imperium (2013) - Hunter [FLAC]", ["Hunter"], None),                  # the title first
    ("Hunter Imperium 2013 FLAC", ["Hunter"], None),                        # the title right after
    ("Hunter_-_Imperium_(2013)_[FLAC]", ["Hunter"], None),
    ("Tab Hunter - Imperium (2013) [FLAC]", ["Hunter"], ON_ITS_OWN),
    ("Demon Hunter - Imperium (2013) [FLAC]", ["Hunter"], ON_ITS_OWN),
    ("Hunter Hayes - Imperium (2013) [FLAC]", ["Hunter"], ON_ITS_OWN),
    ("Hunter x Hunter OST - Imperium", ["Hunter"], ON_ITS_OWN),
    ("Imperium - Tab Hunter [FLAC]", ["Hunter"], ON_ITS_OWN),
    ("Lou Reed & Metallica - Lulu (2011) [FLAC] 88", ["Lou Reed"], None),   # joint credits
    ("Kanye West & Jay-Z - Watch The Throne", ["JAŸ-Z", "Jay-Z"], None),
    ("Linkin Park   Jay Z  Collision Course (2004) [FLAC]", ["JAŸ-Z", "Jay-Z"], None),  # separators dropped, their spaces left
    ("[TSDM自購][Hi-Res][241101]ゲーム『Limbus Company』収録曲「Hero」／Mili[96kHz/24bit][FLAC]", ["Mili"], None),
    ("Mili feat.KIHOW - Hero [FLAC]", ["Mili"], None),
])
def test_the_artists_name_stands_on_its_own_not_inside_anothers(name, artists, why):
    titles = {"Lulu": "Lulu", "Watch": "Watch the Throne", "Collision": "Collision Course", "Hero": "Hero"}
    title = next((t for k, t in titles.items() if k in name), "Imperium")
    assert reject(result(name), want(title, *artists, year="2013" if title == "Imperium" else "")) == why


def test_variants_bundles_videos_and_sizes_are_not_the_album():
    meteora = want("Meteora", "Linkin Park", year="2003", others=["Hybrid Theory", "Minutes to Midnight"])
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]"), meteora) is None
    assert reject(result("Linkin Park - Minutes to Midnight (Tour Edition) (2007) FLAC"), want("Minutes to Midnight", "Linkin Park")) == "variant: touredition"
    assert reject(result("Linkin Park - Meteora 20th Anniversary Edition (2023) FLAC"), meteora) == "variant: anniversary"
    assert reject(result("Linkin Park - Hybrid Theory + Meteora [FLAC]"), meteora) == "bundle: also Hybrid Theory"
    assert reject(result("David Hasselhoff - Open Your Eyes - 2019, FLAC"), want("Open Your Eyes", "David Hasselhoff", others=["David"])) is None
    assert reject(result("Rammstein - Feuer frei!, Du hast [2013, Industrial, HDTV 1080i]"), want("Feuer frei!", "Rammstein")) == "video"
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]", size=6e9), meteora) == "size"
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]", size=6e9), {**meteora, "discs": 2}) is None
    # An anniversary edition wanted isn't the plain album.
    assert reject(result("Linkin Park - Meteora (2003) [FLAC]"), want("Meteora (20th Anniversary Edition)", "Linkin Park")) == "not the variant: anniversary"
    party = want("The Hunting Party", "Linkin Park", year="2014")
    assert reject(result("Linkin Park - The Hunting Party Acapellas & Instrumentals"), party) == "variant: acapellas"
    assert reject(result("Linkin Park - The Hunting Party (2014) [FLAC 16-44] remastered"), party) is None
    assert reject(result("Linkin Park - The Hunting Party (Live in Texas) 2014"), party) == "variant: live"
    assert reject(result("Rammstein - Feuer Frei! [2002, Tanz-Metal, Industrial, DVD5]"), want("Feuer Frei!", "Rammstein")) == "video"
    assert reject(result("Linkin Park - Meteora - 2003, FLAC (image+.cue), lossless"), meteora) is None
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
    flac, flac_cached = result("A - B [FLAC]", "aa", seeders=50, downloadUrl="http://p/1"), result("A - B [FLAC 24bit]", "bb", seeders=0)
    mp3_cached, v0 = result("A - B [MP3 320]", "cc", seeders=0), result("A - B [V0]", "dd", seeders=90, downloadUrl="http://p/2")
    dead, twin = result("A - B [FLAC] dead", "ee", seeders=0), result("A - B [FLAC] other indexer", "aa", seeders=3)
    picks = rank([v0, dead, mp3_cached, flac, twin, flac_cached], {"bb", "cc"})
    assert [r["infoHash"] for r in picks] == ["bb", "aa", "cc", "dd"] and picks[1] is flac
    assert [r["infoHash"] for r in rank([v0, mp3_cached, flac], {"cc"}, upgrade=True)] == ["aa"]  # here lossy already: lossless only


def test_a_disc_image_comes_after_a_file_per_track():
    image, tracks = result("A - B (image+.cue) [FLAC]", "aa", seeders=90), result("A - B (tracks+.cue) [FLAC]", "bb", seeders=1, downloadUrl="http://p/1")
    assert [r["infoHash"] for r in rank([image, tracks], {"aa"})] == ["bb", "aa"]


def test_a_magnet_link_comes_after_every_release_oasis_can_read_before_it_goes():
    magnet = result("A - B [FLAC]", "aa", seeders=90, magnetUrl="http://prowlarr/1/download")
    torrent, cached = result("A - B [MP3 320]", "bb", seeders=2, downloadUrl="http://prowlarr/2/download"), result("A - B [MP3 128]", "cc", seeders=0)
    assert [r["infoHash"] for r in rank([magnet, torrent, cached], {"cc"})] == ["cc", "bb", "aa"]
    assert [r["infoHash"] for r in rank([magnet], set())] == ["aa"]  # nothing readable: the magnet link goes


def test_queries_drop_brackets_and_try_the_album_alone_and_the_native_names():
    assert queries(RAMMSTEIN) == ["Rammstein Rammstein", "Rammstein"]  # 'Rammstein' is 9 characters: distinctive
    assert queries(want("Case", "Creepy Nuts", "クリーピーナッツ", native=["クリーピーナッツ"])) == ["Creepy Nuts Case", "クリーピーナッツ Case"]
    assert queries(want("Living Things", "Linkin Park")) == ["Linkin Park Living Things", "Living Things"]


def test_an_album_is_also_found_and_matched_by_its_other_titles():
    creepy = {**CREEPY, "other_titles": ["Ensemble Play"]}
    assert queries(creepy)[-1] == "Creepy Nuts Ensemble Play"
    assert reject(result("Creepy Nuts - Ensemble Play (2022) [FLAC 24bit]"), creepy) is None
    assert reject(result("Creepy Nuts - Ensemble Play (2022) [FLAC 24bit]"), CREEPY) == "no title"
    assert reject(result("Creepy Nuts - Creep Show (2022) [FLAC]"), creepy) == "no title"
    assert reject(result("Creepy Nuts - Ensemble Play (Instrumentals) [FLAC]"), creepy) == "variant: instrumentals"
    # Only whole titles of this album, as a variant of none and another album's title never
    assert releases.other_titles("ビリケン", ["Biriken", "BIRIKEN (Instrumental)", "biriken", "Case", "ビリケン"], ["Case"]) == ["Biriken"]


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


def test_a_seedless_release_only_counts_when_torbox_has_it_cached(downloads, torbox, db):
    downloads.prowlarr = Prowlarr({"Artist Album": [result("Artist - Album [FLAC]", "abcd", seeders=0, magnetUrl="magnet:?xt=urn:btih:abcd"),
                                                    result("Artist - Album [MP3]", "ef01", seeders=0, magnetUrl="magnet:?xt=urn:btih:ef01")]})
    torbox.have = {"ef01"}
    assert downloads.find(db, WANT, "mb-album")
    assert torbox.added == [("Artist - Album [MP3]", "magnet:?xt=urn:btih:ef01", None)]  # the seedless FLAC nobody cached is no use
    assert db.execute("select hash, album_mbid, state from download").fetchone()[:] == ("ef01", "mb-album", "added")


def test_a_release_listed_without_its_hash_is_skipped_when_it_failed_before(downloads, torbox, db, monkeypatch):
    info = b"d6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces0:e"
    torrent = b"d8:announce3:url4:info" + info + b"e"
    assert info_hash({"torrent": torrent}) == hashlib.sha1(info).hexdigest() and info_hash({"torrent": b"<html>"}) == ""
    assert info_hash({"magnet": "magnet:?xt=urn:btih:" + "AB" * 20 + "&dn=x"}) == "ab" * 20
    monkeypatch.setattr(download, "source", lambda release: {"torrent": torrent} if release["guid"] == "guid-Artist - Album [FLAC]"
                        else {"magnet": release["magnetUrl"]})
    db.execute("insert into release_block(hash, reason, at) values (?, 'dead', 0)", (hashlib.sha1(info).hexdigest(),))
    downloads.prowlarr = Prowlarr({"Artist Album": [result("Artist - Album [FLAC]", seeders=9, downloadUrl="http://prowlarr/1/download"),
                                                    result("Artist - Album [MP3]", "ef01", magnetUrl="magnet:?xt=urn:btih:ef01")]})
    assert downloads.find(db, WANT, "mb-album")
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


def test_the_songs_tell_which_of_the_groups_a_folder_may_be_it_is_and_a_folder_named_otherwise_is_the_group_it_holds_exactly():
    wanted = {"g-ht", "g-ht-ep", "g-met"}
    album, ep = ["01 Papercut.flac", "02 One Step Closer.flac", "03 In the End.flac"], ["1. Carousel.mp3", "2. Technique.mp3"]
    assert releases.targets(listing({"Hybrid Theory": album}), LP, LP_GROUPS, wanted, editions) == {"g-ht": [f"LP Discography/Hybrid Theory/{n}" for n in album]}
    assert releases.targets(listing({"Hybrid Theory": ep}), LP, LP_GROUPS, wanted, editions) == {"g-ht-ep": [f"LP Discography/Hybrid Theory/{n}" for n in ep]}
    assert releases.targets(listing({"Hybrid Theory": ["01 Intro.flac", "02 Outro.flac"]}), LP, LP_GROUPS, wanted, editions) == {}  # numbers don't do here
    # Named after none of the artist's albums: the one it holds song for song, not one it holds part of or more than.
    assert releases.targets(listing({"Fan Club CD (2003)": ["Foreword.flac", "Don't Stay.flac"]}), LP, LP_GROUPS, wanted, editions) == \
        {"g-met": ["LP Discography/Fan Club CD (2003)/Foreword.flac", "LP Discography/Fan Club CD (2003)/Don't Stay.flac"]}
    assert releases.targets(listing({"Fan Club CD": ["Foreword.flac", "Don't Stay.flac", "Bonus.flac"]}), LP, LP_GROUPS, wanted, editions) == {}
    assert releases.targets(listing({"Live Bootleg": ["Foreword.flac", "Don't Stay.flac"]}), LP, LP_GROUPS, wanted, editions) == {}  # a variant


@pytest.mark.parametrize("folder, title", [
    ("2014 - Underground XIV", "Underground 14"), ("Linkin Park - LP Underground Fourteen (2014)", "Underground 14"),
    ("2006 - Underground 6.0", "Underground 6"), ("Underground V6 [2006]", "Underground 6"),
    ("2009 - Underground 9.0 (Demos)", "LP Underground 9: Demos"), ("(2009) Underground 9", "LP Underground 9: Demos"),
    ("2010 - Underground X", "LP Underground X: Demos"), ("2011 - Underground 11", "Underground Eleven"),
    ("Underground V2.5 [Limited Edition Fan Club CD]", "Underground V2.5: Limited Edition Fan Club CD"), ("Underground 2.0 (2002)", "Underground V2.0"),
    ("2011 - North American Tour EP", "2011 North American Tour EP"), ("2002 - In the End (Live & Rare)", "In the End: Live & Rare"),
    ("Linkin Park - Hybrid Theory [EP]", "Hybrid Theory EP"), ("Hybrid Theory CD-EP", "Hybrid Theory EP"), ("1999 - Hybrid Theory", "Hybrid Theory EP"),
    ("Hybrid Theory (2000)", "Hybrid Theory"),
    ("2008 - Underground 8.0 (mmm...Cookies - Sweet Hamster Like Jewels From America!)", "Mmm...Cookies: Sweet Hamster Like Jewels from America!"),
])
def test_a_folder_is_matched_by_its_title_however_numbered_prefixed_subtitled_or_typed(folder, title):
    group = lambda id, title, date, kind="EP": {"id": id, "title": title, "first-release-date": date, "primary-type": kind}
    groups = [group("Underground 14", "LP Underground XIV", "2014"), group("Underground 6", "Underground 6", "2006"),
              group("Underground V2.5: Limited Edition Fan Club CD", "Underground V2.5: Limited Edition Fan Club CD", "2003", "Album"),
              group("Underground V2.0", "Underground V2.0", "2002"), group("Hybrid Theory EP", "Hybrid Theory", "1999"),
              group("Hybrid Theory", "Hybrid Theory", "2000", "Album"), group("In the End", "In the End", "2001", "Single"),
              *(group(t, t, d) for t, d in [("LP Underground 9: Demos", "2009"), ("LP Underground X: Demos", "2010"), ("Underground Eleven", "2011"),
                                           ("2011 North American Tour EP", "2010-12-20"), ("In the End: Live & Rare", "2002"),
                                           ("Mmm...Cookies: Sweet Hamster Like Jewels from America!", "2008")])]
    assert releases.folder_groups(folder, LP, groups) == [title]


def test_a_wanted_single_is_also_found_as_loose_tracks_by_their_exact_titles():
    groups = [*LP_GROUPS, {"id": "g-nd", "title": "New Divide", "first-release-date": "2009", "primary-type": "Single"},
              {"id": "g-qw", "title": "QWERTY", "first-release-date": "2006", "primary-type": "Single"}]
    songs = {**SONGS, "g-nd": ["New Divide"], "g-qw": ["QWERTY", "Behind My Eyes"]}
    tracks = lambda rg: [{"status": "Official", "media": [{"position": 1, "tracks": [{"id": t, "position": n + 1, "title": t, "recording": {}} for n, t in enumerate(songs[rg])]}]}]
    bundle = listing({"Singles": ["Linkin Park - New Divide (Live).flac", "Linkin Park - New Divide.mp3", "Linkin Park - New Divide.flac", "05 QWERTY.flac",
                                  "Linkin Park - Divide.flac"],
                      "Transformers OST (2009)": ["03 Behind My Eyes Again.flac"]})
    assert releases.targets(bundle, LP, groups, {"g-nd", "g-qw"}, tracks) == {"g-nd": ["LP Discography/Singles/Linkin Park - New Divide.flac"]}  # not the live one
    bundle += listing({"Misc": ["Behind My Eyes.mp3"]})  # QWERTY's two tracks, wherever they are
    assert releases.targets(bundle, LP, groups, {"g-qw"}, tracks) == {"g-qw": ["LP Discography/Singles/05 QWERTY.flac", "LP Discography/Misc/Behind My Eyes.mp3"]}


def test_a_title_tracks_single_neither_takes_its_albums_folder_nor_keeps_it_from_the_album():
    groups = [{"id": "g-oml", "title": "One More Light", "first-release-date": "2017", "primary-type": "Album"},
              {"id": "g-oml-s", "title": "One More Light", "first-release-date": "2017", "primary-type": "Single"}]
    songs = {"g-oml": ["Nobody Can Save Me", "One More Light"], "g-oml-s": ["One More Light"]}
    tracks = lambda rg: [{"status": "Official", "media": [{"position": 1, "tracks": [{"id": t, "position": n + 1, "title": t, "recording": {}} for n, t in enumerate(songs[rg])]}]}]
    album = listing({"One More Light (2017)": ["01 Nobody Can Save Me.flac", "02 One More Light.flac"]})
    assert releases.folder_album("One More Light (2017)", LP, groups) == "g-oml"
    assert releases.folder_album("One More Light (2017) [Single]", LP, groups) == "g-oml-s"
    found = releases.targets(album, LP, groups, {"g-oml", "g-oml-s"}, tracks)
    assert found == {"g-oml": [f["name"] for f in album], "g-oml-s": [album[1]["name"]]}  # one file, a copy for each


def test_a_bundles_folder_of_disc_images_is_taken_by_its_name_and_size():
    """Meteora's two tracks last 400 s: a lossless image of them is some 40 MB."""
    image = lambda folder, size, *more: [{"name": f"LP/{folder}/{n}", "size": size if n.endswith(".flac") else 1} for n in ("Meteora.flac", "Meteora.cue", *more)]
    found = releases.targets(image("Meteora (2003) [FLAC image+cue]", 40e6, "cover.jpg", "rip.log"), LP, LP_GROUPS, {"g-met"}, editions)
    assert found == {"g-met": [f"LP/Meteora (2003) [FLAC image+cue]/{n}" for n in ("Meteora.flac", "Meteora.cue", "cover.jpg")]}
    assert releases.targets(image("Meteora (2003)", 1e6), LP, LP_GROUPS, {"g-met"}, editions) == {}  # too small to be the album
    assert releases.targets(image("Meteora (2003)", 40e6)[:1], LP, LP_GROUPS, {"g-met"}, editions) == {}  # no cue sheet: one track
    discs = [f for d in ("CD1", "CD2") for f in image(f"Meteora (2003)/{d}", 20e6)]
    assert sorted(releases.targets(discs, LP, LP_GROUPS, {"g-met"}, editions)["g-met"]) == sorted(f["name"] for f in discs)
    tracks = [{"name": f"LP/Meteora (2003) [tracks+cue]/{n}", "size": 20e6} for n in ("01 Foreword.flac", "02 Don't Stay.flac", "Meteora.cue")]
    assert releases.targets(image("Meteora (2003) [image]", 40e6) + tracks, LP, LP_GROUPS, {"g-met"}, editions) == \
        {"g-met": [f["name"] for f in tracks]}  # a file per track first


def test_a_bundles_album_folder_may_be_named_by_one_of_its_other_titles():
    groups = [{"id": "g-ens", "title": "アンサンブル・プレイ", "first-release-date": "2022", "other_titles": ["Ensemble Play"]},
              {"id": "g-cs", "title": "クリープ・ショー", "first-release-date": "2023"}]
    assert releases.folder_album("Creepy Nuts - Ensemble Play (2022) [FLAC]", CREEPY, groups) == "g-ens"
    assert releases.folder_album("2022 - アンサンブル・プレイ (Ensemble Play)", CREEPY, groups) == "g-ens"  # both its titles: still one album
    assert releases.folder_album("Creepy Nuts - Creep Show (2023)", CREEPY, groups) is None


def test_a_bundle_is_the_artists_and_says_so_or_holds_two_albums():
    assert releases.is_bundle(result("Linkin Park - Discography (2000-2017) [FLAC]"), LP)
    assert releases.is_bundle(result("Linkin Park - Hybrid Theory + Meteora [FLAC]"), LP)
    assert not releases.is_bundle(result("Linkin Park - Meteora [FLAC]"), LP)
    assert not releases.is_bundle(result("Pink Floyd - Discography [FLAC]"), LP)
    assert not releases.is_bundle(result("Linkin Park - Discography [DVD] 1080p"), LP)
    assert releases.is_bundle(result("Linkin Park Discography FLAC"), LP) and releases.is_bundle(result("Linkin Park 2000-2017 Discography"), LP)
    assert releases.is_bundle(result("Linkin Park   Papercuts (Singles Collection 2000–2023) (2024) [FLAC]"), LP)
    assert not releases.is_bundle(result("Hunter Hayes - Discography"), HUNTER) and not releases.is_bundle(result("Hunter x Hunter Collection"), HUNTER)
    assert not releases.is_bundle(result("Blizzard Hunter - Discography (10 releases) - 2014 - 2026, MP3, 320 kbps"), HUNTER)
    assert releases.is_bundle(result("Daron Malakian & Scars On Broadway - Discography"), {**LP, "names": ["Daron Malakian and Scars on Broadway"]})


def test_only_cached_bundles_with_wanted_albums_are_picked_most_albums_first():
    prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "aa"), result("Linkin Park - Discography [MP3]", "bb"),
                                                     result("Linkin Park - Complete [FLAC]", "cc", seeders=900), result("Linkin Park - Meteora", "dd")],
                         "Linkin Park": [result("Linkin Park - Discography [FLAC]", "aa"), result("Linkin Park - Box Set", "ee")]})
    listed = {"aa": DISCOGRAPHY, "bb": listing({"Hybrid Theory (2000)": ["01 Papercut.mp3", "02 One Step Closer.mp3", "03 In the End.mp3"]}),
              "ee": listing({"Live (2005)": ["01 Papercut.flac"]})}
    asked = []
    cached = lambda hashes: asked.append(hashes) or {h: listed[h] for h in hashes if h in listed}
    picks = releases.bundles(prowlarr, LP, LP_GROUPS, {"g-ht", "g-mtm"}, {"bb"}, cached, editions)
    assert [(r["infoHash"], sorted(t), holds) for r, t, holds in picks] == [("aa", ["g-ht", "g-mtm"], {"g-ht", "g-mtm"})]  # cc isn't cached; bb is blocked; ee holds nothing wanted
    assert prowlarr.asked == ["Linkin Park discography", "Linkin Park", "Linkin Park singles", "Linkin Park complete singles"]
    assert sorted(asked[0]) == ["aa", "cc", "ee"]  # dd isn't a bundle


def test_a_singles_collection_is_a_bundle_whose_singles_folders_are_targets():
    prowlarr = Prowlarr({"Linkin Park singles": [result("Linkin Park - Singles [FLAC]", "ss")]})
    listed = {"ss": listing({"2017 - One [Single]": ["01 One.flac"], "2003 - Meteora": ["01 Foreword.flac", "02 Don't Stay.flac"]})}
    picks = releases.bundles(prowlarr, LP, LP_GROUPS, {"g-one"}, set(), lambda hashes: {h: listed[h] for h in hashes if h in listed}, editions)
    assert [(r["infoHash"], t) for r, t, _ in picks] == [("ss", {"g-one": ["LP Discography/2017 - One [Single]/01 One.flac"]})]


@pytest.mark.parametrize("failure, blocked", [(LookupError("no edition matches"), False), (LossySource("lossy source in a FLAC"), True)])
def test_a_cached_bundle_fetches_only_its_wanted_albums_and_imports_each(downloads, torbox, db, tmp_path, monkeypatch, failure, blocked):
    """An album of it made from lossy files blocks the bundle: its next search finds another."""
    class Own:
        def place(self, folder, group):
            placed[group] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
            if group in failing:
                raise failure

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
    assert downloads.groups(db) == set() and downloads.scans == [1]
    assert bool(db.execute("select 1 from release_block where hash = 'abcd'").fetchone()) == blocked
    if blocked:
        return  # the rest searches the same release again

    # None of a bundle's albums imports: it fails and is blocked.
    failing.add("g-ht")
    downloads.prowlarr.results["Linkin Park discography"][0].update(infoHash="bcde", magnetUrl="magnet:?xt=urn:btih:bcde")
    torbox.cached_files = lambda hashes: {"bcde": DISCOGRAPHY}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht", "g-mtm"})
    torbox.list = [{**torbox.list[0], "id": 72, "hash": "bcde"}]
    downloads.cycle()
    assert db.execute("select state, error from download where hash = 'bcde'").fetchone()[:] == ("failed", "import failed: no album of the bundle matched")
    assert "bcde" in downloads.blocked(db)


def test_uncached_bundles_come_after_cached_ones_and_only_while_slots_are_free():
    prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [MP3]", "aa", seeders=50), result("Linkin Park - Discography [FLAC]", "bb"),
                                                     result("Linkin Park - Complete [FLAC]", "cc", seeders=0), result("Linkin Park - Box Set [FLAC]", "dd", seeders=90)]})
    cached = lambda hashes: {"aa": DISCOGRAPHY}
    found = lambda **more: [(r["infoHash"], t and sorted(t)) for r, t, _ in releases.bundles(prowlarr, LP, LP_GROUPS, {"g-ht"}, set(), cached, editions, **more)]
    assert found() == [("aa", ["g-ht"])]
    assert found(uncached=True) == [("aa", ["g-ht"]), ("dd", None), ("bb", None)]  # lossless, then seeders; cc has no seeder
    assert found(uncached=True, slow=frozenset({"dd"})) == [("aa", ["g-ht"]), ("bb", None), ("dd", None)]


def test_an_uncached_bundles_albums_are_found_once_torbox_lists_its_files(downloads, torbox, db, tmp_path, monkeypatch):
    placed, downloads.own.place = {}, lambda folder, group: placed.setdefault(group, sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()))
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {}
    db.executemany("insert into want_album(rg, artist_mbid, title, reason, state) values (?, 'mb-lp', ?, 'followed', 'missing')",
                   [("g-ht", "Hybrid Theory"), ("g-mtm", "Minutes to Midnight"), ("g-met", "Meteora")])
    downloads.crowded = True
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht", "g-mtm"}) is None  # slots all needed: cached bundles only
    downloads.crowded = False
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht", "g-mtm"})
    # Till TorBox lists its files, the albums its name says it holds count as downloading.
    assert db.execute("select state, bundle is not null from download").fetchone()[:] == ("added", 1) and downloads.groups(db) == {"g-ht", "g-mtm"}

    torbox.list = [tb(71, "abcd", progress=0.5)]  # TorBox lists no files while it downloads
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading" and downloads.groups(db) == {"g-ht", "g-mtm"}

    db.execute("update want_album set state = 'have' where rg = 'g-mtm'")  # arrived meanwhile
    torbox.list = [finished(71, "abcd", [f["name"] for f in DISCOGRAPHY], tmp_path)]
    downloads.cycle()
    assert db.execute("select state, bundle from download").fetchone()[:] == ("done", None)
    assert placed == {"g-ht": ["01. Papercut.flac", "02. One Step Closer.flac", "03. In the End.flac", "cover.jpg"]}

    # One that holds no wanted album fails and is blocked.
    db.execute("update want_album set state = 'have' where rg = 'g-ht'")
    downloads.prowlarr.results["Linkin Park discography"][0].update(infoHash="bcde", magnetUrl="magnet:?xt=urn:btih:bcde")
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met"})
    torbox.list = [finished(72, "bcde", ["LP/Meteora (2003)/01 Foreword.flac"], tmp_path)]  # a track short
    downloads.cycle()
    assert db.execute("select state, error from download where hash = 'bcde'").fetchone()[:] == ("failed", "the bundle holds no wanted album")
    assert "bcde" in downloads.blocked(db) and 72 in torbox.deleted
    assert db.execute("select artist_mbid, wanted from release_block where hash = 'bcde'").fetchone()[:] == ("mb-lp", '["g-met"]')
    assert "bcde" in downloads.blocked(db)  # Meteora is still wanted, but it was then too
    assert json.loads(db.execute("select wanted from release_block where hash = 'bcde'").fetchone()[0]) == ["g-met"]
    db.execute("update want_album set state = 'missing' where rg = 'g-mtm'")  # went missing again after the block: looked at again
    assert "bcde" not in downloads.blocked(db) and not db.execute("select 1 from release_block where hash = 'bcde'").fetchone()
    db.execute("insert into release_block(hash, reason, at) values ('cdef', ?, 0)", (download.EMPTY_BUNDLE,))  # an older oasis's: once
    assert "cdef" not in downloads.blocked(db)


def test_a_bundles_disc_image_arrives_with_its_cue_sheet_for_the_importer_to_split(downloads, torbox, db, tmp_path, monkeypatch):
    class Own:
        def place(self, folder, group):
            placed[group] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())

    placed, downloads.own = {}, Own()
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    names = [f"LP/Meteora (2003) [image+cue]/{n}" for n in ("Meteora.ape", "Meteora.cue", "Meteora.log")]
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {"abcd": [{"name": n, "size": 40e6} for n in names]}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met"})
    torbox.list = [finished(71, "abcd", names, tmp_path)]
    downloads.cycle()
    assert placed == {"g-met": ["Meteora.ape", "Meteora.cue"]} and db.execute("select state from download").fetchone()[0] == "done"


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, (str, bytes)):
        data = value.encode() if isinstance(value, str) else value
        return b"%d:%s" % (len(data), data)
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def dot_torrent(name: str, files: list[str]) -> bytes:
    return bencode({"announce": "http://tracker", "info": {"name": name, "piece length": 16384, "pieces": b"",
                                                           "files": [{"length": 7, "path": f.split("/")} for f in files]}})


def test_a_torrents_files_are_read_with_their_folders():
    data = dot_torrent("LP", ["Meteora (2003)/01 Foreword.flac", "cover.jpg"])
    assert download.torrent_files(data) == [{"name": "LP/Meteora (2003)/01 Foreword.flac", "size": 7}, {"name": "LP/cover.jpg", "size": 7}]
    assert download.torrent_files(bencode({"info": {"name": "Album.zip", "length": 9}})) == [{"name": "Album.zip", "size": 9}]
    assert download.torrent_files(b"<html>") == []


HUNTER = {"artist": "Hunter", "names": ["Hunter"], "native": [], "albums": ["Imperium", "Medeis", "Arachne", "Królestwo"]}
HUNTER_GROUPS = [{"id": "g-med", "title": "Medeis", "first-release-date": "2003"}, {"id": "g-imp", "title": "Imperium", "first-release-date": "2013"},
                 {"id": "g-ara", "title": "Arachne", "first-release-date": "2019"}]


def test_a_bundle_is_the_artists_only_when_their_name_opens_it():
    others = ["Ian Hunter (ex Mott the Hoople) - Discography 1975-2021 FLAC (Jamal The Moroccan",
              "Blizzard Hunter - Discography (10 releases) - 2014 - 2026, MP3, 320 kbps", "Tab Hunter - Discography (9 Releases) - 1958-2023, MP3, 320 kbps",
              "Bakuretsu Hunter OST I and Vocal Collection", "Demon Hunter - Discography - 2002-2025, (26 CD), FLAC (tracks+cue, tracks), lossless",
              "Alberta Hunter (with Lucille Hegamin, Victoria Spivey and Lovie Austin) - Discography (16 albums) - 1961-2000, MP3, 64-320 kbps",
              "Galaxy Hunter - Discography (17 Releases) - 2008-2022, MP3, 256-320 kbps", "Sonya Hunter - Discography 1991-2002 (6 releases), MP3, 192-320 kbps"]
    wanted = {g["id"] for g in HUNTER_GROUPS}
    for title in others:
        assert not releases.is_bundle(result(title), HUNTER) and releases.expected(result(title), HUNTER, HUNTER_GROUPS, wanted) == set(), title
    for title in ["Hunter (Poland) - Imperium + Medeis", "Hunter - Medeis + Imperium", "[FLAC] Hunter - Discography", "(Pol) Hunter - Discography",
                  "2003-2019 - Hunter - Discography"]:
        assert releases.is_bundle(result(title), HUNTER) and releases.expected(result(title), HUNTER, HUNTER_GROUPS, wanted), title
    assert releases.expected(result("Hunter (Poland) - Imperium + Medeis"), HUNTER, HUNTER_GROUPS, wanted) == {"g-imp", "g-med"}


def test_a_bundle_oasis_can_read_goes_before_every_magnet_link_and_a_magnet_link_needs_the_artists_name_alone():
    magnet = result("Linkin Park - Discography (1999-2017) [FLAC]", "aa", seeders=90, magnetUrl="http://prowlarr/1/download")  # likely all three
    torrent = result("Linkin Park - Box Set [MP3]", "bb", seeders=3, downloadUrl="http://prowlarr/2/download")
    read = {"bb": {"hash": "bb", "files": DISCOGRAPHY}}
    found = lambda *offered, want=LP: [(r["infoHash"], sorted(holds)) for r, _, holds in releases.bundles(
        Prowlarr({f"{want['artist']} discography": list(offered)}), want, LP_GROUPS, {"g-ht", "g-met", "g-mtm"}, set(), lambda hashes: {}, editions,
        uncached=True, torrent=lambda r: read.get(r["infoHash"]))]
    assert found(magnet, torrent) == [("bb", ["g-ht", "g-mtm"]), ("aa", ["g-ht", "g-met", "g-mtm"])]
    assert found(magnet) == [("aa", ["g-ht", "g-met", "g-mtm"])]  # nothing readable: the magnet link goes
    hunter = {**LP, "artist": "Hunter", "names": ["Hunter"]}
    others = [result("Hunter Tom (Hunter Thompson) - Discography [FLAC]", "cc", seeders=9, magnetUrl="http://prowlarr/3/download"),
              result("Hunter x Hunter (1999) Single Collection [2003]", "dd", seeders=9, magnetUrl="http://prowlarr/4/download")]
    assert found(*others, want=hunter) == []
    assert found(result("Hunter - Discography (1999-2017) [FLAC]", "ee", seeders=9, magnetUrl="http://prowlarr/5/download"), want=hunter)


def test_what_a_bundle_holds_is_guessed_from_its_name_when_no_file_list_tells():
    groups = [*LP_GROUPS, {"id": "g-nd", "title": "New Divide", "first-release-date": "2009", "primary-type": "Single"}]
    wanted = {"g-ht-ep", "g-met", "g-mtm", "g-nd"}
    guess = lambda title: releases.expected(result(title), LP, groups, wanted)
    assert guess("Linkin Park - Discography (2000-2005) [FLAC]") == {"g-met"}  # the years it spans; the EP is of 1999
    assert guess("Linkin Park - Discography (1999-2017) (40 Releases)") == wanted  # more releases than albums: singles too
    assert guess("Linkin Park - Singles Collection (2000-2010)") == {"g-nd"}
    assert guess("Linkin Park - Meteora + Minutes to Midnight") == {"g-met", "g-mtm"}
    assert guess("Mike Shinoda (from Linkin Park) - Discography") == guess("Linkin Park - Discography (Instrumentals)") == set()


def test_a_bundle_listed_without_its_hash_is_read_from_its_torrent_once_and_sent_with_it(downloads, torbox, db, monkeypatch):
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    data = dot_torrent("LP", ["Meteora (2003)/01 Foreword.flac", "Meteora (2003)/02 Don't Stay.flac", "One More Light (2017)/01 Heavy.flac"])
    read = []
    monkeypatch.setattr(download, "source", lambda release: read.append(release["title"]) or {"torrent": data})
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", seeders=9, downloadUrl="http://prowlarr/1/download")]})
    torbox.cached_files = lambda hashes: {}
    db.executemany("insert into want_album(rg, artist_mbid, title, reason, state) values (?, 'mb-lp', ?, 'followed', 'missing')", [("g-met", "Meteora"), ("g-oml", "One More Light")])
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met", "g-oml"})  # One More Light's folder holds a track of two
    (name, magnet, torrent), = torbox.added
    assert (magnet, torrent) == (None, data) and downloads.groups(db) == {"g-met"}  # from its files: what it holds for sure
    assert db.execute("select hash from download").fetchone()[0] == "file"
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-oml"}) is None and read == ["Linkin Park - Discography [FLAC]"] * 2  # read once, sent once
    # Its hash, once read, may be one TorBox has cached: then TorBox's list gives its targets.
    downloads.kept, read[:] = {}, []
    db.execute("delete from download")
    hash = download.info_hash({"torrent": data})
    torbox.cached_files = lambda hashes: {hash: [{"name": f["name"], "size": 1} for f in download.torrent_files(data)]} if hash in hashes else {}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met", "g-oml"})
    assert json.loads(db.execute("select files from download_target where rg = 'g-met'").fetchone()[0]) == ["LP/Meteora (2003)/01 Foreword.flac", "LP/Meteora (2003)/02 Don't Stay.flac"]


def test_bundles_combine_the_next_holding_the_most_of_what_the_others_dont(downloads, torbox, db, monkeypatch):
    """Each search sends one: the albums of those sent count as downloading, so the next search picks for the rest."""
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "aa", magnetUrl="magnet:?xt=urn:btih:aaaa"),
                                                               result("Linkin Park - Meteora + Hybrid Theory EP", "bb", magnetUrl="magnet:?xt=urn:btih:bbbb"),
                                                               result("Linkin Park - Complete [FLAC]", "cc", magnetUrl="magnet:?xt=urn:btih:cccc")]})
    listed = {"aa": DISCOGRAPHY, "bb": listing({"Meteora (2003)": ["01 Foreword.flac", "02 Don't Stay.flac"], "Hybrid Theory EP (1999)": ["01 Carousel.mp3", "02 Technique.mp3"]}),
              "cc": listing({"Meteora (2003) [FLAC]": ["01 Foreword.flac", "02 Don't Stay.flac"]})}
    torbox.cached_files = lambda hashes: {h: listed[h] for h in hashes if h in listed}
    missing = {"g-ht", "g-ht-ep", "g-mtm", "g-met", "g-oml", "g-one"}
    picks = lambda: downloads.bundle(db, LP, LP_GROUPS, missing - downloads.groups(db))
    assert picks() and downloads.groups(db) == {"g-ht", "g-mtm", "g-oml"}  # the discography: three
    assert picks() and downloads.groups(db) == missing - {"g-one"}  # then the one with both of the rest, not the lossless one with one
    assert picks() is None and [a[1][-4:] for a in torbox.added] == ["aaaa", "bbbb"]  # One is left to its own search


def test_an_uncached_bundle_needs_seeders_holds_its_albums_while_it_downloads_and_frees_them_when_dead(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    torbox.cached_files = lambda hashes: {}
    db.executemany("insert into want_album(rg, artist_mbid, title, reason, state) values (?, 'mb-lp', ?, 'followed', 'missing')", [("g-met", "Meteora"), ("g-mtm", "Minutes to Midnight")])
    db.execute("insert into bundle_search values ('mb-lp', ?)", (now[0],))
    offer = lambda seeders: [result("Linkin Park - Discography (2000-2010) [FLAC]", "abcd", seeders=seeders, magnetUrl="magnet:?xt=urn:btih:abcd")]
    downloads.prowlarr = Prowlarr({"Linkin Park discography": offer(releases.BUNDLE_SEEDERS - 1)})
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met", "g-mtm"}) is None and not torbox.added  # too few to fetch gigabytes from
    downloads.kept, downloads.prowlarr = {}, Prowlarr({"Linkin Park discography": offer(releases.BUNDLE_SEEDERS)})
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met", "g-mtm"}) and downloads.groups(db) == {"g-met", "g-mtm"}
    torbox.list = [tb(71, "abcd", progress=0, seeds=0, peers=0)]
    downloads.cycle()
    now[0] += download.PATIENT + 60
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "dead torrent: nobody shares it")
    assert torbox.deleted == [71] and "abcd" in downloads.blocked(db) and downloads.groups(db) == set()
    assert db.execute("select searched from bundle_search").fetchone()[0] == 0  # the artist's next bundle search is due


def zipped(path, files: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path.stat().st_size


def test_a_bundle_packed_in_one_zip_is_unpacked_here_and_its_wanted_albums_imported(downloads, torbox, db, tmp_path, monkeypatch):
    """TorBox lists a cached torrent of 100 files or more as one zip: what it holds is guessed from its name till unpacked."""
    placed = {}
    downloads.own.place = lambda folder, group: placed.setdefault(group, sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()))
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    db.executemany("insert into want_album(rg, artist_mbid, title, reason, state) values (?, 'mb-lp', ?, 'followed', 'missing')", [("g-met", "Meteora"), ("g-mtm", "Minutes to Midnight")])
    size = zipped(tmp_path / "cdn/0", {"LP/Meteora (2003)/01 Foreword.flac": "a", "LP/Meteora (2003)/02 Don't Stay.flac": "b", "LP/Meteora (2003)/cover.jpg": "c",
                                       "LP/Underground 7.0 (2007)/01 Wake.flac": "d", "LP/Underground 7.0 (2007)/02 Given Up.flac": "e",  # misnamed
                                       "LP/Underground 7.0 (2007)/03 Leave Out All the Rest.flac": "f", "LP/Hybrid Theory (2000)/01 Papercut.flac": "g"})
    listed = [{"id": 0, "name": "LP Discography/LP Discography.zip", "size": size}]
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {"abcd": listed}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met", "g-mtm"}) and downloads.groups(db) == {"g-met", "g-mtm"}
    torbox.list = [{**tb(71, "abcd", progress=1, active=False), "download_state": "completed", "download_finished": True, "download_present": True, "files": listed}]
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("done", None) and torbox.links == [0]
    assert placed == {"g-met": ["01 Foreword.flac", "02 Don't Stay.flac", "cover.jpg"],
                      "g-mtm": ["01 Wake.flac", "02 Given Up.flac", "03 Leave Out All the Rest.flac"]}  # by its songs
    assert not (tmp_path / "dl/abcd").exists()  # the rest of the copy goes too


def test_too_little_disk_space_for_a_packed_bundle_makes_it_wait_unblocked(downloads, torbox, db, tmp_path, monkeypatch):
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state) values ('g-met', 'mb-lp', 'Meteora', 'followed', 'missing')")
    size = zipped(tmp_path / "cdn/0", {"LP/Meteora (2003)/01 Foreword.flac": "a", "LP/Meteora (2003)/02 Don't Stay.flac": "b"})
    listed = [{"id": 0, "name": "LP/LP.zip", "size": size}]
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {"abcd": listed}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-met"})
    torbox.list = [{**tb(71, "abcd", progress=1, active=False), "download_state": "completed", "download_finished": True, "download_present": True, "files": listed}]
    free = [3 * size - 1]  # the zip and twice it to unpack
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=free[0]))
    downloads.cycle()
    row = db.execute("select state, error from download").fetchone()
    assert row[0] == "fetching" and row[1].startswith("too little disk space") and not torbox.links and not downloads.blocked(db)
    free[0] = 3 * size
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "done" and torbox.links == [0]


def test_another_album_holding_the_title_a_namesake_or_various_artists_are_not_the_album():
    korn = want("Korn", "Korn", year="1994", others=["Korn III: Remember Who You Are", "Issues"])
    assert reject(result("Korn - Korn III_ Remember Who You Are (2010) [FLAC] 88"), korn) == "another album: Korn III: Remember Who You Are"
    assert reject(result("Korn - Korn (1994) [FLAC]"), korn) is None
    name = "2021 (3 CD), FLAC / Various (Roxy Music, Lou Reed, New York Dolls) – Oh! You Pretty Things"
    assert reject(result(name), want("New York", "Lou Reed", year="1989")) == "various artists"
    ep = {**want("Hybrid Theory", "Linkin Park", year="1999"), "type": "EP", "namesakes": [{"type": "Album", "year": "2000"}]}
    album = {**want("Hybrid Theory", "Linkin Park", year="2000"), "type": "Album", "namesakes": [{"type": "EP", "year": "1999"}]}
    for name, ep_why, album_why in [
        ("Linkin Park - Hybrid Theory (2000) [FLAC]", "namesake: the Album of 2000", None),
        ("Linkin Park - Hybrid Theory [FLAC]", "namesake: the Album of 2000", None),
        ("Linkin Park - Hybrid Theory (1999) [FLAC]", None, "namesake: the EP of 1999"),
        ("Linkin Park - Hybrid Theory EP [FLAC]", None, "namesake: the EP"),
    ]:
        assert (reject(result(name), ep), reject(result(name), album)) == (ep_why, album_why), name
    single = {**want("St. Anger", "Metallica", year="2003"), "type": "Single", "namesakes": [{"type": "Album", "year": "2003"}]}
    assert reject(result("Metallica  St Anger (Full Album) (2003) (16 Bit 44 1 kHz FLAC)"), single) == "namesake: the Album of 2003"
    assert reject(result("Metallica - St. Anger (Single) 2003 FLAC"), single) is None


def test_an_accented_artist_is_searched_as_release_names_spell_it_first():
    assert queries(want("4:44", "JAŸ-Z")) == ["JAY-Z 4:44", "JAŸ-Z 4:44"]
    assert queries({**want("Homogenic", "Björk"), "other_titles": ["Homogénic"]}) == ["Bjork Homogenic", "Björk Homogenic", "Homogenic", "Bjork Homogénic"]
    assert queries(want("Case", "Creepy Nuts"))[0] == "Creepy Nuts Case"
    assert queries(want("ID", "Димаш"))[0] == "Димаш ID"  # Cyrillic stays: й isn't и
    assert queries(want("The Blueprint²: The Gift & The Curse", "JAŸ-Z"))[0] == "JAY-Z The Blueprint 2: The Gift & The Curse"
    assert releases.bundle_queries({"artist": "JAŸ-Z", "native": []})[:2] == ["JAY-Z discography", "JAY-Z"]


def test_slashes_leave_queries():
    assert queries(want("Avid / Hands Up to the Sky", "SawanoHiroyuki[nZk]"))[0] == "SawanoHiroyuki nZk Avid Hands Up to the Sky"


@pytest.mark.parametrize("name, why", [
    ("Rammstein - Mein Herz brennt (2012) [FLAC]", None),
    ("Torsten Rasch - Mein Herz Brennt (Based on Music of Rammstein) - 2003, FLAC", "another artist's: based on music of"),
    ("Some Quartet plays Rammstein - Mein Herz brennt [FLAC]", "another artist's: plays"),
    ("Mein Herz brennt - in the style of Rammstein [FLAC]", "another artist's: in the style of"),
    ("Rammstein - Mein Herz brennt (Songs of Rammstein) [FLAC]", None),  # the artist is credited before the phrase
])
def test_an_artist_named_only_as_the_source_is_not_the_albums_artist(name, why):
    assert reject(result(name), want("Mein Herz brennt", "Rammstein", year="2012")) == why


def test_other_peoples_mixtapes_of_an_album_are_not_it():
    gangster = want("American Gangster", "JAŸ-Z", "Jay-Z", year="2007")
    assert reject(result("Jay-Z & Teknology - Blu Magik (The American Gangster Re-Mix-Tape)"), gangster) == "variant: mixtape"
    assert reject(result("Funk Daddy & Mistah Clark Present Jay-Z - West Coast American Gangster"), gangster) == "another artist's: present"
    assert reject(result("Jay-Z - American Gangster (2007) [FLAC]"), gangster) is None


def test_music_videos_and_vocal_free_tracks_are_not_the_single():
    assert reject(result("Metallica - Whiskey In The Jar.MPG (uncensored)"), want("Whiskey in the Jar", "Metallica")) == "video"
    assert reject(result("Foo Fighters  Everlong (No Vocal Original Track) flac"), want("Everlong", "Foo Fighters")) == "variant: novocal"


def test_a_bundle_imported_is_remembered_and_later_albums_come_from_it_while_torbox_has_it_cached(downloads, torbox, db, tmp_path, monkeypatch):
    placed = {}
    downloads.own.place = lambda folder, group: placed.setdefault(group, sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()))
    monkeypatch.setattr(download, "editions", lambda db, rg: editions(rg))
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [FLAC]", "abcd", magnetUrl="magnet:?xt=urn:btih:abcd")]})
    torbox.cached_files = lambda hashes: {"abcd": DISCOGRAPHY} if "abcd" in hashes else {}
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state) values ('g-ht', 'mb-lp', 'Hybrid Theory', 'followed', 'missing')")
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht"})
    torbox.list = [finished(71, "abcd", [f["name"] for f in DISCOGRAPHY], tmp_path)]
    downloads.cycle()
    [t.join(10) for t in threading.enumerate() if t.name != "MainThread" and "map_kept" in repr(getattr(t, "_target", None))]
    assert set(placed) == {"g-ht"} and torbox.deleted == [71]
    kept = db.execute("select * from bundle_kept").fetchone()
    assert (kept["hash"], kept["artist_mbid"], len(json.loads(kept["files"]))) == ("abcd", "mb-lp", len(DISCOGRAPHY))
    assert sorted(json.loads(kept["groups"])) == ["g-ht", "g-mtm", "g-oml"]  # all its folders hold, wanted or not

    # An album starred since: from the remembered bundle, its files only, before any search. TorBox's cache check
    # needn't list its files (it lists one of 100 or more as one zip): the remembered paths are fetched.
    torbox.cached = lambda hashes: {"abcd"} & set(hashes)
    asked = len(downloads.prowlarr.asked)
    assert downloads.find(db, want("One More Light", "Linkin Park"), "g-oml")
    assert torbox.added[-1][1] == "magnet:?xt=urn:btih:abcd" and len(downloads.prowlarr.asked) == asked
    row = db.execute("select * from download where hash = 'abcd'").fetchone()
    assert (row["state"], row["kept"]) == ("added", 1)
    assert [tuple(r) for r in db.execute("select rg, files from download_target where hash = 'abcd'")] == [("g-oml", json.dumps(
        ["LP Discography/One More Light (2017)/01 Nobody Can Save Me.flac", "LP Discography/One More Light (2017)/02 Heavy.flac"]))]
    torbox.list = [finished(72, "abcd", [f["name"] for f in DISCOGRAPHY], tmp_path)]
    downloads.cycle()
    assert placed["g-oml"] == ["01 Nobody Can Save Me.flac", "02 Heavy.flac"] and db.execute("select count(*) from bundle_kept").fetchone()[0] == 1

    # An album it was tried for already isn't fetched from it again: searched as usual.
    db.execute("update download set state = 'done' where hash = 'abcd'")
    added, asked = len(torbox.added), len(downloads.prowlarr.asked)
    assert downloads.find(db, want("Hybrid Theory", "Linkin Park"), "g-ht") is None
    assert len(torbox.added) == added and len(downloads.prowlarr.asked) > asked
    assert json.loads(db.execute("select tried from bundle_kept").fetchone()[0]) == ["g-ht", "g-oml"]
    # TorBox no longer has it: it is forgotten and the album searched as usual.
    torbox.cached = torbox.cached_files = lambda hashes: {}
    downloads.find(db, want("Minutes to Midnight", "Linkin Park"), "g-mtm")
    assert len(downloads.prowlarr.asked) > asked and db.execute("select count(*) from bundle_kept").fetchone()[0] == 0


def test_an_empty_bundle_stays_blocked_when_an_album_downloading_elsewhere_then_comes_back_missing(downloads, db):
    db.executemany("insert into want_album(rg, artist_mbid, title, reason, state) values (?, 'mb-lp', ?, 'followed', ?)",
                   [("g-ht", "Hybrid Theory", "missing"), ("g-mtm", "Minutes to Midnight", "downloading"), ("g-met", "Meteora", "have")])
    downloads.block_release(db, {"hash": "flac", "guid": None}, download.EMPTY_BUNDLE, ["g-ht"])  # looked at for Hybrid Theory only
    db.execute("update want_album set state = 'missing' where rg = 'g-mtm'")  # its other download's import failed
    assert "flac" in downloads.blocked(db)
    db.execute("update want_album set state = 'missing' where rg = 'g-met'")  # wanted anew
    assert "flac" not in downloads.blocked(db)


def test_a_bundle_imported_before_isnt_sent_again_by_a_bundle_search(downloads, torbox, db):
    db.execute("insert into download(hash, album_id, title, state, created, updated) values ('aa', 0, 'Linkin Park - Discography [MP3]', 'done', 0, 0)")
    db.execute("insert into download_target(hash, rg, files, state) values ('aa', 'g-mtm', '[]', 'failed')")
    downloads.prowlarr = Prowlarr({"Linkin Park discography": [result("Linkin Park - Discography [MP3]", "aa", seeders=50)]})
    torbox.cached_files = lambda hashes: {"aa": DISCOGRAPHY}
    assert downloads.bundle(db, LP, LP_GROUPS, {"g-ht"}) is None and not torbox.added
