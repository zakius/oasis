import pytest

from conftest import call, make_client, make_track
from oasis import latin, musicbrainz
from oasis.enrich import Enrichment

YONEZU = {"id": "mb-yonezu", "name": "米津玄師", "sort-name": "Yonezu, Kenshi", "type": "Person",
          "aliases": [{"name": "Kenshi Yonezu", "locale": "en", "primary": True}, {"name": "米津玄師", "locale": "ja", "primary": True}]}


def test_an_artists_latin_name_is_its_ja_latn_alias_else_its_sort_name_in_its_own_order():
    assert latin.artist_name(YONEZU) == "Yonezu Kenshi"  # not the English alias, which turns the name around
    assert latin.artist_name({**YONEZU, "aliases": [*YONEZU["aliases"], {"name": "Yonedzu Kenshi", "locale": "ja-Latn"}]}) == "Yonedzu Kenshi"
    assert latin.artist_name({**YONEZU, "type": "Character"}) is None
    assert latin.artist_name({**YONEZU, "sort-name": "米津, 玄師"}) is None  # its sort name isn't Latin: generated


def credit(*artists):
    """artists: (name, mbid) pairs, joined by ' + '."""
    return [{"name": name, "joinphrase": " + " if i < len(artists) - 1 else "", "artist": {"id": mbid, "name": name}}
            for i, (name, mbid) in enumerate(artists)]


def release(id, title, language, *tracks, rel=None):
    """tracks: (title, artist credit) pairs. rel: the releases this one links as its transliterations; without them
    it is a Latin-script pseudo-release."""
    return {"id": id, "title": title, "status": "Pseudo-Release" if rel is None else "Official",
            "text-representation": {"language": language, "script": "Latn" if rel is None else "Jpan"},
            "media": [{"position": 1, "tracks": [{"id": f"{id}-t{i}", "position": i, "title": t, "artist-credit": c, "recording": {"id": f"{id}-r{i}"}}
                                                 for i, (t, c) in enumerate(tracks, 1)]}],
            "relations": [{"type": "transl-tracklisting", "direction": "forward", "release": {"id": r}} for r in rel or []]}


@pytest.fixture
def musicbrainz_knows(monkeypatch):
    """Serves MusicBrainz's answers from a dict by 'artist/<mbid>' or 'release/<mbid>'; returns the paths asked."""
    asked = []

    def serve(answers):
        monkeypatch.setattr(musicbrainz, "get", lambda url, db=None, keep=0: asked.append(url) or answers[url.removeprefix(musicbrainz.MB).split("?")[0]])
        return asked
    return serve


def fill(client):
    enrichment = Enrichment(client.app.state.oasis.connect)
    while enrichment.step():
        pass


def songs(client, query="", **params):
    return {s["title"]: s for s in call(client, "search3", query=query, **params)["searchResult3"]["song"]}


def test_a_releases_transliteration_names_its_album_and_tracks_but_a_translation_doesnt(tmp_path, musicbrainz_knows):
    root = tmp_path / "m"
    for n, title in enumerate(["眠れ", "東京"], 1):
        make_track(root / f"z/{n}.flac", title=title, artist="米津玄師", album="眠れ", tracknumber=str(n),
                   musicbrainz_albumid="rel", musicbrainz_releasetrackid=f"rel-t{n}")
    yonezu = credit(("米津玄師", "mb-yonezu"))
    asked = musicbrainz_knows({
        "artist/mb-yonezu": YONEZU,
        "release/rel": release("rel", "眠れ", "jpn", ("眠れ", yonezu), ("東京", yonezu), rel=["sleep", "nemure"]),
        "release/sleep": release("sleep", "Sleep", "eng", ("Sleep", []), ("The Capital", [])),  # a translation, tagged English
        "release/nemure": release("nemure", "Nemure", "eng", ("Nemure", []), ("Tokio", []))})  # a transliteration, tagged English too
    with make_client(root, tmp_path / "oasis.db") as client:
        assert songs(client, romanization="both")["東京"]["titleLatin"] == "Tōkyō"  # generated, and searched by, before MusicBrainz is read
        fill(client)
        found = songs(client, romanization="both")
        assert (found["東京"]["titleLatin"], found["眠れ"]["titleLatin"], found["眠れ"]["albumLatin"]) == ("Tokio", "Nemure", "Nemure")
        assert list(songs(client, "tokio")) == ["東京"]  # search reads the songs' romanized forms again
        read = len(asked)
        fill(client)
        assert len(asked) == read  # read once, till REFRESH


def test_musicbrainz_names_come_before_generated_ones_in_names_and_search(tmp_path, musicbrainz_knows):
    root = tmp_path / "m"
    make_track(root / "k/1.flac", title="感電", artist="米津玄師 + アイナ・ジ・エンド", album="STRAY SHEEP", tracknumber="1",
               musicbrainz_albumid="k", musicbrainz_releasetrackid="k-t1")
    make_track(root / "k/2.flac", title="馬と鹿", artist="米津玄師", album="STRAY SHEEP", tracknumber="2",
               musicbrainz_albumid="k", musicbrainz_releasetrackid="k-t2")
    yonezu = ("米津玄師", "mb-yonezu")
    musicbrainz_knows({
        "artist/mb-yonezu": {**YONEZU, "aliases": [{"name": "Yonedzu Kenshi", "locale": "ja-Latn"}]},
        "artist/mb-aina": {"id": "mb-aina", "name": "アイナ・ジ・エンド", "sort-name": "アイナ・ジ・エンド", "type": "Person"},
        "release/k": release("k", "STRAY SHEEP", "jpn", ("感電", credit(yonezu, ("アイナ・ジ・エンド", "mb-aina"))), ("馬と鹿", credit(yonezu)), rel=["k-latn"]),
        "release/k-latn": release("k-latn", "STRAY SHEEP", "jpn", ("Kanden MB", []))})  # names the first track only
    with make_client(root, tmp_path / "oasis.db") as client:
        fill(client)
        found = songs(client, romanization="both")
        assert found["感電"]["titleLatin"] == "Kanden MB" and found["馬と鹿"]["artistLatin"] == "Yonedzu Kenshi"
        assert found["感電"]["artistLatin"].startswith("Yonedzu Kenshi + ")  # the other name generated
        assert found["馬と鹿"]["titleLatin"] == "Uma to Shika"  # none from MusicBrainz: generated
        assert list(songs(client, "kanden mb")) == ["感電"]
