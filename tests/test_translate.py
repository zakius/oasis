import io
import json

import pytest

from conftest import call, make_client, make_track
from oasis import translate


class FakeTranslator:
    def __init__(self, source="ja", fail=False):
        self.asked, self.source, self.fail = [], source, fail

    def translate(self, lines, lang):
        self.asked.append((lines, lang))
        if self.fail:
            raise OSError("HTTP 429")
        return [f"{lang}: {line}" for line in lines], self.source


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "lib"
    make_track(root / "a/1.flac", title="夜に駆ける", artist="YOASOBI", album="THE BOOK")
    (root / "a/1.lrc").write_text("[00:01.00]沈むように\n[00:05.00]溶けてゆくように\n")
    make_track(root / "b/1.flac", title="Faint", artist="Linkin Park", album="Meteora")
    (root / "b/1.lrc").write_text("[00:01.00]I am a little bit of loneliness\n[00:01.00]我是一点点孤独\n")
    with make_client(root, tmp_path / "oasis.db") as c:
        c.app.state.oasis.translations.translator = FakeTranslator()
        yield c


def song(client, title):
    return next(s["id"] for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == title)


def lyrics(client, title="夜に駆ける", **params):
    return call(client, "getLyricsBySongId", id=song(client, title), **params)["lyricsList"].get("structuredLyrics", [])


def test_lyrics_are_translated_only_when_an_app_asks(client):
    fake = client.app.state.oasis.translations.translator
    assert [e["kind"] for e in lyrics(client, enhanced="true")] == ["main", "pronunciation"]
    assert [e["kind"] for e in lyrics(client, enhanced="true", oasisTranslate="en")] == ["main", "pronunciation"]  # without oasis=1
    assert len(lyrics(client, oasis="1", oasisTranslate="en")) == 1  # songLyrics v1: main only
    assert fake.asked == []
    translation = lyrics(client, enhanced="true", oasis="1", oasisTranslate="en")[2]
    assert translation == {**translation, "kind": "translation", "lang": "en", "oasisMachineTranslation": True}
    assert translation["line"] == [{"start": 1000, "value": "en: 沈むように"}, {"start": 5000, "value": "en: 溶けてゆくように"}]
    assert lyrics(client, enhanced="true", oasis="1", oasisTranslate="en", kinds="translation") == [translation]  # kept
    assert fake.asked == [(["沈むように", "溶けてゆくように"], "en")]
    assert call(client, "getLyricsBySongId", id=song(client, "Faint"), enhanced="true", oasis="1", oasisTranslate="english")["error"]["code"] == 0


def test_oasis_translate_1_uses_the_dashboards_language(client):
    fake = client.app.state.oasis.translations.translator
    assert len(lyrics(client, enhanced="true", oasis="1", oasisTranslate="1")) == 2 and fake.asked == []  # none chosen
    client.post("/login", data={"name": "admin", "password": "secret"})
    assert "Lyrics translation" in client.get("/").text
    client.post("/account/translation", data={"lang": "pl"})
    assert call(client, "oasisTranslation")["oasisTranslation"]["language"] == "pl"
    assert lyrics(client, enhanced="true", oasis="1", oasisTranslate="1")[2]["lang"] == "pl"


def test_a_translation_that_comes_with_the_lyrics_wins(client):
    fake = client.app.state.oasis.translations.translator
    fake.source = "en"
    assert [(e["kind"], e.get("lang")) for e in lyrics(client, "Faint", enhanced="true", oasis="1", oasisTranslate="zh-CN")] == [("main", "xxx"), ("translation", "zh")]
    assert fake.asked == []
    found = lyrics(client, "Faint", enhanced="true", oasis="1", oasisTranslate="pl")
    assert [e.get("lang") for e in found] == ["xxx", "zh", "pl"] and found[2]["line"][0]["value"] == "pl: I am a little bit of loneliness"


def test_lyrics_in_the_language_asked_get_no_translation(client):
    client.app.state.oasis.translations.translator.source = "ja"
    assert len(lyrics(client, enhanced="true", oasis="1", oasisTranslate="ja")) == 2
    assert len(lyrics(client, enhanced="true", oasis="1", oasisTranslate="ja")) == 2 and len(client.app.state.oasis.translations.translator.asked) == 1  # kept


def test_a_failing_translator_leaves_the_lyrics_and_rests(client):
    translations = client.app.state.oasis.translations
    translations.translator = FakeTranslator(fail=True)
    assert [e["kind"] for e in lyrics(client, enhanced="true", oasis="1", oasisTranslate="en")] == ["main", "pronunciation"]
    lyrics(client, "Faint", enhanced="true", oasis="1", oasisTranslate="en")
    assert len(translations.translator.asked) == 1  # resting after the failure


def test_the_server_says_translation_is_there(client):
    assert {"name": "oasis", "versions": [1, 2, 3]} in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]
    found = call(client, "oasisTranslation")["oasisTranslation"]
    assert found["available"] is True and "language" not in found and {"id": "zh-CN", "name": "Chinese (Simplified)"} in found["languages"]


def test_google_keeps_lines_one_for_one(monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append((request.full_url, request.data))
        return io.BytesIO(json.dumps([[["Sinking\n", "沈む\n"], ["Melting", "溶ける"]], None, "ja"]).encode())
    monkeypatch.setattr(translate.urllib.request, "urlopen", urlopen)
    assert translate.Google().translate(["沈む", "", "溶ける"], "en") == (["Sinking", "", "Melting"], "ja")
    assert sent[0][0].endswith("tl=en") and sent[0][1] == b"q=%E6%B2%88%E3%82%80%0A%E6%BA%B6%E3%81%91%E3%82%8B"  # one request, blank lines out
    with pytest.raises(ValueError):
        translate.Google().translate(["沈む", "溶ける", "夜"], "en")
