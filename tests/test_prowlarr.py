from urllib.parse import parse_qs

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
