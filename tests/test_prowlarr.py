from urllib.parse import parse_qs

import pytest

from oasis.prowlarr import Prowlarr, music_categories


def indexer(id, *categories, tags=(2,), enable=True):
    return {"id": id, "enable": enable, "tags": list(tags), "protocol": "torrent",
            "capabilities": {"categories": [{"id": c, "name": n} for c, n in categories]}}


BIGFAN = indexer(32, (2000, "Movies"), (3000, "Audio"), (100044, "Зарубежная музыка"), (8000, "Other"))
RUTOR = indexer(35, (2000, "Movies"), (100001, "Movies"), (5000, "TV"), (100002, "TV"), (8000, "Other"), (100003, "Other"))


def test_an_indexer_without_audio_is_searched_in_its_other_categories_but_video():
    assert music_categories(BIGFAN) == [3000]
    assert music_categories(RUTOR) == [8000, 100003]
    assert music_categories(indexer(1, (100009, "Сериалы"), (100010, "Музыка"))) == [100010]


def test_search_asks_each_indexer_in_its_own_categories(monkeypatch):
    asked = {}

    def call(self, path):
        if path == "indexerstatus":
            return [{"indexerId": 9, "disabledTill": "9999"}]
        if path == "indexer":
            return [BIGFAN, RUTOR, indexer(7, (3000, "Audio"), tags=(1,)), indexer(9, (3000, "Audio"))]
        q = parse_qs(path.split("?", 1)[1])
        asked[int(q["indexerIds"][0])] = [int(c) for c in q["categories"]]
        return [{"title": "x", "protocol": "torrent"}]
    monkeypatch.setattr(Prowlarr, "call", call)
    assert len(Prowlarr("http://p", "k").search("Linkin Park")) == 2
    assert asked == {32: [3000], 35: [8000, 100003]}  # anime's tag and a paused indexer stay out


def test_a_search_with_every_indexer_paused_or_disabled_fails(monkeypatch):
    monkeypatch.setattr(Prowlarr, "call", lambda self, path: [{"indexerId": 32, "disabledTill": "9999"}] if path == "indexerstatus" else
                        [BIGFAN, indexer(35, (3000, "Audio"), enable=False)] if path == "indexer" else [{"title": "x", "protocol": "torrent"}])
    with pytest.raises(OSError, match="no music indexer"):
        Prowlarr("http://p", "k").search("Linkin Park")


def test_results_from_private_and_semi_private_indexers_name_their_tracker(monkeypatch):
    found = [{**indexer(40, (3000, "Audio")), "name": "Tracker-A", "privacy": "semiPrivate"},
             {**indexer(41, (3000, "Audio")), "name": "Tracker-D", "privacy": "private"}, {**BIGFAN, "name": "Open-Index-2", "privacy": "public"},
             {**indexer(36, (3000, "Audio")), "name": "Local-DHT", "privacy": "private", "indexerUrls": ["http://local-dht:3333/torznab"]},
             {**indexer(39, (3000, "Audio")), "name": "LAN", "privacy": "private", "indexerUrls": ["http://192.168.1.5:9117/"]}]
    monkeypatch.setattr(Prowlarr, "call", lambda self, path: [] if path == "indexerstatus" else found if path == "indexer" else [])
    p = Prowlarr("http://p", "k")
    p.indexers()
    assert [p.tracker({"indexerId": i}) for i in (40, 41, 32, 36, 39)] == ["Tracker-A", "Tracker-D", None, None, None]  # a local index: no login, no seeding
