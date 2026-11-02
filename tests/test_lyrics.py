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
    assert found.words == [[[1000, 1500, "Hel"], [1500, 2000, "lo (world)"]]]


def test_kugou_decrypts_krc(monkeypatch):
    krc = "[1000,2000]<0,500,0>Hi<500,500,0> there".encode()
    packed = zlib.compress(krc)
    encrypted = base64.b64encode(b"krc1" + bytes(b ^ KUGOU_KEY[i % 16] for i, b in enumerate(packed))).decode()
    respond(monkeypatch, {
        "https://mobileservice.kugou.com": json.dumps({"data": {"info": [{"songname": "Song", "duration": 200, "hash": "h"}]}}).encode(),
        "https://krcs.kugou.com/search": json.dumps({"candidates": [{"id": 1, "accesskey": "k"}]}).encode(),
        "https://krcs.kugou.com/download": json.dumps({"content": encrypted}).encode()})
    found = lyrics.kugou(QUERY)
    assert found.lines == [(1000, "Hi there")] and found.words == [[[1000, 1500, "Hi"], [1500, 2000, " there"]]]  # starts relative to the line


def test_lyricsplus_rejects_timing_that_does_not_fit(monkeypatch):
    def body(end):
        return json.dumps({"metadata": {"source": "Apple"}, "lyrics": [
            {"time": 1000, "duration": end - 1000, "syllabus": [{"text": "Hey"}]}]}).encode()
    respond(monkeypatch, {"https://lyricsplus": body(190_000)})
    found = lyrics.lyricsplus(QUERY)
    assert found.source == "LyricsPlus · Apple" and found.word_level and found.words == [[[0, 0, "Hey"]]]
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


def test_enhanced_lrc_keeps_word_timing():
    found = lyrics.parse("[offset:100]\n[00:02.00]<00:02.00>b<00:02.50>c\n[00:01.00]<00:01.00>a\n[00:03.00]d")
    assert found.lines == [(900, "a"), (1900, "bc"), (2900, "d")]
    assert found.words == [[[900, 1900, "a"]], [[1900, 2400, "b"], [2400, 2900, "c"]], []]  # an open word runs to the next line
    cut = lyrics.cleaned(found, Query("x", "y", None, 0)).subset([1])
    assert cut.lines == [(1900, "bc")] and cut.words == [[[1900, 2400, "b"], [2400, 2900, "c"]]]


MERGED = """[00:01.00]I can't feel the way I did before
[00:01.00]我再也感觉不到从前的自己
[00:04.00]Don't turn your back on me
[00:04.00]别背弃我
[00:07.00]Oh
[00:09.00]I won't be ignored
[00:09.00]我不会被无视
"""


def test_translations_merged_into_lyrics_are_taken_out():
    found = lyrics.separate(parse(MERGED))
    assert found.lines == [(1000, "I can't feel the way I did before"), (4000, "Don't turn your back on me"), (7000, "Oh"), (9000, "I won't be ignored")]
    assert found.translations == {"zh": ["我再也感觉不到从前的自己", "别背弃我", "", "我不会被无视"]}
    assert lyrics.separate(found) == found  # once is enough


def test_a_japanese_songs_chinese_translation_goes_and_its_own_lines_stay():
    found = lyrics.separate(parse("[00:01.00]夜に駆ける\n[00:01.00]向夜晚奔去\n[00:03.00]東京\n[00:03.00]东京\n[00:05.00]Hey\n[00:05.00]Hey Hey"))
    assert [t for _, t in found.lines] == ["夜に駆ける", "東京", "Hey", "Hey Hey"]  # a backing line in the song's own Latin stays
    assert found.translations == {"zh": ["向夜晚奔去", "东京", "", ""]}


def test_a_merged_reading_goes_without_becoming_a_translation():
    found = lyrics.separate(parse("[00:01.00]夜に駆ける\n[00:01.00]yoru ni kakeru\n[00:03.00]忙しい\n[00:03.00]isogashii"))
    assert [t for _, t in found.lines] == ["夜に駆ける", "忙しい"] and found.translations is None


def test_lines_sung_together_in_one_language_stay():
    duet = parse("[00:01.00]I go\n[00:01.00]You stay\n[00:03.00]One")
    assert lyrics.separate(duet) == duet


def test_kugou_krc_translation_block_is_a_translation(monkeypatch):
    language = base64.b64encode(json.dumps({"content": [{"type": 0, "lyricContent": [["Hai"], ["za"]]},
                                                        {"type": 1, "lyricContent": [["你好"], ["那里"]]}]}).encode()).decode()
    krc = f"[language:{language}]\n[1000,2000]<0,500,0>Hi\n[3000,1000]<0,500,0>there".encode()
    packed = zlib.compress(krc)
    encrypted = base64.b64encode(b"krc1" + bytes(b ^ KUGOU_KEY[i % 16] for i, b in enumerate(packed))).decode()
    respond(monkeypatch, {
        "https://mobileservice.kugou.com": json.dumps({"data": {"info": [{"songname": "Song", "duration": 200, "hash": "h"}]}}).encode(),
        "https://krcs.kugou.com/search": json.dumps({"candidates": [{"id": 1, "accesskey": "k"}]}).encode(),
        "https://krcs.kugou.com/download": json.dumps({"content": encrypted}).encode()})
    found = lyrics.kugou(QUERY)
    assert found.lines == [(1000, "Hi"), (3000, "there")] and found.translations == {"zh": ["你好", "那里"]}


def test_netease_translation_is_a_translation(monkeypatch):
    respond(monkeypatch, {
        "https://music.163.com/api/cloudsearch": json.dumps({"result": {"songs": [{"id": 7, "name": "Song", "dt": 200500}]}}).encode(),
        "https://music.163.com/api/song/lyric": json.dumps({"yrc": {"lyric": "[1000,2000](1000,500,0)Hello\n[3000,500](3000,500,0)Bye"},
                                                            "tlyric": {"lyric": "[00:01.20]你好\n[00:03.00]//"}}).encode()})
    found = lyrics.netease(QUERY)
    assert found.lines == [(1000, "Hello"), (3000, "Bye")] and found.translations == {"zh": ["你好", ""]}


def test_translations_interleaved_with_their_own_times_are_taken_out():
    # QQ Music through LyricsPlus: each translation is its own line, timed after the line it translates.
    found = lyrics.separate(parse("[00:14.13][Verse 1]\n[00:15.09]No smoke with no fire\n[00:17.04]无火便无烟\n"
                                  "[00:18.03]No silence if there's no sound\n[00:20.94]无声便无寂\n[00:21.00]One way or another\n[00:22.94]无论怎样"))
    assert [t for _, t in found.lines] == ["[Verse 1]", "No smoke with no fire", "No silence if there's no sound", "One way or another"]
    assert found.translations == {"zh": ["", "无火便无烟", "无声便无寂", "无论怎样"]}


def test_a_song_with_a_few_lines_in_another_language_keeps_them():
    kpop = parse("[00:01.00]너를 보면\n[00:02.00]Baby\n[00:03.00]가슴이 뛰어\n[00:04.00]내 마음\n[00:05.00]Oh my love\n[00:06.00]사랑해\n[00:07.00]너만\n")
    assert lyrics.separate(kpop) == kpop
