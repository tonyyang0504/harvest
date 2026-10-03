"""Record-schema templates: the fields a record type carries, their types, units, currency and
period rules, sanity bounds, cross-source dedup keys and category-conflict checks.

Field spec keys:
  type      string | text | integer | number | money | currency | area | distance | weight | date |
            datetime | url | enum | boolean | list | period | country | object
  required  the record is quarantined when the value is missing or unparseable
  unit      canonical unit after normalisation (area: sqm, distance: km, weight: kg)
  currency_field  (money) the field holding the ISO currency of this amount
  period_field    (money) the field holding the period the amount is quoted per
  enum, synonyms  allowed values and raw->canonical synonyms (any language, lower-case keys)
  min, max        sanity bounds on the normalised value; `when` limits a bound to matching rows
  usd_min, usd_max  bounds on the amount converted to USD (applied only when an FX rate exists)
  pattern         regex the normalised string must match
Template keys:
  periods   [{field, amounts, canonical, default, suffix}] -> derived `<amount><suffix>` fields
  dedup     list of field sets; rows equal on every field of a set across sources share a fingerprint
  conflicts [{field, pattern, reason}] -> rows whose field matches are quarantined
  expires   {fields, grace_days, reason} -> rows whose first present date field lies further in the past are quarantined
"""

from __future__ import annotations

import copy
import datetime as _dt

_YEAR = _dt.date.today().year

PERIODS = ["hour", "day", "week", "month", "year"]
PERIOD_SYNONYMS = {
    "h": "hour", "hr": "hour", "hour": "hour", "hourly": "hour", "per hour": "hour", "/h": "hour", "stunde": "hour", "hora": "hour", "час": "hour", "時間": "hour", "시간": "hour",
    "d": "day", "day": "day", "daily": "day", "night": "day", "nightly": "day", "per night": "day", "noche": "day", "nuit": "day", "tag": "day", "dia": "day", "día": "day",
    "сутки": "day", "день": "day", "日": "day", "泊": "day", "일": "day", "giorno": "day", "doba": "day",
    "w": "week", "wk": "week", "week": "week", "weekly": "week", "pw": "week", "p.w.": "week", "semana": "week", "semaine": "week", "woche": "week", "неделя": "week", "週": "week", "주": "week", "settimana": "week",
    "m": "month", "mo": "month", "mon": "month", "month": "month", "monthly": "month", "pm": "month", "p.m.": "month", "pcm": "month", "per month": "month", "/mo": "month", "mes": "month", "mês": "month",
    "mensual": "month", "mensal": "month", "mois": "month", "monat": "month", "monatlich": "month", "mese": "month", "мес": "month", "месяц": "month", "месяц.": "month", "月": "month", "월": "month", "miesiąc": "month", "ay": "month", "თვე": "month",
    "y": "year", "yr": "year", "year": "year", "yearly": "year", "annual": "year", "annum": "year", "pa": "year", "p.a.": "year", "año": "year", "ano": "year", "an": "year", "jahr": "year",
    "anno": "year", "год": "year", "年": "year", "년": "year", "rok": "year", "yıl": "year",
    # Dutch, Polish, German abbreviations, Indonesian / Malay, Arabic (trials 2026-10: NL/PL salaries, ID/MY prices, AE/SA)
    "uur": "hour", "per uur": "hour", "godz": "hour", "godz.": "hour", "godzinę": "hour", "std": "hour", "std.": "hour", "jam": "hour", "ساعة": "hour",
    "dag": "day", "dzień": "day", "hari": "day", "يوم": "day", "يومي": "day",
    "tydzień": "week", "minggu": "week", "أسبوع": "week",
    "maand": "month", "per maand": "month", "p/m": "month", "mies": "month", "mies.": "month", "miesięcznie": "month", "msc": "month", "mc": "month",
    "bulan": "month", "bln": "month", "sebulan": "month", "شهر": "month", "شهريا": "month", "شهرياً": "month", "monatl.": "month",
    "jaar": "year", "per jaar": "year", "rocznie": "year", "jährlich": "year", "tahun": "year", "thn": "year", "setahun": "year", "سنة": "year", "سنويا": "year", "سنوياً": "year",
}

_COMMON = {
    "source_id": {"type": "string", "required": True, "desc": "the site's stable id for the item (listing id, slug); falls back to the URL"},
    "url": {"type": "url", "required": True, "desc": "canonical detail URL"},
    "title": {"type": "string"},
    "posted_at": {"type": "datetime", "desc": "when the item was published, ISO-8601"},
    "country": {"type": "country", "desc": "ISO-3166 alpha-2"},
    "city": {"type": "string"},
    "location": {"type": "string", "desc": "free-text locality as shown"},
    "image": {"type": "url"},
    "description": {"type": "text"},
}

_SALE_WORDS = (r"\bfor\s+sale\b|\bsale\b|zu\s+verkaufen|\bverkauf|\bventa\b|\bse\s+vende|\bvendo\b|\bvende-se\b|\bvenda\b|\bkaufen\b|\bà\s+vendre\b|"
               r"продаж|продаётся|продается|للبيع|売買|出售|매매|satılık|vendita")
_RENT_WORDS = (r"\bfor\s+rent\b|\bto\s+let\b|\brent(al)?\b|zu\s+vermieten|\bmiete\b|\balquiler\b|\bse\s+alquila|\barrenda|"
               r"\barrendamento\b|\baluga-se\b|\bà\s+louer\b|\blocation\b|аренд|сда[её]тся|للإيجار|للايجار|賃貸|出租|임대|전세|월세|kiralık|affitto")
_RENT_TO_OWN = r"mietkauf|rent[\s-]to[\s-]own|alquiler\s+con\s+opci[oó]n|lease[\s-]to[\s-]own"

PROPERTY_TYPES = {"enum": ["apartment", "house", "land", "commercial", "room", "parking", "other"], "synonyms": {
    "flat": "apartment", "apartamento": "apartment", "apartament": "apartment", "wohnung": "apartment", "appartement": "apartment", "piso": "apartment",
    "квартира": "apartment", "condo": "apartment", "studio": "apartment", "penthouse": "apartment", "マンション": "apartment", "アパート": "apartment", "아파트": "apartment", "公寓": "apartment",
    "ბინა": "apartment", "moradia": "house", "casa": "house", "haus": "house", "maison": "house", "villa": "house", "дом": "house", "chalet": "house", "townhouse": "house", "一戸建て": "house",
    "terreno": "land", "grundstück": "land", "plot": "land", "участок": "land", "terrain": "land", "lote": "land",
    "office": "commercial", "shop": "commercial", "local": "commercial", "oficina": "commercial", "escritório": "commercial", "gewerbe": "commercial", "коммерческая": "commercial",
    "habitación": "room", "quarto": "room", "zimmer": "room", "комната": "room", "garage": "parking", "garaje": "parking", "garagem": "parking"}}

_RE_BASE = {
    **_COMMON,
    "property_type": {"type": "enum", **PROPERTY_TYPES},
    "price": {"type": "money", "required": True, "currency_field": "currency", "min": 0, "exclusive_min": True},
    "currency": {"type": "currency"},
    "area": {"type": "area", "unit": "sqm", "min": 3, "max": 1_000_000,
             "bounds_when": [{"when": {"property_type": "apartment"}, "max": 2000}, {"when": {"property_type": "room"}, "max": 300}]},
    "bedrooms": {"type": "integer", "min": 0, "max": 50},
    "bathrooms": {"type": "number", "min": 0, "max": 50},
    "rooms": {"type": "number", "min": 0, "max": 100},
    "floor": {"type": "integer", "min": -5, "max": 200},
    "year_built": {"type": "integer", "min": 1500, "max": _YEAR + 6},
    "latitude": {"type": "number", "min": -90, "max": 90},
    "longitude": {"type": "number", "min": -180, "max": 180},
    "furnished": {"type": "boolean"},
    # what `price` is quoted per: the whole unit, per area (psf, per m²) or per person (per seat, desk, pax). Derived from
    # the price text when the scraper does not set it (trials 2026-10: coworking 'From RM799 / Seat / Month', '$35 PSF')
    "price_basis": {"type": "enum", "enum": ["total", "per_area", "per_person"], "synonyms": {
        "unit": "total", "whole": "total", "psf": "per_area", "per sqft": "per_area", "per m2": "per_area", "per seat": "per_person",
        "per desk": "per_person", "per pax": "per_person", "per person": "per_person"}},
    "seller_type": {"type": "enum", "enum": ["agency", "developer", "private", "other"], "synonyms": {"agent": "agency", "broker": "agency", "owner": "private", "particular": "private", "privat": "private", "собственник": "private", "агентство": "agency"}},
}

TEMPLATES: dict[str, dict] = {
    "vehicles": {
        "label": "Vehicles for sale (cars, motorcycles, trucks)",
        "fields": {
            **_COMMON,
            "make": {"type": "string", "required": True},
            "model": {"type": "string"},
            "trim": {"type": "string"},
            "year": {"type": "integer", "min": 1900, "max": _YEAR + 1},
            "price": {"type": "money", "required": True, "currency_field": "currency", "min": 0, "exclusive_min": True, "usd_min": 100, "usd_max": 20_000_000},
            "currency": {"type": "currency"},
            "mileage": {"type": "distance", "unit": "km", "min": 0, "max": 2_000_000},
            "fuel_type": {"type": "enum", "enum": ["petrol", "diesel", "hybrid", "plugin_hybrid", "electric", "lpg", "cng", "hydrogen", "other"], "synonyms": {
                "gasoline": "petrol", "gas": "petrol", "benzin": "petrol", "benzine": "petrol", "essence": "petrol", "gasolina": "petrol", "бензин": "petrol", "ბენზინი": "petrol", "ガソリン": "petrol", "汽油": "petrol",
                "gasoil": "diesel", "gazole": "diesel", "дизель": "diesel", "gasóleo": "diesel", "dizel": "diesel", "ディーゼル": "diesel", "柴油": "diesel", "დიზელი": "diesel",
                "ev": "electric", "elektro": "electric", "eléctrico": "electric", "électrique": "electric", "электро": "electric", "hybride": "hybrid", "híbrido": "hybrid", "гибрид": "hybrid", "hybrid": "hybrid",
                "phev": "plugin_hybrid", "plug-in hybrid": "plugin_hybrid", "газ": "lpg", "autogas": "lpg", "gpl": "lpg", "glp": "lpg", "cng": "cng", "метан": "cng"}},
            "transmission": {"type": "enum", "enum": ["manual", "automatic", "cvt", "robot", "other"], "synonyms": {
                "mt": "manual", "stick": "manual", "manuell": "manual", "manuelle": "manual", "manual": "manual", "механика": "manual", "механическая": "manual", "мкпп": "manual",
                "at": "automatic", "auto": "automatic", "automatik": "automatic", "automatique": "automatic", "automático": "automatic", "automática": "automatic", "автомат": "automatic", "акпп": "automatic",
                "вариатор": "cvt", "variator": "cvt", "робот": "robot", "dct": "robot", "dsg": "robot", "tiptronic": "automatic"}},
            "body_type": {"type": "string"},
            "engine_size": {"type": "number", "min": 0.05, "max": 16, "desc": "litres"},
            "power_hp": {"type": "number", "min": 1, "max": 2500},
            "drive": {"type": "string"},
            "color": {"type": "string"},
            "condition": {"type": "enum", "enum": ["new", "used"], "synonyms": {"neu": "new", "nuevo": "new", "novo": "new", "новый": "new", "gebraucht": "used", "usado": "used", "occasion": "used", "с пробегом": "used", "б/у": "used"}},
            "vin": {"type": "string", "pattern": r"^[A-HJ-NPR-Z0-9]{17}$", "upper": True},
            "seller_type": {"type": "enum", "enum": ["dealer", "private", "other"], "synonyms": {"händler": "dealer", "concesionario": "dealer", "автосалон": "dealer", "дилер": "dealer", "particular": "private", "privat": "private", "частное лицо": "private", "собственник": "private"}},
        },
        "dedup": [["vin"], ["make", "model", "year", "mileage", "price"]],
    },
    "real_estate_sale": {
        "label": "Real estate for sale",
        "fields": {**_RE_BASE},
        "dedup": [["property_type", "area", "price", "city", "bedrooms"], ["property_type", "area", "price", "city"]],
        "conflicts": [{"field": "title", "pattern": _RENT_TO_OWN, "reason": "rent-to-own listing in a sale category"},
                      {"field": "title", "pattern": _RENT_WORDS, "unless": _SALE_WORDS, "reason": "rent listing in a sale category"}],
    },
    "real_estate_rent": {
        "label": "Real estate for rent (long-term)",
        "fields": {**_RE_BASE,
                   "price": {"type": "money", "required": True, "currency_field": "currency", "period_field": "rent_period", "min": 0, "exclusive_min": True},
                   "rent_period": {"type": "period", "default": "month"},
                   "deposit": {"type": "money", "currency_field": "currency", "min": 0},
                   "available_from": {"type": "date"}},
        "periods": [{"field": "rent_period", "amounts": ["price"], "canonical": "month", "default": "month", "suffix": "_per_month"}],
        # offices and shops have no bedrooms: without the second set they never got a fingerprint (trials 2026-10)
        "dedup": [["property_type", "area", "price", "city", "bedrooms"], ["property_type", "area", "price", "city"]],
        "conflicts": [{"field": "title", "pattern": _RENT_TO_OWN, "reason": "rent-to-own listing in a rent category"},
                      {"field": "title", "pattern": _SALE_WORDS, "unless": _RENT_WORDS, "reason": "sale listing in a rent category"}],
    },
    "rentals": {
        "label": "Short-term and item rentals (holiday stays, cars, equipment)",
        "fields": {**_COMMON,
                   "item_type": {"type": "string"},
                   "price": {"type": "money", "required": True, "currency_field": "currency", "period_field": "rent_period", "min": 0, "exclusive_min": True},
                   "currency": {"type": "currency"},
                   "rent_period": {"type": "period", "default": "day"},
                   "deposit": {"type": "money", "currency_field": "currency", "min": 0},
                   "capacity": {"type": "integer", "min": 0, "max": 1000},
                   "available_from": {"type": "date"},
                   "available_to": {"type": "date"},
                   "rating": {"type": "number", "min": 0, "max": 5},
                   "reviews_count": {"type": "integer", "min": 0},
                   "latitude": {"type": "number", "min": -90, "max": 90},
                   "longitude": {"type": "number", "min": -180, "max": 180}},
        "periods": [{"field": "rent_period", "amounts": ["price"], "canonical": "day", "default": "day", "suffix": "_per_day"}],
        "dedup": [["title", "city", "price"]],
    },
    "jobs": {
        "label": "Job postings",
        "fields": {**_COMMON,
                   "title": {"type": "string", "required": True},
                   "company": {"type": "string"},
                   "remote": {"type": "boolean"},
                   "employment_type": {"type": "enum", "enum": ["full_time", "part_time", "contract", "temporary", "internship", "freelance", "other"], "synonyms": {
                       "full-time": "full_time", "full time": "full_time", "fulltime": "full_time", "vollzeit": "full_time", "tiempo completo": "full_time", "полная занятость": "full_time", "cdi": "full_time",
                       "part-time": "part_time", "part time": "part_time", "teilzeit": "part_time", "medio tiempo": "part_time", "частичная занятость": "part_time",
                       "contractor": "contract", "cdd": "temporary", "temp": "temporary", "intern": "internship", "praktikum": "internship", "стажировка": "internship", "prácticas": "internship"}},
                   "salary_min": {"type": "money", "currency_field": "currency", "period_field": "salary_period", "min": 0},
                   "salary_max": {"type": "money", "currency_field": "currency", "period_field": "salary_period", "min": 0},
                   "currency": {"type": "currency"},
                   "salary_period": {"type": "period", "default": "year"},
                   "valid_through": {"type": "datetime"},
                   "category": {"type": "string"}},
        "periods": [{"field": "salary_period", "amounts": ["salary_min", "salary_max"], "canonical": "year", "default": "year", "suffix": "_annual"}],
        "dedup": [["title", "company", "city"]],
    },
    "products": {
        "label": "Products / e-commerce offers",
        "fields": {**_COMMON,
                   "title": {"type": "string", "required": True},
                   "price": {"type": "money", "required": True, "currency_field": "currency", "min": 0},
                   "currency": {"type": "currency"},
                   "original_price": {"type": "money", "currency_field": "currency", "min": 0},
                   "brand": {"type": "string"},
                   "sku": {"type": "string"},
                   "gtin": {"type": "string", "pattern": r"^\d{8,14}$"},
                   "category": {"type": "string"},
                   "availability": {"type": "enum", "enum": ["in_stock", "out_of_stock", "preorder", "unknown"], "synonyms": {
                       "instock": "in_stock", "in stock": "in_stock", "available": "in_stock", "https://schema.org/instock": "in_stock", "http://schema.org/instock": "in_stock", "auf lager": "in_stock", "en stock": "in_stock", "в наличии": "in_stock",
                       "outofstock": "out_of_stock", "out of stock": "out_of_stock", "sold out": "out_of_stock", "https://schema.org/outofstock": "out_of_stock", "agotado": "out_of_stock", "нет в наличии": "out_of_stock",
                       "preorder": "preorder", "https://schema.org/preorder": "preorder"}},
                   "rating": {"type": "number", "min": 0, "max": 5},
                   "reviews_count": {"type": "integer", "min": 0},
                   "seller": {"type": "string"},
                   "weight": {"type": "weight", "unit": "kg", "min": 0}},
        "dedup": [["gtin"], ["brand", "sku"]],
    },
    "events": {
        "label": "Events (concerts, conferences, exhibitions)",
        "fields": {**_COMMON,
                   "title": {"type": "string", "required": True},
                   "starts_at": {"type": "datetime", "required": True},
                   "ends_at": {"type": "datetime"},
                   "venue": {"type": "string"},
                   "address": {"type": "string"},
                   "online": {"type": "boolean"},
                   "price_min": {"type": "money", "currency_field": "currency", "min": 0},
                   "price_max": {"type": "money", "currency_field": "currency", "min": 0},
                   "currency": {"type": "currency"},
                   "organizer": {"type": "string"},
                   "category": {"type": "string"}},
        "dedup": [["title", "starts_at", "city"]],
        # an event that has ended is not a listing any more (trials 2026-10: shows back to 2009 were stored as current)
        "expires": {"fields": ["ends_at", "starts_at"], "grace_days": 1, "reason": "event already ended"},
    },
    "businesses": {
        "label": "Businesses / B2B leads (companies only, never private persons)",
        "fields": {**_COMMON,
                   "name": {"type": "string", "required": True},
                   "category": {"type": "string"},
                   "address": {"type": "string"},
                   "postal_code": {"type": "string"},
                   "phone": {"type": "string", "desc": "the business's published phone only"},
                   "website": {"type": "url"},
                   "registration_id": {"type": "string"},
                   "employees": {"type": "integer", "min": 0},
                   "rating": {"type": "number", "min": 0, "max": 5},
                   "reviews_count": {"type": "integer", "min": 0},
                   "latitude": {"type": "number", "min": -90, "max": 90},
                   "longitude": {"type": "number", "min": -180, "max": 180}},
        "dedup": [["registration_id", "country"], ["website"], ["name", "city"]],
    },
    "generic": {
        "label": "Anything else (title, url, optional price and date)",
        "fields": {**_COMMON,
                   "price": {"type": "money", "currency_field": "currency", "min": 0},
                   "currency": {"type": "currency"},
                   "category": {"type": "string"},
                   "attributes": {"type": "object"}},
        "dedup": [],
    },
}
ALIASES = {"cars": "vehicles", "used_cars": "vehicles", "vehicle": "vehicles", "real_estate": "real_estate_sale", "property": "real_estate_sale",
           "rent": "real_estate_rent", "rental_apartments": "real_estate_rent", "job": "jobs", "ecommerce": "products", "product": "products",
           "event": "events", "leads": "businesses", "business": "businesses", "companies": "businesses"}

KNOWN_TYPES = {"string", "text", "integer", "number", "money", "currency", "area", "distance", "weight", "date", "datetime",
               "url", "enum", "boolean", "list", "period", "country", "object"}


def canonical_type(record_type: str) -> str:
    rt = (record_type or "").strip().lower()
    rt = ALIASES.get(rt, rt)
    if rt not in TEMPLATES:
        raise ValueError(f"unknown record_type {record_type!r}; known: {sorted(TEMPLATES)}")
    return rt


def get(record_type: str, extra_fields: list | dict | None = None) -> dict:
    """The template for a project, with the project's extra wanted fields appended (string unless typed)."""
    rt = canonical_type(record_type)
    t = copy.deepcopy(TEMPLATES[rt])
    t["record_type"] = rt
    if isinstance(extra_fields, dict):
        items = extra_fields.items()
    else:
        items = [(f, None) for f in (extra_fields or [])]
    for name, spec in items:
        name = str(name).strip()
        if not name or name in t["fields"]:
            continue
        if not name.replace("_", "").isalnum():
            raise ValueError(f"field names are [A-Za-z0-9_]: {name!r}")
        spec = dict(spec or {"type": "string"})
        if spec.get("type") not in KNOWN_TYPES:
            raise ValueError(f"field {name}: unknown type {spec.get('type')!r}")
        t["fields"][name] = spec
    return t


def listing() -> list[dict]:
    return [{"record_type": k, "label": v["label"], "fields": sorted(v["fields"]),
             "required": sorted(n for n, f in v["fields"].items() if f.get("required"))} for k, v in TEMPLATES.items()]


def output_columns(template: dict) -> list[str]:
    """Stored columns: every field plus derived period and FX columns."""
    cols = list(template["fields"])
    for p in template.get("periods") or []:
        for a in p["amounts"]:
            cols.append(a + p["suffix"])
    for name, f in template["fields"].items():
        if f.get("type") == "money":
            cols.append(name + "_report")
    return cols
