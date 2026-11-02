from pathlib import Path

import pytest

from oasis import romanize

CASES = [line.rstrip("\n").split("\t") for line in (Path(__file__).parent / "romanization-cases.tsv").read_text().splitlines()
         if line and not line.startswith("#")]


def run(kind: str, input: str, siblings: list[str]) -> str:
    match kind:
        case "name" | "title": return romanize.text(input, siblings, kind == "title")
        case "line": return romanize.machine(input, siblings, sentence=True)
        case "parts": return "|".join(romanize.parts(input.split("|"), siblings))
        case "pieces": return "|".join(romanize.pieces(input, siblings))
        case "chinese": return str(romanize.looks_chinese(input)).lower()
        case "lyrics": return "\\n".join(romanize.lyrics(input.split("\\n"), siblings))


@pytest.mark.parametrize("kind,input,siblings,expected", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_same_as_roma(kind, input, siblings, expected):
    assert run(kind, input, [s for s in siblings.split("|") if s]) == expected


def test_latin_needs_no_romanizing():
    assert romanize.is_latin("you to the monster （Yeah）", romanize.text("you to the monster （Yeah）"))
    assert romanize.is_latin("ＬＯＶＥ", "LOVE")
    assert not romanize.is_latin("君の声", romanize.text("君の声"))


def test_language_tag_reads_han_as_chinese_when_the_text_cannot_tell():
    assert romanize.lyrics(["声音"]) == ["Kowane"] and romanize.language(["声音"]) == "ja"
    assert romanize.lyrics(["声音"], lang="zho") == ["Shēng yīn"] and romanize.language(["声音"], lang="zh-Hans") == "zh"
    assert romanize.lyric_words(["声音"], [["声", "音"]], lang="chi") == [["Shēng", " yīn"]]
    assert romanize.lyrics(["声音", "こころ"], lang="zho") == ["Kowane", "Kokoro"]  # kana outweighs the tag
    assert romanize.lyrics(["声音"], lang="eng") == ["Kowane"]
