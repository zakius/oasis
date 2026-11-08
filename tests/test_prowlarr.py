import json
from urllib.parse import parse_qs

import pytest

from oasis.config import Prowlarr as Settings
from oasis.prowlarr import Prowlarr


def indexer(id, *categories, tags=(2,), enable=True):
    return {"id": id, "enable": enable, "tags": list(tags), "protocol": "torrent",
            "capabilities": {"categories": [{"id": c, "name": n} for c, n in categories]}}


WITH_AUDIO = indexer(32, (2000, "Movies"), (3000, "Audio"), (100044, "Зарубежная музыка"), (8000, "Other"))
WITHOUT_AUDIO = indexer(35, (2000, "Movies"), (100001, "Movies"), (5000, "TV"), (100002, "TV"), (8000, "Other"), (100003, "Other"))


def test_search_asks_each_indexer_in_its_own_categories(monkeypatch):
    asked = {}

    def call(self, path):
        if path == "indexerstatus":
            return [{"indexerId": 9, "disabledTill": "9999"}]
        if path == "indexer":
            return [WITH_AUDIO, WITHOUT_AUDIO, indexer(1, (100009, "Сериалы"), (100010, "Музыка")), indexer(7, (3000, "Audio"), tags=(1,)),
                    indexer(9, (3000, "Audio"))]
        q = parse_qs(path.split("?", 1)[1])
        asked[int(q["indexerIds"][0])] = [int(c) for c in q["categories"]]
        return [{"title": "x", "protocol": "torrent"}]
    monkeypatch.setattr(Prowlarr, "call", call)
    assert len(Prowlarr(Settings("http://p", "k")).search("Linkin Park")) == 3
    assert asked == {32: [3000], 35: [8000, 100003], 1: [100010]}  # anime's tag and a paused indexer stay out


def test_a_search_with_every_indexer_paused_or_disabled_fails(monkeypatch):
    monkeypatch.setattr(Prowlarr, "call", lambda self, path: [{"indexerId": 32, "disabledTill": "9999"}] if path == "indexerstatus" else
                        [WITH_AUDIO, indexer(35, (3000, "Audio"), enable=False)] if path == "indexer" else [{"title": "x", "protocol": "torrent"}])
    with pytest.raises(OSError, match="no music indexer"):
        Prowlarr(Settings("http://p", "k")).search("Linkin Park")


def test_results_name_their_indexer_and_only_login_trackers_count_as_logins(monkeypatch):
    found = [{**indexer(40, (3000, "Audio")), "name": "Tracker One", "privacy": "semiPrivate"},
             {**indexer(41, (3000, "Audio")), "name": "Tracker Two", "privacy": "private"}, {**WITH_AUDIO, "name": "Open Index", "privacy": "public"},
             {**indexer(36, (3000, "Audio")), "name": "Local DHT", "privacy": "private", "indexerUrls": ["http://dht:3333/torznab"]},
             {**indexer(39, (3000, "Audio")), "name": "LAN", "privacy": "private", "indexerUrls": ["http://192.168.1.5:9117/"]}]
    monkeypatch.setattr(Prowlarr, "call", lambda self, path: [] if path == "indexerstatus" else found if path == "indexer" else [])
    p = Prowlarr(Settings("http://p", "k"))
    p.indexers()
    assert [p.tracker({"indexerId": i}) for i in (40, 41, 32, 36, 39)] == ["Tracker One", "Tracker Two", "Open Index", "Local DHT", "LAN"]
    assert sorted(p.logins.values()) == ["Tracker One", "Tracker Two"]  # a local index needs no login, whatever Prowlarr calls it


def test_a_failed_write_reports_prowlarrs_messages_but_never_the_values_it_echoes(monkeypatch, caplog):
    import io
    import urllib.error
    import urllib.request
    answer = json.dumps([{"propertyName": "cookie", "errorMessage": "Unable to connect to indexer", "attemptedValue": "uid=1; pass=topsecret"},
                         {"propertyName": "", "errorMessage": "Unable to connect to indexer", "attemptedValue": "uid=1; pass=topsecret"}]).encode()
    sent = []

    def urlopen(request, timeout):
        sent.append((request.get_method(), request.full_url, json.loads(request.data)))
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(answer))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    p = Prowlarr(Settings("http://p", "k"))
    with pytest.raises(OSError) as e:
        p.save({"id": 9, "name": "Public-Index", "fields": [{"name": "cookie", "value": "uid=1; pass=topsecret"}]}, force=True)
    assert str(e.value) == "Prowlarr: Unable to connect to indexer" and "topsecret" not in caplog.text
    assert sent[0][:2] == ("PUT", "http://p/api/v1/indexer/9?forceSave=true")
    with pytest.raises(OSError, match="Unable to connect"):
        p.test({"id": 9})
    assert sent[1][:2] == ("POST", "http://p/api/v1/indexer/test")
