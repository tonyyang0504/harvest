"""Normalise and clean scraped rows against a record template.

- locale-aware numbers: grouping by space/dot/comma/apostrophe, Indian grouping (12,34,567),
  multipliers (k, m, mn, million, bn, тыс, млн, млрд, lakh, crore, 万/萬, 億/亿, 만, 억, 천, 千, 兆/조)
- money: amount + currency detected from ISO codes and symbols (ambiguous symbols such as $, ¥,
  kr and Rs resolve to the region's currency), conversion through a pluggable FX table
- units: area -> m² (sqft, 坪, 평, acre, ha, sotka), distance -> km (mi, тыс. км, 万公里), weight -> kg
- periods: rent/salary per day/week/month/year -> canonical period
- dates: ISO, epoch, dd.mm.yyyy, dd/mm/yyyy (mm/dd for US), relative words
- enums with multilingual synonyms, booleans, URLs made absolute
- sanity bounds and category conflicts -> quarantine with a reason
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import geo, regions
from .templates import PERIOD_SYNONYMS

# ----------------------------------------------------------------------------------- numbers
_MULT_WORDS = [
    (r"crores?|cr\b", 1e7), (r"lakhs?|lacs?|lac\b", 1e5),
    (r"billion|bn\b|mrd\.?|milliard[s]?|млрд\.?|mlrd", 1e9),
    (r"million[s]?|millones|millions|mio\.?|mill\.?|mln\.?|mn\b|млн\.?|m\b|mm\b|juta|jt\b\.?", 1e6),
    (r"thousand|mil\b|тыс\.?|тис\.?|k\b|K\b|tsd\.?|ribu|rb\b\.?", 1e3),
]
# Arabic-Indic and Persian digits, and the Arabic thousands / decimal separators (٬ ٫)
_DIGITS = str.maketrans({**{chr(0x660 + i): str(i) for i in range(10)}, **{chr(0x6F0 + i): str(i) for i in range(10)}, "\u066c": ",", "\u066b": "."})
_CJK_BIG = {"兆": 1e12, "조": 1e12, "億": 1e8, "亿": 1e8, "억": 1e8, "萬": 1e4, "万": 1e4, "만": 1e4}
_CJK_SMALL = {"千": 1e3, "천": 1e3, "仟": 1e3, "百": 1e2, "백": 1e2}
_NUM = r"\d[\d.,\s   '’]*"


def _plain_number(s: str, decimal: str | None = None) -> float | None:
    """'1 234,5' -> 1234.5; '1.250.000' -> 1250000; '12,34,567' -> 1234567; '82,32' -> 82.32; '1,500' -> 1500.
    `decimal` forces the decimal mark when a caller knows the locale."""
    s = re.sub(r"[\s   '’]", "", s).rstrip(".,")
    if not s or not re.search(r"\d", s):
        return None
    if decimal:
        grp = "." if decimal == "," else ","
        s = s.replace(grp, "").replace(decimal, ".")
    elif "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2 or len(parts[-1]) == 3:
            s = "".join(parts)
        else:
            s = ".".join(parts)
    try:
        return float(s)
    except ValueError:
        return None


def parse_number(value: Any, decimal: str | None = None) -> float | None:
    """A number from text with grouping and multipliers. None when there is no number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = html.unescape(str(value)).strip().translate(_DIGITS)
    if not text:
        return None
    if any(ch in text for ch in _CJK_BIG) or any(ch in text for ch in _CJK_SMALL):
        total, found = 0.0, False
        for m in re.finditer(r"(\d[\d.,]*)\s*([千천仟百백]?)\s*([兆조億亿억萬万만]?)", text):
            if not m.group(2) and not m.group(3):
                continue  # a bare number beside CJK units ('2LDK', '築10年') is not part of the amount
            n = _plain_number(m.group(1), decimal)
            if n is not None:
                total += n * _CJK_SMALL.get(m.group(2), 1) * _CJK_BIG.get(m.group(3), 1)
                found = True
        if found:
            return total
    m = re.search(r"(?<![\w-])(-?)(" + _NUM + r")\s*(" + "|".join(p for p, _ in _MULT_WORDS) + r")?", text, re.I)
    if not m:
        return None
    n = _plain_number(m.group(2), decimal)
    if n is None:
        return None
    if m.group(3):
        word = m.group(3)
        for pat, mult in _MULT_WORDS:
            if re.fullmatch(pat, word, re.I) or re.fullmatch(pat, word):
                n *= mult
                break
    return -n if m.group(1) else n


def parse_int(value: Any) -> int | None:
    n = parse_number(value)
    return None if n is None else int(round(n))


# ------------------------------------------------------------------------------------ money
ISO_CURRENCIES = {c["currency"] for c in regions.COUNTRIES.values()} | {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "BTC", "XAU"}
_SYMBOLS = [  # longest first; value None = region-ambiguous family
    ("R$", "BRL"), ("US$", "USD"), ("C$", "CAD"), ("A$", "AUD"), ("NZ$", "NZD"), ("HK$", "HKD"), ("S$", "SGD"), ("NT$", "TWD"), ("MX$", "MXN"),
    ("S/.", "PEN"), ("S/", "PEN"), ("zł", "PLN"), ("Kč", "CZK"), ("Ft", "HUF"), ("lei", "RON"), ("лв", "BGN"), ("руб", "RUB"), ("р.", "RUB"), ("₽", "RUB"),
    ("₸", "KZT"), ("тг", "KZT"), ("тенге", "KZT"), ("₾", "GEL"), ("лари", "GEL"), ("ლარი", "GEL"), ("₼", "AZN"), ("֏", "AMD"), ("₴", "UAH"), ("грн", "UAH"),
    ("€", "EUR"), ("£", "GBP"), ("₩", "KRW"), ("원", "KRW"), ("₹", "INR"), ("₺", "TRY"), ("₪", "ILS"), ("₫", "VND"), ("฿", "THB"), ("₱", "PHP"), ("₦", "NGN"),
    ("円", "JPY"), ("元", "CNY"), ("RMB", "CNY"), ("Rp", "IDR"), ("RM", "MYR"), ("د.إ", "AED"), ("ر.س", "SAR"), ("ج.م", "EGP"), ("сум", "UZS"), ("сом", "KGS"),
    ("ر.ق", "QAR"), ("د.ك", "KWD"), ("ر.ع", "OMR"), ("Dhs", "AED"), ("Dh", "AED"),
    ("$", None), ("¥", None), ("kr", None), ("Rs", None), ("ريال", None), ("درهم", None), ("Fr.", "CHF"), ("CHF", "CHF"),
]
_FAMILIES = {"$": {"USD", "CAD", "AUD", "NZD", "MXN", "ARS", "CLP", "COP", "UYU", "SGD", "HKD", "TWD", "BSD", "BBD", "BZD", "BMD", "FJD", "GYD", "JMD", "KYD", "LRD", "NAD", "SBD", "SRD", "TTD", "XCD", "BND", "DOP", "CUP"},
             "¥": {"JPY", "CNY"}, "kr": {"SEK", "NOK", "DKK", "ISK"}, "Rs": {"INR", "PKR", "LKR", "NPR", "MUR", "SCR"},
             "ريال": {"SAR", "QAR", "OMR", "YER", "IRR"}, "درهم": {"AED", "MAD"}}
_FAMILY_DEFAULT = {"$": "USD", "¥": "JPY", "kr": "SEK", "Rs": "INR", "ريال": "SAR", "درهم": "AED"}
# area units whose letters look like a currency ("sq ft" is not Hungarian forint)
_NOT_MONEY = re.compile(r"\bsq\.?\s*f(?:ee|oo)?t\b|\bsquare\s+f(?:ee|oo)t\b|\bft[²2]|\bpsf\b", re.I)


def detect_currency(text: str, default: str | None = None) -> str | None:
    t = _NOT_MONEY.sub(" ", str(text or ""))
    for code in re.findall(r"\b([A-Z]{3})\b", t):
        if code in ISO_CURRENCIES:
            return code
    low = t.lower()
    for sym, code in _SYMBOLS:
        hit = (sym in t) if not sym.isalpha() or sym in ("RM", "Rp", "Rs", "RMB", "CHF") else re.search(r"(?<![^\W\d_])" + re.escape(sym.lower()) + r"(?![^\W\d_])", low)
        if hit:
            if code:
                return code
            fam = _FAMILIES[sym]
            return default if default in fam else _FAMILY_DEFAULT[sym]
    return default


def parse_money(value: Any, default_currency: str | None = None) -> tuple[float | None, str | None]:
    if isinstance(value, dict):  # schema.org Offer / {amount, currency}
        amt = value.get("price", value.get("amount", value.get("value")))
        cur = value.get("priceCurrency") or value.get("currency")
        a, c = parse_money(amt, cur or default_currency)
        return a, (cur.upper() if isinstance(cur, str) and cur.upper() in ISO_CURRENCIES else c)
    if value is None or isinstance(value, bool):
        return None, default_currency
    if isinstance(value, (int, float)):
        return float(value), default_currency
    text = html.unescape(str(value))
    if re.search(r"\b(free|gratis|gratuit|kostenlos|бесплатно|無料|免费)\b", text, re.I) and not re.search(r"\d", text):
        return 0.0, default_currency
    # a currency glued to the amount ('RM799', 'Rp5.000.000', 'Rs.500', 'USD1200'): the number parser refuses a digit
    # that follows a letter (so 'A4' or 'iPhone13' are not amounts), so split them first
    spaced = re.sub(r"(?<![^\W\d_])([^\W\d_]{1,3}\.?)(?=\d)", r"\1 ", text)
    return parse_number(spaced), detect_currency(spaced, default_currency)


def price_range(value: Any) -> tuple[float | None, float | None]:
    """'1 200 – 1 500' -> (1200, 1500)."""
    text = str(value or "")
    parts = re.split(r"\s[-–—]\s|–|—|\bto\b|\bbis\b|\bdo\b|\ba\b|\bдо\b", text)
    nums = [parse_number(p) for p in parts if re.search(r"\d", p)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None
    return min(nums), max(nums)


class FxTable:
    """Units of each currency per 1 unit of `base` (default USD). Pluggable: build from a dict, a JSON
    file ({"base": "USD", "as_of": "...", "rates": {...}}) or a provider callable."""

    def __init__(self, rates: dict | None = None, base: str = "USD", as_of: str | None = None):
        self.base = base.upper()
        self.rates = {k.upper(): float(v) for k, v in (rates or {}).items() if v}
        self.rates.setdefault(self.base, 1.0)
        self.as_of = as_of

    @classmethod
    def from_json(cls, data: dict) -> "FxTable":
        return cls(data.get("rates") or {}, data.get("base", "USD"), data.get("as_of"))

    def convert(self, amount: float | None, src: str | None, dst: str | None) -> float | None:
        if amount is None or not src or not dst:
            return None
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return amount
        if src not in self.rates or dst not in self.rates:
            return None
        return amount / self.rates[src] * self.rates[dst]

    def to_dict(self) -> dict:
        return {"base": self.base, "as_of": self.as_of, "rates": self.rates}


# ------------------------------------------------------------------------------------ units
AREA_UNITS = [  # (unit, pattern, factor to m²) — None factor = recognised, not safely convertible
    ("sqft", r"sq\.?\s*ft|sqft|square\s*f(ee|oo)t|ft²|ft2|pies?\s*cuadrados", 0.09290304),
    ("sqm", r"m²|m2\b|\bqm\b|sq\.?\s*m\b|sqm|square\s*met|mts2|㎡|平米|平方米|кв\.?\s*м|м²|м2|metros?\s*cuadrados|m\.?\s*q\.?", 1.0),
    ("tsubo", r"坪|tsubo", 3.305785),
    ("pyeong", r"평|pyeong", 3.305785),
    ("hectare", r"\bha\b|hectares?|гектар|\bга\b", 10000.0),
    ("acre", r"\bacres?\b", 4046.8564224),
    ("sotka", r"сот(ка|ки|ок)|sotka|sotok", 100.0),
    ("perch", r"perch(es)?", None),
    ("marla", r"marlas?", None),
    ("kanal", r"kanals?", None),
    ("aana", r"aanas?", None),
]
DISTANCE_UNITS = [("mi", r"\bmi\b|miles?|mls", 1.609344), ("km", r"km|кm|км|kilomet|公里|キロ|킬로", 1.0), ("m", r"\bm\b|meters?|metres?", 0.001)]
WEIGHT_UNITS = [("lb", r"\blbs?\b|pounds?", 0.45359237), ("g", r"\bg\b|grams?|gr\b|грамм|г\b", 0.001), ("kg", r"kg|kilo|кг|公斤|キロ", 1.0), ("t", r"\btons?\b|\bt\b|тонн", 1000.0)]


def _unit_parse(value: Any, units, default_unit: str | None, unit_hint: str | None):
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        unit = unit_hint or default_unit
        factor = next((f for u, _, f in units if u == unit), None)
        return (float(value) * factor if factor else None), unit
    text = html.unescape(str(value))
    unit, factor, num_text = None, None, text
    for u, pat, f in units:
        m = re.search(pat, text, re.I)
        if m:
            unit, factor = u, f
            before = text[: m.start()]
            nm = list(re.finditer(r"\d[\d.,\s  '’]*(?:\s*(?:тыс\.?|thousand|k\b|万|萬))?", before))
            if nm:
                num_text = nm[-1].group(0)
            break
    if unit is None:
        unit = unit_hint or default_unit
        factor = next((f for u, _, f in units if u == unit), None)
    n = parse_number(num_text)
    if n is None:
        return None, unit
    return (n * factor if factor else None), unit


def parse_area(value: Any, unit_hint: str | None = None) -> tuple[float | None, str | None]:
    """-> (m², unit as shown). Numbers get a decimal comma only when not 3 trailing digits ('82,32 m²' = 82.32)."""
    a, u = _unit_parse(value, AREA_UNITS, "sqm", unit_hint)
    return (round(a, 2) if a is not None else None), u


def parse_distance(value: Any, unit_hint: str | None = None) -> tuple[float | None, str | None]:
    d, u = _unit_parse(value, DISTANCE_UNITS, "km", unit_hint)
    return (round(d, 1) if d is not None else None), u


def parse_weight(value: Any, unit_hint: str | None = None) -> tuple[float | None, str | None]:
    w, u = _unit_parse(value, WEIGHT_UNITS, "kg", unit_hint)
    return (round(w, 4) if w is not None else None), u


# ------------------------------------------------------------------------------ price basis
_PER_AREA = re.compile(r"\bpsf\b|\bpsm\b|per\s*(?:sq\.?\s*(?:ft|feet|m)|square\s*(?:foot|feet|met)|m²|m2|sqm|sqft)|/\s*(?:sq\.?\s*ft|sqft|sf|m²|m2|sqm)\b|"
                       r"/\s*m²|€/m²|/qm\b|pro\s*m²|per\s*meter\s*persegi|/\s*kaki\s*persegi", re.I)
_PER_PERSON = re.compile(r"(?:per|/|a)\s*(?:seat|desk|pax|person|head|workstation|member|orang|kerusi|meja)s?\b|\bper\s*cap|"
                         r"/\s*(?:seat|desk|pax|person)\b|\bseorang\b", re.I)


def price_basis(text: Any) -> str | None:
    """'From RM799 / Seat / Month' -> per_person; 'S$35.00 PSF' -> per_area; None when the text does not say."""
    t = str(text or "")
    if _PER_AREA.search(t):
        return "per_area"
    if _PER_PERSON.search(t):
        return "per_person"
    return None


# ----------------------------------------------------------------------------------- period
_PERIOD_FACTORS = {"hour": 2080.0, "day": 365.0, "week": 52.0, "month": 12.0, "year": 1.0}  # occurrences per year (40 h/week)


def parse_period(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    t = str(value).strip().lower()
    if t in PERIOD_SYNONYMS:
        return PERIOD_SYNONYMS[t]
    t2 = re.sub(r"^(per|a|an|по|al|pro|par|/)\s*", "", t).strip(" ./")
    if t2 in PERIOD_SYNONYMS:
        return PERIOD_SYNONYMS[t2]
    for key in sorted(PERIOD_SYNONYMS, key=len, reverse=True):
        if len(key) >= 3 and re.search(r"(?<![^\W\d_])" + re.escape(key) + r"(?![^\W\d_])", t):
            return PERIOD_SYNONYMS[key]
    # a short unit after a slash: '120 zł/h', '4 500 S$/mo', '€/m' (never '€/m²', which is per square metre)
    m = re.search(r"/\s*([^\W\d_]{1,4})\.?(?![^\W_]|[²³])", t)
    if m and m.group(1) in PERIOD_SYNONYMS:
        return PERIOD_SYNONYMS[m.group(1)]
    return default


def convert_period(amount: float | None, src: str | None, dst: str) -> float | None:
    if amount is None or not src or src not in _PERIOD_FACTORS:
        return None
    return round(amount * _PERIOD_FACTORS[src] / _PERIOD_FACTORS[dst], 2)


# ------------------------------------------------------------------------------------ dates
_REL = {"today": 0, "heute": 0, "hoy": 0, "hoje": 0, "aujourd'hui": 0, "сегодня": 0, "oggi": 0, "bugün": 0, "今日": 0, "今天": 0, "오늘": 0, "დღეს": 0, "just now": 0,
        "yesterday": 1, "gestern": 1, "ayer": 1, "ontem": 1, "hier": 1, "вчера": 1, "ieri": 1, "dün": 1, "昨日": 1, "昨天": 1, "어제": 1, "გუშინ": 1}

# month names and abbreviations: en de nl fr es pt it pl (nominative and genitive) id ms
_MONTH_WORDS = [
    ("january jan januar januari janvier enero janeiro gennaio gen styczeń stycznia sty", 1),
    ("february feb februar februari février fevrier febrero fevereiro fev febbraio luty lutego lut", 2),
    ("march mar märz maerz mrz maart mrt mars marzo março marco marzec marca maret mac", 3),
    ("april apr avril abril aprile kwiecień kwietnia kwi", 4),
    ("may mai mei mayo maio maggio mag maj maja", 5),
    ("june jun juni juin junio junho giugno giu czerwiec czerwca cze", 6),
    ("july jul juli juillet julio julho luglio lug lipiec lipca lip julai", 7),
    ("august aug augustus août aout agosto ago sierpień sierpnia sie agustus ogos", 8),
    ("september sep sept septembre septiembre setembro set settembre wrzesień września wrz", 9),
    ("october oct oktober okt octobre octubre outubro out ottobre ott październik października paź paz", 10),
    ("november nov novembre noviembre novembro listopad listopada lis", 11),
    ("december dec dezember dez décembre decembre diciembre dic dezembro dicembre grudzień grudnia gru desember disember des", 12),
]
_MONTHS = {w: n for words, n in _MONTH_WORDS for w in words.split()}
_MONTH_RX = "|".join(sorted((re.escape(w) for w in _MONTHS), key=len, reverse=True))
_DMY_WORDS = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th|er|\.|º)?\s*(?:de\s+)?(" + _MONTH_RX + r")\.?(?![^\W\d_])(?:,?\s*(?:de\s+)?(\d{4}))?", re.I)
_MDY_WORDS = re.compile(r"(?<![^\W\d_])(" + _MONTH_RX + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(\d{4}))?", re.I)
_TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])(?:[:h.]([0-5]\d))?\s*(am|pm|a\.m\.|p\.m\.|uhr|h\b)?(?![\d%])", re.I)
# countries with one civil time zone: a wall-clock time there is local time, not UTC
REGION_TZ = {"GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin", "PL": "Europe/Warsaw", "NL": "Europe/Amsterdam",
             "FR": "Europe/Paris", "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "IT": "Europe/Rome", "BE": "Europe/Brussels",
             "AT": "Europe/Vienna", "CH": "Europe/Zurich", "CZ": "Europe/Prague", "GR": "Europe/Athens", "TR": "Europe/Istanbul",
             "GE": "Asia/Tbilisi", "AZ": "Asia/Baku", "AM": "Asia/Yerevan", "AE": "Asia/Dubai", "SA": "Asia/Riyadh", "EG": "Africa/Cairo",
             "IN": "Asia/Kolkata", "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
             "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "IL": "Asia/Jerusalem", "UA": "Europe/Kyiv", "BY": "Europe/Minsk"}


def _tz(country: str | None):
    name = REGION_TZ.get((country or "").upper())
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # no tz database on this host
            return None
    return None


def _time_of(text: str) -> tuple[int, int] | None:
    """A wall-clock time in what is left of the text after the date: '18:30', '6pm', '6.30 p.m.', '18h30', '19 Uhr'."""
    for m in _TIME.finditer(text):
        h, mi, suf = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        if not m.group(2) and not suf:
            continue  # a bare number is not a time
        if suf in ("pm",) and h < 12:
            h += 12
        elif suf == "am" and h == 12:
            h = 0
        if suf in ("am", "pm") and int(m.group(1)) > 12:
            continue
        return h, mi
    return None


def _infer_year(mo: int, da: int, now: dt.datetime) -> int:
    """A date without a year ('Thu 16 Oct') is the next such day, unless it was within the last 60 days."""
    for y in (now.year, now.year + 1, now.year - 1):
        try:
            d = dt.date(y, mo, da)
        except ValueError:
            continue
        if -60 <= (d - now.date()).days <= 305:
            return y
    return now.year


def _finish(y: int, mo: int, da: int, rest: str, country: str | None, date_only: bool) -> str | None:
    try:
        d = dt.date(y, mo, da)
    except ValueError:
        return None
    if date_only:
        return d.isoformat()
    tm = _time_of(rest)
    if tm is None:
        return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).isoformat()
    tz = _tz(country) or dt.timezone.utc
    return dt.datetime(d.year, d.month, d.day, tm[0], tm[1], tzinfo=tz).isoformat()


def parse_datetime(value: Any, country: str | None = None, now: dt.datetime | None = None, date_only: bool = False) -> str | None:
    """ISO strings, epochs, numeric dates (dd.mm.yyyy, mm/dd for the US), dates with month names in several languages
    ('Tue, 14 Oct 2026, 18:30', 'Oct 14, 2026 6pm', '15 października 2026'), CJK dates and relative words.
    A wall-clock time without an offset is local time in the row's country when that country has one time zone;
    a date without a time stays midnight UTC."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value) / (1000 if value > 1e11 else 1)
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        return d.date().isoformat() if date_only else d.isoformat()
    t = str(value).strip().translate(_DIGITS)
    low = t.lower()
    for word, days in _REL.items():
        if re.search(r"(?<![^\W\d_])" + re.escape(word) + r"(?![^\W\d_])", low):
            d = now - dt.timedelta(days=days)
            return d.date().isoformat() if date_only else d.replace(microsecond=0).isoformat()
    m = re.search(r"(\d+)\s*(minute|min|hour|hr|stunde|hora|час|day|tag|día|dia|jour|день|дн|week|woche|semana|недел|month|monat|mes|месяц)\w*\s*(ago|назад|zurück)?", low)
    if m and (m.group(3) or re.search(r"\b(hace|vor|il y a|há)\b", low)):
        n = int(m.group(1))
        unit = m.group(2)
        delta = (dt.timedelta(minutes=n) if unit.startswith("min") else dt.timedelta(hours=n) if unit in ("hour", "hr", "stunde", "hora", "час")
                 else dt.timedelta(weeks=n) if unit in ("week", "woche", "semana", "недел") else dt.timedelta(days=30 * n) if unit in ("month", "monat", "mes", "месяц")
                 else dt.timedelta(days=n))
        d = now - delta
        return d.date().isoformat() if date_only else d.replace(microsecond=0).isoformat()
    try:
        d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if d.tzinfo is None and not date_only:
            has_time = bool(re.search(r"\d[T ]\d{1,2}:\d{2}", t))
            d = d.replace(tzinfo=(_tz(country) if has_time else None) or dt.timezone.utc)
        return d.date().isoformat() if date_only else d.isoformat()
    except ValueError:
        pass
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return _finish(int(m.group(1)), int(m.group(2)), int(m.group(3)), t[m.end():], country, date_only)
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", t)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        if (country or "").upper() in ("US", "PH", "FM", "PR") and "/" in m.group(0):
            mo, da = a, b
        else:
            da, mo = a, b
        if mo > 12 and da <= 12:
            mo, da = da, mo
        return _finish(y, mo, da, t[m.end():], country, date_only)
    m2 = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日|(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", t)
    if m2:
        g = [x for x in m2.groups() if x]
        return _finish(int(g[0]), int(g[1]), int(g[2]), t[m2.end():], country, date_only)
    hits = [(m, "dmy") for m in _DMY_WORDS.finditer(t)] + [(m, "mdy") for m in _MDY_WORDS.finditer(t)]
    if hits:
        # prefer a match that carries a year, then the earliest one
        m, kind = sorted(hits, key=lambda h: (h[0].group(3) is None, h[0].start()))[0]
        if kind == "dmy":
            da, mo = int(m.group(1)), _MONTHS[m.group(2).lower()]
        else:
            mo, da = _MONTHS[m.group(1).lower()], int(m.group(2))
        y = int(m.group(3)) if m.group(3) else _infer_year(mo, da, now)
        return _finish(y, mo, da, t[:m.start()] + " " + t[m.end():], country, date_only)
    return None


# ------------------------------------------------------------------------------------ misc
_TAG = re.compile(r"</?[A-Za-z!?][^>]*>")  # a tag starts with a letter, / or !: "1 < 2 and 3 > 2" is text
_SCRIPT = re.compile(r"<(script|style|noscript)\b[^>]*>.*?(</\1\s*>|$)", re.S | re.I)  # dropped with their content


def clean_text(value: Any, limit: int = 20000) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v is not None)
    t = html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", str(value))))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] if t else None


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    t = str(value).strip().lower()
    if t in ("1", "true", "yes", "y", "si", "sí", "sim", "ja", "oui", "да", "はい", "예", "on", "remote"):
        return True
    if t in ("0", "false", "no", "n", "nein", "non", "não", "нет", "いいえ", "아니오", "off"):
        return False
    return None


def parse_url(value: Any, base: str | None = None) -> str | None:
    if not value:
        return None
    u = str(value).strip()
    if u.startswith("//"):
        u = "https:" + u
    if base:
        u = urljoin(base, u)
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return u


def parse_enum(value: Any, spec: dict) -> str | None:
    if value is None:
        return None
    t = clean_text(value) or ""
    low = t.lower()
    allowed = spec.get("enum") or []
    if low in allowed:
        return low
    low_us = low.replace("-", "_").replace(" ", "_")
    if low_us in allowed:
        return low_us
    syn = spec.get("synonyms") or {}
    if low in syn:
        return syn[low]
    for key in sorted(syn, key=len, reverse=True):
        if re.search(r"(?<![\w])" + re.escape(key) + r"(?![\w])", low):
            return syn[key]
    for a in allowed:
        if re.search(r"\b" + re.escape(a.replace("_", " ")) + r"\b", low):
            return a
    return "other" if "other" in allowed else None


def country_code(value: Any) -> str | None:
    if not value:
        return None
    try:
        codes = regions.resolve(str(value))
        return codes[0] if len(codes) == 1 else None
    except regions.RegionError:
        return None


def get_path(obj: Any, path: str) -> Any:
    """Dotted path into nested dicts/lists: 'offers.price', 'images.0.url'."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


# --------------------------------------------------------------------------------- records
class Normalizer:
    """Turn a raw fetched row into a clean record, or a quarantine reason."""

    def __init__(self, template: dict, *, source_id: str, base_url: str | None = None, region: str | None = None,
                 default_currency: str | None = None, report_currency: str = "USD", fx: FxTable | None = None,
                 field_map: dict | None = None, regions_: list[str] | None = None, project_regions: list[str] | None = None,
                 now: dt.datetime | None = None):
        self.t = template
        self.fields = template["fields"]
        self.source_id = source_id
        self.base_url = base_url
        self.region = region if region and region != regions.GLOBAL else None
        # every region the source covers; with more than one, a row's country is read from the row (country, then a city
        # the gazetteer places in exactly one of them), never assumed to be the first region
        self.regions = [r for r in dict.fromkeys(regions_ or ([self.region] if self.region else [])) if r and r != regions.GLOBAL]
        self._explicit_currency = default_currency
        # the project's regions: a row placed in another country is outside the dataset (GLOBAL or none: no check)
        pr = [r for r in (project_regions or []) if r]
        self.project_regions = None if not pr or regions.GLOBAL in pr else set(pr)
        self.now = now
        self.default_currency = default_currency or (regions.info(self.region)["currency"] if self.region else None)
        self.report_currency = (report_currency or "USD").upper()
        self.fx = fx or FxTable()
        self.field_map = field_map or {}

    def _raw(self, row: dict, name: str) -> Any:
        src = self.field_map.get(name)
        if src:
            if src.startswith("="):
                return src[1:]
            return get_path(row, src)
        return row.get(name)

    def row_region(self, row: dict) -> tuple[str | None, str | None]:
        """(country, default currency) for one row."""
        if len(self.regions) <= 1:
            return self.region, self.default_currency
        cc = None
        if "country" in self.fields:
            cc = country_code(self._raw(row, "country"))
        if not cc:
            for name in ("city", "location", "address"):
                raw = self._raw(row, name) if name in self.fields else None
                if not raw:
                    continue
                parts = [str(raw)] + [x.strip() for x in re.split(r"[,|/·\-–]", str(raw)) if x.strip()]
                hits = {r for r in self.regions for part in parts if geo.lookup(part, r)}
                if len(hits) == 1:
                    cc = hits.pop()
                    break
        currencies = {regions.info(r)["currency"] for r in self.regions}
        if cc:
            return cc, (self._explicit_currency or (regions.info(cc)["currency"] if cc in regions.COUNTRIES else self.default_currency))
        return None, self._explicit_currency or (currencies.pop() if len(currencies) == 1 else None)

    def normalize(self, row: dict) -> tuple[dict | None, list[str], list[str]]:
        """-> (record or None, errors (quarantine reasons), warnings)."""
        if not isinstance(row, dict):
            return None, ["row is not an object"], []
        region, dcur = self.row_region(row)
        rec: dict[str, Any] = {}
        errors: list[str] = []
        warnings: list[str] = []
        units: dict[str, str] = {}
        unmapped: dict[str, Any] = {}
        # currencies first so money fields can use them
        for name, f in self.fields.items():
            if f["type"] == "currency":
                v = self._raw(row, name)
                code = str(v).strip().upper() if v else None
                if code and code not in ISO_CURRENCIES:
                    code = detect_currency(str(v), None)
                rec[name] = code
        for name, f in self.fields.items():
            ftype = f["type"]
            if ftype == "currency":
                continue
            raw = self._raw(row, name)
            val: Any = None
            try:
                if ftype in ("string",):
                    val = clean_text(raw, 1000)
                    if val and f.get("upper"):
                        val = val.upper().replace(" ", "")
                elif ftype == "text":
                    val = clean_text(raw, 20000)
                elif ftype == "integer":
                    val = parse_int(raw)
                elif ftype == "number":
                    val = parse_number(raw)
                elif ftype == "money":
                    cf = f.get("currency_field")
                    amount, cur = parse_money(raw, rec.get(cf) if cf else None)
                    if cur is None and amount is not None:
                        cur = dcur
                    val = amount
                    if cf and amount is not None:
                        if rec.get(cf) and cur and rec[cf] != cur:
                            warnings.append(f"{name}: currency {cur} in text differs from {cf}={rec[cf]}; kept {rec[cf]}")
                        rec[cf] = rec.get(cf) or cur
                elif ftype == "area":
                    val, unit = parse_area(raw, self._raw(row, name + "_unit"))
                    if raw is not None and val is None and unit:
                        warnings.append(f"{name}: unit {unit} is not converted (regional definitions vary)")
                    units[name] = unit or ""
                elif ftype == "distance":
                    val, unit = parse_distance(raw, self._raw(row, name + "_unit"))
                    units[name] = unit or ""
                elif ftype == "weight":
                    val, unit = parse_weight(raw, self._raw(row, name + "_unit"))
                elif ftype == "date":
                    val = parse_datetime(raw, region, date_only=True)
                elif ftype == "datetime":
                    val = parse_datetime(raw, region)
                elif ftype == "url":
                    val = parse_url(raw, self.base_url)
                elif ftype == "enum":
                    val = parse_enum(raw, f)
                    if val in ("other", None) and raw not in (None, "") and str(raw).strip().lower() != "other":
                        unmapped[name] = clean_text(raw, 200)  # kept so synonyms can be extended later
                elif ftype == "boolean":
                    val = parse_bool(raw)
                elif ftype == "period":
                    val = parse_period(raw, None)
                elif ftype == "country":
                    val = country_code(raw) or (region if raw is None else None)
                elif ftype == "list":
                    val = raw if isinstance(raw, list) else ([x.strip() for x in str(raw).split(",") if x.strip()] if raw else None)
                elif ftype == "object":
                    val = raw if isinstance(raw, dict) else None
            except Exception as exc:  # a parser bug costs the field, never the row
                warnings.append(f"{name}: {exc.__class__.__name__}")
                val = None
            if raw not in (None, "", [], {}) and val is None and ftype not in ("period", "country"):
                warnings.append(f"{name}: could not parse {str(raw)[:60]!r}")
            rec[name] = val
        if "price_basis" in self.fields and rec.get("price_basis") is None and isinstance(self._raw(row, "price"), str):
            rec["price_basis"] = price_basis(self._raw(row, "price"))
        # one canonical city name per country (Lisboa -> Lisbon, Тбилиси -> Tbilisi); the site's wording stays in extra
        if "city" in self.fields and rec.get("city"):
            canon = geo.canonical_city(rec["city"], rec.get("country") or region)
            if canon != rec["city"]:
                unmapped["city"] = rec["city"]
                rec["city"] = canon
        # source_id falls back to the URL
        if not rec.get("source_id") and rec.get("url"):
            rec["source_id"] = rec["url"]
        # periods
        for p in self.t.get("periods") or []:
            period = rec.get(p["field"])
            for a in p["amounts"]:  # '950 €/mês' carries its own period
                if period is None and isinstance(self._raw(row, a), str):
                    period = parse_period(self._raw(row, a), None)
            if period is None and all(rec.get(a) is None for a in p["amounts"]):
                rec[p["field"]] = None  # no amount, no period: a default here would claim a salary/rent period nobody stated
                for a in p["amounts"]:
                    rec[a + p["suffix"]] = None
                continue
            period = period or p.get("default")
            rec[p["field"]] = period
            for a in p["amounts"]:
                rec[a + p["suffix"]] = convert_period(rec.get(a), period, p["canonical"])
        # FX: every money field gets <field>_report in the project's report currency
        for name, f in self.fields.items():
            if f["type"] == "money":
                cur = rec.get(f.get("currency_field") or "") or dcur
                rec[name + "_report"] = _round(self.fx.convert(rec.get(name), cur, self.report_currency))
        # required
        for name, f in self.fields.items():
            if f.get("required") and rec.get(name) in (None, ""):
                errors.append(f"missing required field {name}")
        # bounds and patterns
        for name, f in self.fields.items():
            v = rec.get(name)
            if v is None:
                continue
            if f.get("pattern") and isinstance(v, str) and not re.search(f["pattern"], v):
                warnings.append(f"{name}: {v!r} does not match the expected pattern; dropped")
                rec[name] = None
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = f.get("min"), f.get("max")
                for b in f.get("bounds_when") or []:
                    if all(rec.get(k) == want for k, want in b["when"].items()):
                        lo = b.get("min", lo)
                        hi = b.get("max", hi)
                if lo is not None and (v < lo or (f.get("exclusive_min") and v == lo)):
                    errors.append(f"{name}={v} below the sanity bound {lo}")
                if hi is not None and v > hi:
                    errors.append(f"{name}={v} above the sanity bound {hi}")
                if f["type"] == "money" and (f.get("usd_min") or f.get("usd_max")):
                    cur = rec.get(f.get("currency_field") or "") or dcur
                    usd = self.fx.convert(v, cur, "USD")
                    if usd is not None and f.get("usd_min") and usd < f["usd_min"]:
                        errors.append(f"{name}={v} {cur} is below {f['usd_min']} USD")
                    if usd is not None and f.get("usd_max") and usd > f["usd_max"]:
                        errors.append(f"{name}={v} {cur} is above {f['usd_max']} USD")
        # a city the gazetteer places only in another country contradicts the stated country (a scraper that set the
        # country from the site's domain: arbeitnow.com tagged London jobs DE)
        if rec.get("city") and rec.get("country"):
            homes = geo.countries_of(rec["city"])
            if homes and rec["country"] not in homes:
                errors.append(f"city {rec['city']} is in {'/'.join(sorted(homes))}, not {rec['country']}")
        if self.project_regions and rec.get("country") and rec["country"] not in self.project_regions:
            errors.append(f"country {rec['country']} is outside the project's regions ({', '.join(sorted(self.project_regions))})")
        exp = self.t.get("expires")
        if exp:
            when = next((rec.get(f) for f in exp["fields"] if rec.get(f)), None)
            now = self.now or dt.datetime.now(dt.timezone.utc)
            if when and when[:10] < (now - dt.timedelta(days=exp.get("grace_days", 0))).date().isoformat():
                errors.append(f"{exp['reason']} ({when[:10]})")
        # category conflicts (rent ad in a sale category ...)
        for c in self.t.get("conflicts") or []:
            text = str(rec.get(c["field"]) or "")
            if text and re.search(c["pattern"], text, re.I) and not (c.get("unless") and re.search(c["unless"], text, re.I)):
                errors.append(c["reason"])
        # extra keys the template does not know are kept (bounded) for later mapping
        mapped_src = {v.split(".")[0] for v in self.field_map.values() if isinstance(v, str) and not v.startswith("=")}
        extra = {k: v for k, v in row.items() if k not in self.fields and k not in mapped_src and not k.endswith("_unit") and not k.startswith("_")}
        extra.update({f"{k}_raw": v for k, v in unmapped.items()})
        if extra:
            blob = json.dumps(extra, ensure_ascii=False, default=str)
            rec["extra"] = extra if len(blob) <= 4000 else {"_truncated": blob[:4000]}
        rec["source"] = self.source_id
        rec["country"] = rec.get("country") or region
        if not rec["country"] and len(self.regions) > 1:
            warnings.append(f"country: not stated and the city does not place the row in one of {'/'.join(self.regions)}; left empty")
        if errors:
            return None, errors, warnings
        return rec, [], warnings

    def _dedup_value(self, k: str, v: Any) -> Any:
        ftype = (self.fields.get(k) or {}).get("type")
        if not isinstance(v, str):
            return v
        if ftype == "datetime":
            return v[:10]  # by local date: one site gives '2026-10-14', another '2026-10-14T09:00:00+01:00'
        if ftype == "url":
            u = urlsplit(v.lower())  # 'https://greentech.ae' = 'http://www.greentech.ae/'
            return re.sub(r"^www\d*\.", "", u.netloc) + u.path.rstrip("/") + (("?" + u.query) if u.query else "")
        if k in ("name", "company", "title"):
            t = re.sub(r"[^\w\s]", " ", v.lower())
            t = re.sub(r"\s+", " ", t).strip()
            if k in ("name", "company"):  # 'Buildfy General Contracting LLC OPC' = 'Buildfy General Contracting L.L.C.'
                t = _LEGAL_SUFFIX.sub("", t).strip()
            return t
        return v

    def fingerprints(self, rec: dict) -> str | None:
        for keys in self.t.get("dedup") or []:
            vals = [self._dedup_value(k, rec.get(k)) for k in keys]
            if all(v not in (None, "") for v in vals):
                norm = "|".join(str(round(v, 1) if isinstance(v, float) else v).lower().strip() for v in vals)
                return hashlib.sha1(("/".join(keys) + ":" + norm).encode()).hexdigest()[:20]
        return None


_LEGAL_SUFFIX = re.compile(r"(?:\s+(?:l\s?l\s?c|fz\s?e|fz\s?co|fz\s?llc|opc|est|establishment|co|company|ltd|limited|trading|wll|w\s?l\s?l|"
                           r"gmbh|ag|bv|b\s?v|nv|sp\s?z\s?o\s?o|pvt|private|inc|plc|sdn\s?bhd|bhd|pte|tbk|pt|s\s?a|sarl))+$")


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def record_uid(source_id: str, rec: dict) -> str:
    key = str(rec.get("source_id") or rec.get("url") or json.dumps(rec, sort_keys=True, default=str))
    return hashlib.sha1(f"{source_id}\x00{key}".encode()).hexdigest()
