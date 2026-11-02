"""Machine translation of lyrics, made only when a client asks for it (api.py, oasisTranslate). A backend translates
lines into a language: as many lines back, and the language it found they were in; it raises when it can't."""
import json
import re
import urllib.parse
import urllib.request
from typing import Protocol

GOOGLE = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&dt=t&tl={}"
LANG = re.compile(r"[a-z]{2,3}(-[A-Z]{2}|-[A-Z][a-z]{3})?")  # what oasisTranslate takes: en, pl, zh-CN, zh-TW, sr-Latn
LANGUAGES = {"en": "English", "pl": "Polish", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
             "nl": "Dutch", "sv": "Swedish", "cs": "Czech", "uk": "Ukrainian", "ru": "Russian", "tr": "Turkish", "ja": "Japanese",
             "ko": "Korean", "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)", "id": "Indonesian", "vi": "Vietnamese",
             "th": "Thai", "ar": "Arabic", "hi": "Hindi"}  # offered on the dashboard; requests may name others


class Translator(Protocol):
    def translate(self, lines: list[str], lang: str) -> tuple[list[str], str]:
        """lines in lang, one for one, and the language they were in. Raises when it can't."""


def primary(lang: str | None) -> str:
    return (lang or "").split("-")[0].lower()


class Google:
    """Google Translate's unofficial endpoint (the one its web widgets use): no key, one request per song, line breaks
    kept. Google may block it at any time; callers serve no translation then."""

    def translate(self, lines: list[str], lang: str) -> tuple[list[str], str]:
        worded = [i for i, line in enumerate(lines) if line.strip()]  # blank lines would merge
        body = urllib.parse.urlencode({"q": "\n".join(lines[i].strip() for i in worded)}).encode()
        request = urllib.request.Request(GOOGLE.format(urllib.parse.quote(lang)), body,
                                         {"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        out = "".join(segment[0] for segment in data[0] or [] if segment and segment[0]).split("\n")
        if len(out) != len(worded):
            raise ValueError(f"Google gave {len(out)} lines for {len(worded)}")
        found = dict(zip(worded, out))
        return [found.get(i, "").strip() for i in range(len(lines))], data[2] or "und"
