"""Vietnamese text normalization for TTS (numbers, dates, times, currencies, roman numerals)."""

from __future__ import annotations

import re
from datetime import date

NORMALIZER_VERSION = "vi-context-v1"

_CHU_SO = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_HANG_DON_VI = ["", "nghìn", "triệu", "tỷ"]

_ROMAN_MAP = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "XI": 11,
    "XII": 12,
    "XIII": 13,
    "XIV": 14,
    "XV": 15,
    "XVI": 16,
    "XVII": 17,
    "XVIII": 18,
    "XIX": 19,
    "XX": 20,
    "XXI": 21,
    "XXII": 22,
}


def _doc_hang_chuc(tram: int, chuc: int, don_vi: int, full: bool = False) -> str:
    """Đọc nhóm 3 chữ số (0-999)."""
    parts: list[str] = []
    if tram > 0 or full:
        parts.append(f"{_CHU_SO[tram]} trăm")

    if chuc == 0:
        if (tram > 0 or full) and don_vi > 0:
            parts.append("lẻ")
    elif chuc == 1:
        parts.append("mười")
    else:
        parts.append(f"{_CHU_SO[chuc]} mươi")

    if chuc > 1 and don_vi == 1:
        parts.append("mốt")
    elif chuc > 0 and don_vi == 5:
        parts.append("lăm")
    elif chuc > 1 and don_vi == 4:
        parts.append("tư")
    elif don_vi > 0:
        parts.append(_CHU_SO[don_vi])

    return " ".join(parts)


def read_integer(n: int) -> str:
    """Đọc số nguyên sang chữ tiếng Việt."""
    if n == 0:
        return "không"
    if n < 0:
        return f"âm {read_integer(-n)}"

    groups: list[int] = []
    temp = n
    while temp > 0:
        groups.append(temp % 1000)
        temp //= 1000

    parts: list[str] = []
    num_groups = len(groups)

    for i in reversed(range(num_groups)):
        g = groups[i]
        if g == 0:
            continue

        tram = g // 100
        chuc = (g % 100) // 10
        dv = g % 10

        is_highest = i == num_groups - 1
        g_text = _doc_hang_chuc(tram, chuc, dv, full=not is_highest)

        hang_str = " ".join(filter(None, [_HANG_DON_VI[i % 3], *(["tỷ"] * (i // 3))]))

        if hang_str:
            parts.append(f"{g_text} {hang_str}".strip())
        else:
            parts.append(g_text.strip())

    return " ".join(parts).strip()


def _read_number(value: str) -> str:
    negative = value.startswith("-")
    value = (
        value.lstrip("+-").replace(".", "")
        if "," in value or re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+", value)
        else value.lstrip("+-")
    )
    pieces = re.split("[,.]", value, maxsplit=1)
    integer = pieces[0]
    reading = (
        " ".join(_CHU_SO[int(c)] for c in integer)
        if len(integer) > 1 and integer.startswith("0")
        else read_integer(int(integer))
    )
    if len(pieces) > 1:
        reading += " phẩy " + " ".join(_CHU_SO[int(c)] for c in pieces[1])
    return ("âm " if negative else "") + reading


_NUMBER = r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[,.]\d+)?"
_TOKEN = re.compile(
    r"(?P<measure>(?<!\w)"
    + _NUMBER
    + r"\s*(?:km/h|kg|km|cm|mm|ml|m²|m³|°C|g|l)(?!\w))|"
    r"(?P<protected>https?://[^\s]+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|"
    r"\b(?:phiên bản|version)\s+v?\d+(?:\.\d+)+|\bv\d+(?:\.\d+)+|"
    r"\b(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}\b(?!\.\d)|\b(?:RTX|GTX|iPhone|Galaxy)\s*\d+[\w-]*|"
    r"\b[A-Za-z]+[-_]?\d+[\w.-]*|\b(?!\d{1,2}h\d{2}\b)\d+[A-Za-z][\w-]+)"
    r"|(?P<date>(?:ngày\s+)?\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b)"
    r"|(?P<month>(?:tháng\s+)?\b\d{1,2}[/-]\d{4}\b)"
    r"|(?P<time>\b\d{1,2}(?::\d{2}(?::\d{2})?|h(?:\d{2})?)\b)"
    r"|(?P<range>\b\d+\s*[-–—]\s*\d+\s*%?)"
    r"|(?P<roman>\b(?:thế kỷ|thế kỉ|tập|phần|chương|kỳ)\s+[IVXLCDM]+\b)"
    r"|(?P<money>(?:\$|USD\s+)"
    + _NUMBER
    + r"|"
    + _NUMBER
    + r"\s*(?:VNĐ|VND|đồng|đ|USD|\$)(?!\w))"
    r"|(?P<number>(?<![\w.])" + _NUMBER + r"(?!\w|\.\d)\s*%?)",
    re.IGNORECASE,
)


def normalize_vietnamese_for_tts(text: str) -> str:
    """Read contextual numbers while preserving identifiers and ambiguous invalid dates."""

    def replace(match):
        kind, raw = match.lastgroup, match.group().strip()
        if kind == "protected":
            return match.group()
        if sum(c.isdigit() for c in raw) > 27:
            return match.group()  # Long identifiers are not assumed to be quantities.
        if kind == "measure":
            number = re.match(_NUMBER, raw).group()
            unit = raw[len(number) :].strip().lower()
            units = {
                "kg": "ki lô gam",
                "g": "gam",
                "km": "ki lô mét",
                "km/h": "ki lô mét trên giờ",
                "cm": "xen ti mét",
                "mm": "mi li mét",
                "ml": "mi li lít",
                "l": "lít",
                "m²": "mét vuông",
                "m³": "mét khối",
                "°c": "độ C",
            }
            return _read_number(number) + " " + units[unit]
        if kind == "date":
            d, m, y = map(int, re.findall(r"\d+", raw))
            try:
                date(y, m, d)
            except ValueError:
                return raw
            return (
                f"ngày {read_integer(d)} tháng {read_integer(m)} năm {read_integer(y)}"
            )
        if kind == "month":
            m, y = map(int, re.findall(r"\d+", raw))
            if not 1 <= m <= 12 or y < 1:
                return raw
            return f"tháng {read_integer(m)} năm {read_integer(y)}"
        if kind == "time":
            values = list(map(int, re.findall(r"\d+", raw)))
            h = values[0]
            minute = values[1] if len(values) > 1 else None
            if (
                h > 23
                or minute is not None
                and minute > 59
                or len(values) > 2
                and values[2] > 59
            ):
                return raw
            return (
                read_integer(h)
                + " giờ"
                + (f" {read_integer(minute)} phút" if minute is not None else "")
                + (f" {read_integer(values[2])} giây" if len(values) > 2 else "")
            )
        if kind == "roman":
            prefix, roman = raw.rsplit(" ", 1)
            return (
                prefix + " " + read_integer(_ROMAN_MAP[roman.upper()])
                if roman.upper() in _ROMAN_MAP
                else raw
            )
        if kind == "range":
            a, b = re.findall(r"\d+", raw)
            return (
                read_integer(int(a))
                + " đến "
                + read_integer(int(b))
                + (" phần trăm" if "%" in raw else "")
                + (" " if match.group()[-1:].isspace() else "")
            )
        if kind == "money":
            number = re.search(_NUMBER, raw).group()
            return _read_number(number) + (
                " đô la" if "$" in raw or "usd" in raw.lower() else " đồng"
            )
        result = _read_number(raw.rstrip("%").strip()) + (
            " phần trăm" if "%" in raw else ""
        )
        return result + (" " if match.group()[-1:].isspace() else "")

    # Single token pass prevents one rule from reinterpreting output of another.
    result = _TOKEN.sub(replace, text)
    glossary = {
        "AI": "ây ai",
        "API": "ây pi ai",
        "App": "áp",
        "Video": "vi-đê-ô",
        "FPS": "ép pê ét",
        "CPU": "xê pê u",
        "GPU": "gờ pê u",
    }
    # Apply glossary only to standalone words outside identifiers/URLs.
    parts = []
    end = 0
    for match in _TOKEN.finditer(result):
        if match.lastgroup != "protected":
            continue
        parts.append(_glossary(result[end : match.start()], glossary))
        parts.append(match.group())
        end = match.end()
    parts.append(_glossary(result[end:], glossary))
    return " ".join("".join(parts).split())


def _glossary(text, glossary):
    return re.sub(
        r"\b(?:" + "|".join(glossary) + r")\b",
        lambda m: next(
            value for key, value in glossary.items() if key.lower() == m.group().lower()
        ),
        text,
        flags=re.IGNORECASE,
    )
