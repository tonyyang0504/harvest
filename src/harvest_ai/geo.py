"""Location canonicalisation: one canonical name per city/region, keyed by ISO country.

`canonical_city("Lisboa", "PT") -> "Lisbon"`, `canonical_city("Тбилиси", "GE") -> "Tbilisi"`.

Sources, merged in this order (later ones add aliases, never remove):
  1. the small built-in gazetteer below (capitals and large cities of commonly collected markets);
  2. `HARVEST_GAZETTEER` files, `os.pathsep`-separated, either
     - JSON `{"PT": {"Lisbon": ["Lisboa", "Lisbonne"], ...}, ...}`, or
     - a GeoNames dump (`cities500.txt`, `cities15000.txt`, `allCountries.txt`: tab-separated
       geonameid, name, asciiname, alternatenames, …, country code at column 9). The GeoNames `name`
       becomes canonical and `asciiname` + `alternatenames` its aliases (CC BY 4.0, geonames.org).
Matching ignores case, accents, punctuation and common prefixes ("г.", "город", "city of"); an
unknown name is returned cleaned but unchanged.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

# canonical (English exonym where one is common) -> aliases in local languages/scripts
BUILTIN: dict[str, dict[str, list[str]]] = {
    "GE": {"Tbilisi": ["Tiflis", "Тбилиси", "თბილისი"], "Batumi": ["Батуми", "ბათუმი"], "Kutaisi": ["Кутаиси", "ქუთაისი"],
           "Rustavi": ["Рустави", "რუსთავი"], "Zugdidi": ["Зугдиди", "ზუგდიდი"], "Gori": ["Гори", "გორი"], "Poti": ["Поти", "ფოთი"],
           "Telavi": ["Телави", "თელავი"]},
    "AZ": {"Baku": ["Bakı", "Баку", "Bakı şəhəri"], "Ganja": ["Gəncə", "Гянджа"], "Sumgait": ["Sumqayıt", "Сумгаит"],
           "Mingachevir": ["Mingəçevir", "Мингечевир"], "Lankaran": ["Lənkəran", "Ленкорань"], "Shirvan": ["Şirvan", "Ширван"],
           "Nakhchivan": ["Naxçıvan", "Нахичевань"]},
    "KZ": {"Almaty": ["Алматы", "Алма-Ата", "Alma-Ata"], "Astana": ["Астана", "Nur-Sultan", "Нур-Султан", "Akmola"],
           "Shymkent": ["Шымкент", "Chimkent"], "Karaganda": ["Караганда", "Qaraghandy", "Қарағанды"], "Aktobe": ["Актобе", "Ақтөбе"],
           "Pavlodar": ["Павлодар"], "Ust-Kamenogorsk": ["Усть-Каменогорск", "Oskemen", "Өскемен"], "Atyrau": ["Атырау"]},
    "AM": {"Yerevan": ["Ереван", "Երևան"], "Gyumri": ["Гюмри", "Գյումրի"]},
    "RU": {"Moscow": ["Москва", "Moskva"], "Saint Petersburg": ["Санкт-Петербург", "St. Petersburg", "Sankt-Peterburg"],
           "Novosibirsk": ["Новосибирск"], "Yekaterinburg": ["Екатеринбург"], "Kazan": ["Казань"]},
    "UA": {"Kyiv": ["Kiev", "Київ", "Киев"], "Kharkiv": ["Kharkov", "Харків", "Харьков"], "Odesa": ["Odessa", "Одеса", "Одесса"],
           "Lviv": ["Lvov", "Львів", "Львов"], "Dnipro": ["Дніпро", "Днепр"]},
    "UZ": {"Tashkent": ["Toshkent", "Ташкент"], "Samarkand": ["Samarqand", "Самарканд"]},
    "PT": {"Lisbon": ["Lisboa", "Lisbonne", "Lissabon"], "Porto": ["Oporto"], "Braga": [], "Coimbra": [], "Faro": [], "Funchal": [],
           "Setubal": ["Setúbal"], "Aveiro": [], "Cascais": [], "Sintra": [], "Oeiras": [], "Loures": [], "Almada": [],
           "Vila Nova de Gaia": ["Gaia"], "Matosinhos": [], "Amadora": [], "Ponta Delgada": []},
    "ES": {"Madrid": [], "Barcelona": [], "Valencia": ["València"], "Seville": ["Sevilla"], "Zaragoza": ["Saragossa"], "Malaga": ["Málaga"],
           "Bilbao": ["Bilbo"], "San Sebastian": ["San Sebastián", "Donostia"], "A Coruna": ["A Coruña", "La Coruña"],
           "Palma": ["Palma de Mallorca"], "Alicante": ["Alacant"], "Cordoba": ["Córdoba"], "Valladolid": [], "Vigo": [], "Gijon": ["Gijón"],
           "Las Palmas": ["Las Palmas de Gran Canaria"], "Santa Cruz de Tenerife": [], "Granada": [], "Murcia": []},
    "FR": {"Paris": [], "Marseille": ["Marseilles"], "Lyon": ["Lyons"], "Toulouse": [], "Nice": [], "Nantes": [], "Strasbourg": [], "Bordeaux": []},
    "DE": {"Berlin": [], "Munich": ["München", "Muenchen"], "Cologne": ["Köln", "Koeln"], "Hamburg": [], "Frankfurt": ["Frankfurt am Main", "Frankfurt a.M."],
           "Nuremberg": ["Nürnberg", "Nuernberg"], "Dusseldorf": ["Düsseldorf", "Duesseldorf"], "Stuttgart": [], "Leipzig": [], "Dresden": [], "Hanover": ["Hannover"],
           "Bremen": [], "Essen": [], "Dortmund": [], "Bonn": [], "Karlsruhe": [], "Mannheim": [], "Aachen": [], "Heidelberg": [], "Darmstadt": [],
           "Wiesbaden": [], "Mainz": [], "Munster": ["Münster"], "Freiburg": ["Freiburg im Breisgau"], "Augsburg": [], "Potsdam": [], "Kiel": []},
    "IT": {"Rome": ["Roma"], "Milan": ["Milano"], "Naples": ["Napoli"], "Turin": ["Torino"], "Florence": ["Firenze"], "Venice": ["Venezia"],
           "Genoa": ["Genova"]},
    "PL": {"Warsaw": ["Warszawa"], "Krakow": ["Kraków", "Cracow"], "Wroclaw": ["Wrocław", "Breslau"], "Poznan": ["Poznań"], "Gdansk": ["Gdańsk", "Danzig"],
           "Lodz": ["Łódź"], "Katowice": [], "Gdynia": [], "Szczecin": [], "Lublin": [], "Bydgoszcz": [], "Bialystok": ["Białystok"], "Rzeszow": ["Rzeszów"],
           "Torun": ["Toruń"], "Gliwice": [], "Sopot": [], "Kielce": [], "Olsztyn": [], "Opole": [], "Bielsko-Biala": ["Bielsko-Biała"], "Tricity": ["Trójmiasto"]},
    "CZ": {"Prague": ["Praha", "Prag"], "Brno": []},
    "AT": {"Vienna": ["Wien"]},
    "BE": {"Brussels": ["Bruxelles", "Brussel"], "Antwerp": ["Antwerpen", "Anvers"]},
    "GR": {"Athens": ["Athina", "Αθήνα"], "Thessaloniki": ["Salonica", "Θεσσαλονίκη"]},
    "TR": {"Istanbul": ["İstanbul"], "Ankara": [], "Izmir": ["İzmir"], "Antalya": []},
    "AE": {"Dubai": ["دبي"], "Abu Dhabi": ["أبو ظبي", "أبوظبي", "Abudhabi"], "Sharjah": ["الشارقة", "Al Sharjah"], "Ajman": ["عجمان"],
           "Ras Al Khaimah": ["رأس الخيمة", "RAK", "Ras al-Khaimah"], "Fujairah": ["الفجيرة", "Al Fujairah"], "Umm Al Quwain": ["أم القيوين", "Umm al-Quwain"],
           "Al Ain": ["العين"]},
    "SA": {"Riyadh": ["الرياض", "Ar Riyadh", "Ar-Riyadh"], "Jeddah": ["جدة", "Jiddah", "Jedda"], "Dammam": ["الدمام", "Ad Dammam"],
           "Mecca": ["مكة", "مكة المكرمة", "Makkah"], "Medina": ["المدينة المنورة", "Madinah", "Al Madinah"], "Khobar": ["الخبر", "Al Khobar", "Al-Khobar", "Alkhobar"],
           "Dhahran": ["الظهران"], "Tabuk": ["تبوك"], "Abha": ["أبها"], "Taif": ["الطائف"], "Buraydah": ["بريدة"], "Jubail": ["الجبيل", "Al Jubail"],
           "Hofuf": ["الهفوف", "Al Hofuf", "Al-Ahsa", "الأحساء"], "Khamis Mushait": ["خميس مشيط"], "Hail": ["حائل", "Ha'il"], "Najran": ["نجران"],
           "Jazan": ["جازان", "Jizan"], "Yanbu": ["ينبع"], "Qatif": ["القطيف"]},
    "NL": {"Amsterdam": [], "Rotterdam": [], "The Hague": ["Den Haag", "'s-Gravenhage", "s-Gravenhage"], "Utrecht": [], "Eindhoven": [],
           "Groningen": [], "Tilburg": [], "Almere": [], "Breda": [], "Nijmegen": [], "Arnhem": [], "Haarlem": [], "Enschede": [], "Delft": [],
           "Leiden": [], "Maastricht": [], "Amersfoort": [], "Apeldoorn": [], "Zwolle": [], "Den Bosch": ["'s-Hertogenbosch", "s-Hertogenbosch"],
           "Hilversum": [], "Amstelveen": [], "Hoofddorp": [], "Schiphol": []},
    "IE": {"Dublin": ["Baile Átha Cliath"], "Cork": ["Corcaigh"], "Galway": ["Gaillimh"], "Limerick": ["Luimneach"], "Waterford": ["Port Láirge"],
           "Kilkenny": [], "Sligo": [], "Athlone": [], "Drogheda": [], "Dundalk": [], "Letterkenny": [], "Wexford": [], "Killarney": []},
    "SG": {"Singapore": ["新加坡", "Singapura", "சிங்கப்பூர்", "SG"]},
    "MY": {"Kuala Lumpur": ["KL", "K.L.", "吉隆坡", "Wilayah Persekutuan Kuala Lumpur", "WP Kuala Lumpur"], "Petaling Jaya": ["PJ"],
           "George Town": ["Georgetown"], "Johor Bahru": ["JB", "Johor Baharu", "新山"], "Shah Alam": [], "Subang Jaya": [],
           "Cyberjaya": [], "Putrajaya": [], "Ipoh": [], "Kota Kinabalu": ["KK"], "Kuching": [], "Malacca": ["Melaka"], "Seremban": [], "Klang": [],
           "Puchong": [], "Bangsar": [], "Mont Kiara": [], "Damansara": []},
    "ID": {"Jakarta": ["DKI Jakarta", "Daerah Khusus Ibukota Jakarta", "Jakarta Raya"], "South Jakarta": ["Jakarta Selatan", "Jaksel"],
           "Central Jakarta": ["Jakarta Pusat", "Jakpus"], "West Jakarta": ["Jakarta Barat", "Jakbar"], "East Jakarta": ["Jakarta Timur", "Jaktim"],
           "North Jakarta": ["Jakarta Utara", "Jakut"], "Surabaya": [], "Bandung": [], "Medan": [], "Semarang": [], "Makassar": ["Ujung Pandang"],
           "Palembang": [], "Tangerang": [], "South Tangerang": ["Tangerang Selatan", "Tangsel"], "Depok": [], "Bekasi": [], "Bogor": [],
           "Yogyakarta": ["Jogja", "Jogjakarta", "Yogya", "Daerah Istimewa Yogyakarta", "DIY"], "Denpasar": [], "Malang": [], "Batam": [],
           "Pekanbaru": [], "Balikpapan": [], "Surakarta": ["Solo"], "Padang": [], "Manado": []},
    "EG": {"Cairo": ["القاهرة"], "Alexandria": ["الإسكندرية"], "Giza": ["الجيزة"]},
    "JP": {"Tokyo": ["東京", "東京都"], "Osaka": ["大阪", "大阪市"], "Kyoto": ["京都", "京都市"], "Yokohama": ["横浜", "横浜市"], "Nagoya": ["名古屋", "名古屋市"]},
    "KR": {"Seoul": ["서울", "서울특별시"], "Busan": ["부산", "Pusan"], "Incheon": ["인천"]},
    "CN": {"Beijing": ["北京", "Peking"], "Shanghai": ["上海"], "Guangzhou": ["广州", "Canton"], "Shenzhen": ["深圳"]},
    "IN": {"Mumbai": ["Bombay", "मुंबई"], "Delhi": ["दिल्ली"], "New Delhi": ["नई दिल्ली"], "Bengaluru": ["Bangalore", "बेंगलुरु"], "Chennai": ["Madras"],
           "Kolkata": ["Calcutta"], "Hyderabad": [], "Pune": ["Poona"], "Ahmedabad": ["Amdavad"], "Gurugram": ["Gurgaon"], "Noida": [],
           "Ghaziabad": [], "Faridabad": [], "Navi Mumbai": [], "Thane": [], "Jaipur": [], "Lucknow": [], "Kochi": ["Cochin"],
           "Thiruvananthapuram": ["Trivandrum"], "Chandigarh": [], "Indore": [], "Bhopal": [], "Surat": [], "Vadodara": ["Baroda"],
           "Nagpur": [], "Coimbatore": [], "Visakhapatnam": ["Vizag"], "Patna": [], "Kanpur": [], "Mysuru": ["Mysore"], "Varanasi": ["Benares"]},
    "MX": {"Mexico City": ["Ciudad de México", "CDMX", "México D.F."], "Guadalajara": [], "Monterrey": []},
    "BR": {"Sao Paulo": ["São Paulo"], "Rio de Janeiro": [], "Brasilia": ["Brasília"], "Belo Horizonte": []},
    "US": {"New York": ["New York City", "NYC"], "Los Angeles": ["LA"], "San Francisco": ["SF"], "Washington": ["Washington, D.C.", "Washington DC"]},
    "GB": {"London": ["Greater London", "City of London"], "Manchester": [], "Edinburgh": [], "Glasgow": [], "Birmingham": [], "Leeds": [], "Bristol": [],
           "Liverpool": [], "Sheffield": [], "Newcastle upon Tyne": ["Newcastle"], "Nottingham": [], "Leicester": [], "Cambridge": [], "Oxford": [],
           "Cardiff": ["Caerdydd"], "Belfast": [], "Brighton": ["Brighton and Hove"], "Southampton": [], "Reading": [], "Milton Keynes": [], "Aberdeen": [],
           "Dundee": [], "York": [], "Bath": [], "Coventry": [], "Swansea": []},
}

_PREFIXES = re.compile(r"^(?:г\.|город|гор\.|city of|ciudad de|cidade de|ville de|stadt|şəhəri|q\.|к\.)\s*", re.I)


def key(name: str) -> str:
    """Comparison key: case-folded, accents and punctuation removed."""
    s = unicodedata.normalize("NFKD", str(name or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _PREFIXES.sub("", s.strip())
    s = re.sub(r"[\s\-_.,'’()/]+", " ", s).strip()
    return s


def _add(index: dict, country: str, canonical: str, aliases) -> None:
    table = index.setdefault(country.upper(), {})
    for n in [canonical, *aliases]:
        k = key(n)
        if k and k not in table:
            table[k] = canonical


def _load_file(index: dict, path: Path) -> None:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        for cc, cities in data.items():
            for canonical, aliases in cities.items():
                _add(index, cc, canonical, aliases or [])
        return
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(path, encoding="utf-8", newline="") as f:  # GeoNames: tab-separated, no header
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) < 9 or (len(row) > 6 and row[6] not in ("P", "A")):
                continue
            aliases = [row[2], *[a for a in row[3].split(",") if a]]
            _add(index, row[8], row[1], aliases)


@lru_cache(maxsize=1)
def _index() -> dict:
    index: dict = {}
    for cc, cities in BUILTIN.items():
        for canonical, aliases in cities.items():
            _add(index, cc, canonical, aliases)
    for part in (os.environ.get("HARVEST_GAZETTEER") or "").split(os.pathsep):
        if part and Path(part).is_file():
            _load_file(index, Path(part))
    return index


def reload() -> None:
    _index.cache_clear()


def _variants(cleaned: str) -> list[str]:
    """The name as given, then without a parenthetical qualifier or a postcode, then its first comma part:
    'Utrecht (NDW)' -> 'Utrecht', 'Bedford. MK45 2HZ' -> 'Bedford', 'Paris, Île-de-France, France' -> 'Paris'."""
    out = [cleaned]
    v = re.sub(r"\s*\([^)]*\)\s*", " ", cleaned).strip()
    v = re.sub(r"[\s.,]+(?:[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|\d{4,6}(?:-\d{3,4})?|\d{4}\s?[A-Z]{2})$", "", v).strip(" .,")
    if v and v not in out:
        out.append(v)
    first = re.split(r"\s*[,;|/·]\s*|\s+-\s+", v)[0].strip(" .")
    if first and first not in out:
        out.append(first)
    return out


def canonical_city(name: str | None, country: str | None) -> str | None:
    """The canonical name for `name` in `country`, or the cleaned input when unknown."""
    if not name:
        return name
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    idx = _index()
    for v in _variants(cleaned):
        k = key(v)
        if country and country.upper() in idx:
            hit = idx[country.upper()].get(k)
            if hit:
                return hit
        if not country:
            hits = {t[k] for t in idx.values() if k in t}
            if len(hits) == 1:
                return hits.pop()
    return cleaned


def countries_of(name: str | None) -> set[str]:
    """Every country whose gazetteer knows `name` (or its cleaned variants)."""
    if not name:
        return set()
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    idx = _index()
    for v in _variants(cleaned):
        k = key(v)
        hits = {cc for cc, t in idx.items() if k in t}
        if hits:
            return hits
    return set()


def lookup(name: str | None, country: str) -> str | None:
    """The canonical name when `name` is a known city of `country`, else None (no cleaned fallback)."""
    if not name or not country:
        return None
    t = _index().get(country.upper(), {})
    for v in _variants(re.sub(r"\s+", " ", str(name)).strip()):
        hit = t.get(key(v))
        if hit:
            return hit
    return None


def known(country: str) -> int:
    return len(set(_index().get(country.upper(), {}).values()))
