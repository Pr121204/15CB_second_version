from __future__ import annotations

import re
import unicodedata

SAFE_PUNCTUATION = ".,;:!?()-_/\\#&%+@='\"[]{}"

# Characters that are not reliably handled by NFD decomposition
# and frequently seen in invoice text from multiple locales.
SPECIAL_MAP = {
    # German
    "Ã¼": "u",
    "Ãœ": "U",
    "Ã¶": "o",
    "Ã–": "O",
    "Ã¤": "a",
    "Ã„": "A",
    "ÃŸ": "ss",
    # Portuguese / Spanish
    "Ã£": "a",
    "Ãµ": "o",
    "Ã§": "c",
    "Ã‡": "C",
    "Ã¡": "a",
    "Ã ": "a",
    "Ã¢": "a",
    "Ã‚": "A",
    "Ã©": "e",
    "Ãª": "e",
    "Ã‰": "E",
    "Ã­": "i",
    "Ã": "I",
    "Ã³": "o",
    "Ã´": "o",
    "Ã“": "O",
    "Ãº": "u",
    "Ãš": "U",
    "Ã±": "n",
    "Ã‘": "N",
    # French
    "Ã¨": "e",
    "Ã«": "e",
    "Ã®": "i",
    "Ã¯": "i",
    "Ã¹": "u",
    "Ã»": "u",
    "áºž": "SS",
    "Å“": "oe",
    "Å’": "OE",
    "Ã¦": "ae",
    "Ã†": "AE",
    # Turkish
    "Ä±": "i",
    "Ä°": "I",
    "ÄŸ": "g",
    "Äž": "G",
    "ÅŸ": "s",
    "Åž": "S",
    # Polish / Czech / Romanian
    "Å‚": "l",
    "Å": "L",
    "Åº": "z",
    "Å¼": "z",
    "Å¡": "s",
    "Ä": "c",
    "Å¾": "z",
    "Å™": "r",
    "Äƒ": "a",
    "È™": "s",
    "È›": "t",
    # Scandinavian
    "Ã¥": "a",
    "Ã…": "A",
    "Ã¸": "o",
    "Ã˜": "O",
    "Ã°": "d",
    "Ã": "D",
    "Ã¾": "th",
    "Ãž": "TH",
    "Ä³": "ij",
    "Ä²": "IJ",
    "Đ": "D",
    "đ": "d",
    # Punctuation lookalikes
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u00b0": "",
    "\u00a0": " ",
    "Æ’": "f",
}

EXTENDED_SPECIAL_MAP = {
    "ß": "ss",
    "œ": "oe",
    "æ": "ae",
    "ø": "o",
    "ð": "d",
    "þ": "th",
    "Œ": "OE",
    "Æ": "AE",
    "Ø": "O",
    "Ð": "D",
    "Þ": "TH",
    "ƒ": "f",
    "ı": "i",
    "Ł": "L",
    "ł": "l",
    "Ŋ": "N",
    "ŋ": "n",
    "Ŧ": "T",
    "ŧ": "t",
    "Ħ": "H",
    "ħ": "h",
    "Ĳ": "IJ",
    "ĳ": "ij",
    "Ŀ": "L",
    "ŀ": "l",
    "ŉ": "'n",
    "Ō": "O",
    "ō": "o",
    "Ŏ": "O",
    "ŏ": "o",
    "Ő": "O",
    "ő": "o",
    "Ŕ": "R",
    "ŕ": "r",
    "Ŗ": "R",
    "ŗ": "r",
    "Ř": "R",
    "ř": "r",
    "Ś": "S",
    "ś": "s",
    "Ŝ": "S",
    "ŝ": "s",
    "Ş": "S",
    "ş": "s",
    "Š": "S",
    "š": "s",
    "Ţ": "T",
    "ţ": "t",
    "Ť": "T",
    "ť": "t",
    "Ũ": "U",
    "ũ": "u",
    "Ū": "U",
    "ū": "u",
    "Ŭ": "U",
    "ŭ": "u",
    "Ů": "U",
    "ů": "u",
    "Ű": "U",
    "ű": "u",
    "Ų": "U",
    "ų": "u",
    "Ŵ": "W",
    "ŵ": "w",
    "Ŷ": "Y",
    "ŷ": "y",
    "Ÿ": "Y",
    "Ź": "Z",
    "ź": "z",
    "Ż": "Z",
    "ż": "z",
    "Ž": "Z",
    "ž": "z",
    "ƀ": "b",
    "Ɓ": "B",
    "Ƃ": "B",
    "ƃ": "b",
    "Ƅ": "B",
    "ƅ": "b",
    "Ɔ": "O",
    "Ƈ": "C",
    "ƈ": "c",
    "Ɖ": "D",
    "Ɗ": "D",
    "Ƌ": "D",
    "ƌ": "d",
    "ƍ": "d",
    "Ǝ": "E",
    "Ə": "E",
    "Ɛ": "E",
    "Ƒ": "F",
    "Ɠ": "G",
    "Ɣ": "G",
    "ƕ": "hv",
    "Ɩ": "I",
    "Ɨ": "I",
    "Ƙ": "K",
    "ƙ": "k",
    "ƚ": "l",
    "ƛ": "l",
    "Ɯ": "M",
    "Ɲ": "N",
    "ƞ": "n",
    "Ɵ": "O",
    "Ơ": "O",
    "ơ": "o",
    "Ƣ": "OI",
    "ƣ": "oi",
    "Ƥ": "P",
    "ƥ": "p",
    "Ʀ": "R",
    "Ƨ": "S",
    "ƨ": "s",
    "Ʃ": "S",
    "ƪ": "s",
    "ƫ": "t",
    "Ƭ": "T",
    "ƭ": "t",
    "Ʈ": "T",
    "Ư": "U",
    "ư": "u",
    "Ʊ": "U",
    "Ʋ": "V",
    "Ƴ": "Y",
    "ƴ": "y",
    "Ƶ": "Z",
    "ƶ": "z",
    "Ʒ": "Z",
    "Ƹ": "Z",
    "ƹ": "z",
    "ƺ": "z",
    "ƻ": "2",
    "Ƽ": "5",
    "ƽ": "5",
    "ƾ": "ts",
    "ƿ": "w",
    "ǀ": "|",
    "ǁ": "||",
    "ǂ": "=",
    "ǃ": "!",
    "Ǆ": "DZ",
    "ǅ": "Dz",
    "ǆ": "dz",
    "Ǉ": "LJ",
    "ǈ": "Lj",
    "ǉ": "lj",
    "Ǌ": "NJ",
    "ǋ": "Nj",
    "ǌ": "nj",
    "Ǎ": "A",
    "ǎ": "a",
    "Ǐ": "I",
    "ǐ": "i",
    "Ǒ": "O",
    "ǒ": "o",
    "Ǔ": "U",
    "ǔ": "u",
    "Ǖ": "U",
    "ǖ": "u",
    "Ǘ": "U",
    "ǘ": "u",
    "Ǚ": "U",
    "ǚ": "u",
    "Ǜ": "U",
    "ǜ": "u",
    "ǝ": "e",
    "Ǟ": "A",
    "ǟ": "a",
    "Ǡ": "A",
    "ǡ": "a",
    "Ǣ": "AE",
    "ǣ": "ae",
    "Ǥ": "G",
    "ǥ": "g",
    "Ǧ": "G",
    "ǧ": "g",
    "Ǩ": "K",
    "ǩ": "k",
    "Ǫ": "O",
    "ǫ": "o",
    "Ǭ": "O",
    "ǭ": "o",
    "Ǯ": "E",
    "ǯ": "e",
    "ǰ": "j",
    "Ǳ": "DZ",
    "ǲ": "Dz",
    "ǳ": "dz",
    "Ǵ": "G",
    "ǵ": "g",
    "Ƕ": "H",
    "Ƿ": "W",
    "Ǹ": "N",
    "ǹ": "n",
    "Ǻ": "A",
    "ǻ": "a",
    "Ǽ": "AE",
    "ǽ": "ae",
    "Ǿ": "O",
    "ǿ": "o",
    "Ȁ": "A",
    "ȁ": "a",
    "Ȃ": "A",
    "ȃ": "a",
    "Ȅ": "E",
    "ȅ": "e",
    "Ȇ": "E",
    "ȇ": "e",
    "Ȉ": "I",
    "ȉ": "i",
    "Ȋ": "I",
    "ȋ": "i",
    "Ȍ": "O",
    "ȍ": "o",
    "Ȏ": "O",
    "ȏ": "o",
    "Ȑ": "R",
    "ȑ": "r",
    "Ȓ": "R",
    "ȓ": "r",
    "Ȕ": "U",
    "ȕ": "u",
    "Ȗ": "U",
    "ȗ": "u",
    "Ș": "S",
    "ș": "s",
    "Ț": "T",
    "ț": "t",
    "Ȝ": "Y",
    "ȝ": "y",
    "Ȟ": "H",
    "ȟ": "h",
    "Ƞ": "N",
    "ȡ": "d",
    "Ȣ": "OU",
    "ȣ": "ou",
    "Ȥ": "Z",
    "ȥ": "z",
    "Ȧ": "A",
    "ȧ": "a",
    "Ȩ": "E",
    "ȩ": "e",
    "Ȫ": "O",
    "ȫ": "o",
    "Ȭ": "O",
    "ȭ": "o",
    "Ȯ": "O",
    "ȯ": "o",
    "Ȱ": "O",
    "ȱ": "o",
    "Ȳ": "Y",
    "ȳ": "y",
    "ȴ": "l",
    "ȵ": "n",
    "ȶ": "t",
    "ȷ": "j",
    "ȸ": "db",
    "ȹ": "qp",
    "Ⱥ": "A",
    "Ȼ": "C",
    "ȼ": "c",
    "Ƚ": "L",
    "Ⱦ": "T",
    "ȿ": "s",
    "ɀ": "z",
    "Ɂ": "?",
    "ɂ": "?",
    "Ƀ": "B",
    "Ʉ": "U",
    "Ʌ": "V",
    "Ɇ": "E",
    "ɇ": "e",
    "Ɉ": "J",
    "ɉ": "j",
    "Ɋ": "Q",
    "ɋ": "q",
    "Ɍ": "R",
    "ɍ": "r",
    "Ɏ": "Y",
    "ɏ": "y",
}

SPECIAL_MAP.update(EXTENDED_SPECIAL_MAP)


def _apply_special_map(text: str) -> str:
    out = text
    for src, dst in SPECIAL_MAP.items():
        out = out.replace(src, dst)
    return out


def normalize_invoice_text(text: str, keep_newlines: bool = True) -> str:
    if not text:
        return ""
    t = _apply_special_map(str(text))
    # Remove combining marks while preserving base letters and unknown scripts.
    t = unicodedata.normalize("NFD", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    # Remove only non-printable control chars; keep unknown/non-transliterable chars.
    t = "".join(ch if (ch in "\n\t" or unicodedata.category(ch) != "Cc") else " " for ch in t)

    if keep_newlines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in t.splitlines()]
        # Keep structure, but avoid too many blank lines.
        collapsed: list[str] = []
        blank_run = 0
        for line in lines:
            if line:
                collapsed.append(line)
                blank_run = 0
            else:
                blank_run += 1
                if blank_run <= 1:
                    collapsed.append("")
        return "\n".join(collapsed).strip()

    return re.sub(r"\s+", " ", t).strip()


def normalize_single_line_text(text: str) -> str:
    return normalize_invoice_text(text, keep_newlines=False)



# Known OCR split-word fixes: OCR engines often insert spurious spaces inside
# well-known company suffixes and German/European address words.  Applied
# case-insensitively BEFORE the regex-based camelCase splitter so that the
# regex does not further fragment these tokens.
_OCR_SPLIT_FIXES = {
    "GMB H": "GMBH",
    "GM BH": "GMBH",
    "G MBH": "GMBH",
    "LT D": "LTD",
    "PV T": "PVT",
    "IN C": "INC",
    "LL C": "LLC",
    "COR P": "CORP",
    "PL ATZ": "PLATZ",
    "STR ASSE": "STRASSE",
    "S TR.": "STR.",
}
# Pre-compile a single regex for all patterns (case-insensitive).
_OCR_SPLIT_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_OCR_SPLIT_FIXES, key=len, reverse=True)),
    flags=re.IGNORECASE,
)


def fix_concatenated_words(text: str) -> str:
    """
    Fixes PDF extraction artifacts where words are joined without spaces.

    Handles:
    - Known OCR split compounds: "GMB H" -> "GMBH", "STR ASSE" -> "STRASSE"
    - lowercase->Uppercase boundary: "HosurRoad" -> "Hosur Road"
    - letter->digit boundary: "Bangalore560030" -> "Bangalore 560030"
    - digit->letter boundary: "560030India" -> "560030 India"
    - acronym end before Title case: "INDIAHosur" -> "INDIA Hosur"
    """
    if not text:
        return text

    s = str(text)

    # Pass 1: repair known OCR split compounds (case-insensitive).
    def _ocr_replace(m: re.Match) -> str:  # noqa: E501
        return _OCR_SPLIT_FIXES.get(m.group(0).upper(), m.group(0))
    s = _OCR_SPLIT_RE.sub(_ocr_replace, s)

    # Pass 2: insert spaces at word boundaries.
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    # Do not split ordinal suffixes like 1st/2nd/3rd/4th.
    s = re.sub(r"(\d)(?!(?:st|nd|rd|th)\b)([A-Za-z])", r"\1 \2", s, flags=re.IGNORECASE)
    s = re.sub(r"([A-Z]{2,})([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r" {2,}", " ", s)
    return s.strip()


def is_ascii_clean(text: str) -> bool:
    s = str(text or "")
    return all(ord(ch) < 128 for ch in s)
