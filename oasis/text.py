"""Names and titles folded for matching: what tells two spellings of one name or song apart only in case, accents,
spacing, punctuation or a bracketed extra is dropped, so they compare equal. Release names have their own
normalisers in releases.py, which keep the kana marks these drop."""
import re
import unicodedata


def fold(text: str) -> str:
    """Case, accents, spaces and punctuation aside: 'Suite‐Pee' (Unicode hyphen) and 'Suite-Pee' are the same title,
    'Chylinska' and 'Chylińska' the same name."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold().replace("ł", "l")
    return re.sub(r"[\W_]+", "", text)


def song_key(title: str) -> str:
    """'Du hast (Remastered 2017)', 'Du Hast - 2011 Remaster', 'Du Hast - Live' -> 'duhast'."""
    return fold(re.sub(r"\s+-\s+.*$", "", re.sub(r"\s*[(\[][^)\]]*[)\]]", "", title or "")))
