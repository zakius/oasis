import base64
import json
import zlib

import pytest

from oasis import lyrics
from oasis.lyrics import KUGOU_KEY, Lyrics, Query, choose, parse, same_title, similarity

QUERY = Query("Song", "Artist", "Album", 200)
TEXT = "the first line of it\nanother line here\nand the last one"


def lines(text, start=0):
    return [(start + i * 1000, t) for i, t in enumerate(text.splitlines())]


def test_parse_lrc_with_word_stamps_and_headers():
    found = parse("[ti:x]\n[00:01.00]<00:01.00>hel<00:01.50>lo\n[00:00.50]first")
    assert found.synced and found.word_level and found.lines == [(500, "first"), (1000, "hello")]
    plain = parse("one\ntwo\n\n")
    assert not plain.synced and plain.lines == [(None, "one"), (None, "two")]


def test_same_title_ignores_extras():
    assert same_title("Shinunoga E-Wa (TV Size)", "shinunoga e-wa") and same_title("Café", "cafe")
    assert not same_title("One", "Two")


def test_choose_follows_roma_rules():
    ytm_synced = Lyrics("YouTube Music", lines(TEXT), True)
    ytm_plain = Lyrics("YouTube Music", [(None, t) for t in TEXT.splitlines()], False)
    word = Lyrics("NetEase", lines(TEXT), True, True)
    other_word = Lyrics("KuGou", lines("完全に違う歌詞です\nまったく別の曲"), True, True)
    synced = Lyrics("LRCLIB", lines(TEXT), True)
    assert choose(ytm_synced, [other_word, word]) is word                 # 1: word-timed matching YouTube Music
    assert choose(ytm_synced, [other_word, synced]) is ytm_synced         # 2: YouTube Music line-timed
    assert choose(ytm_plain, [synced]) is synced                          # 3: other synced that matches
    assert choose(ytm_plain, [other_word]) is ytm_plain                   # 4: YouTube Music plain
    assert choose(None, [synced, other_word]) is other_word               # 1 without a reference
    assert choose(None, []) is None
    assert similarity(TEXT, TEXT.upper()) == 1.0


def respond(monkeypatch, pages: dict):
    def get(url, headers=None, data=None):
        return next(body for prefix, body in pages.items() if url.startswith(prefix))
    monkeypatch.setattr(lyrics, "get", get)


def test_lrclib_prefers_synced_near_length(monkeypatch):
    respond(monkeypatch, {"https://lrclib.net": json.dumps([
        {"trackName": "Song", "duration": 300, "syncedLyrics": "[00:01.00]wrong length"},
        {"trackName": "Song", "duration": 201, "syncedLyrics": "[00:01.00]right"},
        {"trackName": "Other", "duration": 200, "syncedLyrics": "[00:01.00]other song"}]).encode()})
    assert lyrics.lrclib(QUERY).lines == [(1000, "right")]


def test_netease_word_timing(monkeypatch):
    respond(monkeypatch, {
        "https://music.163.com/api/cloudsearch": json.dumps({"result": {"songs": [{"id": 7, "name": "Song", "dt": 200500}]}}).encode(),
        "https://music.163.com/api/song/lyric": json.dumps({"yrc": {"lyric": "[1000,2000](1000,500,0)Hel(1500,500,0)lo (world)"}}).encode()})
    found = lyrics.netease(QUERY)
    assert found.word_level and found.lines == [(1000, "Hello (world)")]


def test_kugou_decrypts_krc(monkeypatch):
    krc = "[1000,2000]<0,500,0>Hi<500,500,0> there".encode()
    packed = zlib.compress(krc)
    encrypted = base64.b64encode(b"krc1" + bytes(b ^ KUGOU_KEY[i % 16] for i, b in enumerate(packed))).decode()
    respond(monkeypatch, {
        "https://mobileservice.kugou.com": json.dumps({"data": {"info": [{"songname": "Song", "duration": 200, "hash": "h"}]}}).encode(),
        "https://krcs.kugou.com/search": json.dumps({"candidates": [{"id": 1, "accesskey": "k"}]}).encode(),
        "https://krcs.kugou.com/download": json.dumps({"content": encrypted}).encode()})
    assert lyrics.kugou(QUERY).lines == [(1000, "Hi there")]


def test_lyricsplus_rejects_timing_that_does_not_fit(monkeypatch):
    def body(end):
        return json.dumps({"metadata": {"source": "Apple"}, "lyrics": [
            {"time": 1000, "duration": end - 1000, "syllabus": [{"text": "Hey"}]}]}).encode()
    respond(monkeypatch, {"https://lyricsplus": body(190_000)})
    found = lyrics.lyricsplus(QUERY)
    assert found.source == "LyricsPlus · Apple" and found.word_level
    respond(monkeypatch, {"https://lyricsplus": body(30_000)})
    assert lyrics.lyricsplus(QUERY) is None


def test_find_survives_failing_providers(monkeypatch):
    def broken(query):
        raise OSError("down")
    monkeypatch.setattr(lyrics, "PROVIDERS", {"broken": broken, "ok": lambda query: Lyrics("LRCLIB", lines(TEXT), True)})
    assert lyrics.find(QUERY).source == "LRCLIB"
    assert lyrics.find(QUERY, youtube=lambda: Lyrics("YouTube Music", lines(TEXT), True)).source == "YouTube Music"


def test_cleaning_drops_credits_and_heading_and_unescapes():
    raw = Lyrics("KuGou", [(0, "Artist - Song"), (1, "作词：someone"), (2, "Composer: X"), (3, "don&apos;t stop"), (4, "it\\'s fine")], True)
    assert lyrics.cleaned(raw, QUERY).lines == [(3, "don't stop"), (4, "it's fine")]


def test_lrc_offset_is_applied_positive_sooner():
    assert lyrics.parse("[offset:+500]\n[00:01.00]a\n[00:00.20]b").lines == [(0, "b"), (500, "a")]
    assert lyrics.parse("[offset:-250]\n[00:01.00]a").lines == [(1250, "a")]
