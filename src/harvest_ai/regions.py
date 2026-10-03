"""Regions: ISO-3166-1 alpha-2 countries (name, default currency, main languages) and named groups.

`expand(["KZ", "ge", "EU", "Portugal"])` -> ordered unique ISO codes. `GLOBAL` stays a marker for
international sites and never expands.
"""

from __future__ import annotations

# code|name|currency|languages (ISO-639-1, most used first)
_RAW = """
AD|Andorra|EUR|ca,es,fr
AE|United Arab Emirates|AED|ar,en
AF|Afghanistan|AFN|ps,fa
AG|Antigua and Barbuda|XCD|en
AI|Anguilla|XCD|en
AL|Albania|ALL|sq
AM|Armenia|AMD|hy,ru
AO|Angola|AOA|pt
AR|Argentina|ARS|es
AS|American Samoa|USD|en
AT|Austria|EUR|de
AU|Australia|AUD|en
AW|Aruba|AWG|nl,pap
AZ|Azerbaijan|AZN|az,ru
BA|Bosnia and Herzegovina|BAM|bs,hr,sr
BB|Barbados|BBD|en
BD|Bangladesh|BDT|bn,en
BE|Belgium|EUR|nl,fr,de
BF|Burkina Faso|XOF|fr
BG|Bulgaria|BGN|bg
BH|Bahrain|BHD|ar,en
BI|Burundi|BIF|rn,fr
BJ|Benin|XOF|fr
BM|Bermuda|BMD|en
BN|Brunei|BND|ms,en
BO|Bolivia|BOB|es
BR|Brazil|BRL|pt
BS|Bahamas|BSD|en
BT|Bhutan|BTN|dz
BW|Botswana|BWP|en,tn
BY|Belarus|BYN|be,ru
BZ|Belize|BZD|en,es
CA|Canada|CAD|en,fr
CD|DR Congo|CDF|fr
CF|Central African Republic|XAF|fr
CG|Congo|XAF|fr
CH|Switzerland|CHF|de,fr,it
CI|Cote d'Ivoire|XOF|fr
CL|Chile|CLP|es
CM|Cameroon|XAF|fr,en
CN|China|CNY|zh
CO|Colombia|COP|es
CR|Costa Rica|CRC|es
CU|Cuba|CUP|es
CV|Cabo Verde|CVE|pt
CW|Curacao|ANG|nl,pap
CY|Cyprus|EUR|el,tr,en
CZ|Czechia|CZK|cs
DE|Germany|EUR|de
DJ|Djibouti|DJF|fr,ar
DK|Denmark|DKK|da
DM|Dominica|XCD|en
DO|Dominican Republic|DOP|es
DZ|Algeria|DZD|ar,fr
EC|Ecuador|USD|es
EE|Estonia|EUR|et,ru
EG|Egypt|EGP|ar
ER|Eritrea|ERN|ti,ar
ES|Spain|EUR|es,ca,eu,gl
ET|Ethiopia|ETB|am
FI|Finland|EUR|fi,sv
FJ|Fiji|FJD|en
FM|Micronesia|USD|en
FO|Faroe Islands|DKK|fo,da
FR|France|EUR|fr
GA|Gabon|XAF|fr
GB|United Kingdom|GBP|en
GD|Grenada|XCD|en
GE|Georgia|GEL|ka,ru
GF|French Guiana|EUR|fr
GG|Guernsey|GBP|en
GH|Ghana|GHS|en
GI|Gibraltar|GIP|en
GL|Greenland|DKK|kl,da
GM|Gambia|GMD|en
GN|Guinea|GNF|fr
GP|Guadeloupe|EUR|fr
GQ|Equatorial Guinea|XAF|es,fr
GR|Greece|EUR|el
GT|Guatemala|GTQ|es
GU|Guam|USD|en
GW|Guinea-Bissau|XOF|pt
GY|Guyana|GYD|en
HK|Hong Kong|HKD|zh,en
HN|Honduras|HNL|es
HR|Croatia|EUR|hr
HT|Haiti|HTG|fr,ht
HU|Hungary|HUF|hu
ID|Indonesia|IDR|id
IE|Ireland|EUR|en,ga
IL|Israel|ILS|he,ar
IM|Isle of Man|GBP|en
IN|India|INR|hi,en
IQ|Iraq|IQD|ar,ku
IR|Iran|IRR|fa
IS|Iceland|ISK|is
IT|Italy|EUR|it
JE|Jersey|GBP|en
JM|Jamaica|JMD|en
JO|Jordan|JOD|ar
JP|Japan|JPY|ja
KE|Kenya|KES|en,sw
KG|Kyrgyzstan|KGS|ky,ru
KH|Cambodia|KHR|km
KI|Kiribati|AUD|en
KM|Comoros|KMF|ar,fr
KN|Saint Kitts and Nevis|XCD|en
KP|North Korea|KPW|ko
KR|South Korea|KRW|ko
KW|Kuwait|KWD|ar
KY|Cayman Islands|KYD|en
KZ|Kazakhstan|KZT|kk,ru
LA|Laos|LAK|lo
LB|Lebanon|LBP|ar,fr
LC|Saint Lucia|XCD|en
LI|Liechtenstein|CHF|de
LK|Sri Lanka|LKR|si,ta,en
LR|Liberia|LRD|en
LS|Lesotho|LSL|en,st
LT|Lithuania|EUR|lt
LU|Luxembourg|EUR|lb,fr,de
LV|Latvia|EUR|lv,ru
LY|Libya|LYD|ar
MA|Morocco|MAD|ar,fr
MC|Monaco|EUR|fr
MD|Moldova|MDL|ro,ru
ME|Montenegro|EUR|sr
MG|Madagascar|MGA|mg,fr
MH|Marshall Islands|USD|en
MK|North Macedonia|MKD|mk
ML|Mali|XOF|fr
MM|Myanmar|MMK|my
MN|Mongolia|MNT|mn
MO|Macao|MOP|zh,pt
MQ|Martinique|EUR|fr
MR|Mauritania|MRU|ar
MT|Malta|EUR|mt,en
MU|Mauritius|MUR|en,fr
MV|Maldives|MVR|dv
MW|Malawi|MWK|en
MX|Mexico|MXN|es
MY|Malaysia|MYR|ms,en
MZ|Mozambique|MZN|pt
NA|Namibia|NAD|en
NC|New Caledonia|XPF|fr
NE|Niger|XOF|fr
NG|Nigeria|NGN|en
NI|Nicaragua|NIO|es
NL|Netherlands|EUR|nl
NO|Norway|NOK|no,nb
NP|Nepal|NPR|ne
NZ|New Zealand|NZD|en
OM|Oman|OMR|ar
PA|Panama|PAB|es
PE|Peru|PEN|es
PF|French Polynesia|XPF|fr
PG|Papua New Guinea|PGK|en
PH|Philippines|PHP|en,tl
PK|Pakistan|PKR|ur,en
PL|Poland|PLN|pl
PR|Puerto Rico|USD|es,en
PS|Palestine|ILS|ar
PT|Portugal|EUR|pt
PY|Paraguay|PYG|es
QA|Qatar|QAR|ar
RE|Reunion|EUR|fr
RO|Romania|RON|ro
RS|Serbia|RSD|sr
RU|Russia|RUB|ru
RW|Rwanda|RWF|rw,en,fr
SA|Saudi Arabia|SAR|ar
SB|Solomon Islands|SBD|en
SC|Seychelles|SCR|en,fr
SD|Sudan|SDG|ar
SE|Sweden|SEK|sv
SG|Singapore|SGD|en,zh,ms
SI|Slovenia|EUR|sl
SK|Slovakia|EUR|sk
SL|Sierra Leone|SLE|en
SM|San Marino|EUR|it
SN|Senegal|XOF|fr
SO|Somalia|SOS|so,ar
SR|Suriname|SRD|nl
SS|South Sudan|SSP|en
SV|El Salvador|USD|es
SY|Syria|SYP|ar
SZ|Eswatini|SZL|en
TD|Chad|XAF|fr,ar
TG|Togo|XOF|fr
TH|Thailand|THB|th
TJ|Tajikistan|TJS|tg,ru
TL|Timor-Leste|USD|pt
TM|Turkmenistan|TMT|tk,ru
TN|Tunisia|TND|ar,fr
TO|Tonga|TOP|en
TR|Turkey|TRY|tr
TT|Trinidad and Tobago|TTD|en
TW|Taiwan|TWD|zh
TZ|Tanzania|TZS|sw,en
UA|Ukraine|UAH|uk
UG|Uganda|UGX|en
US|United States|USD|en,es
UY|Uruguay|UYU|es
UZ|Uzbekistan|UZS|uz,ru
VC|Saint Vincent and the Grenadines|XCD|en
VE|Venezuela|VES|es
VG|British Virgin Islands|USD|en
VI|U.S. Virgin Islands|USD|en
VN|Vietnam|VND|vi
VU|Vanuatu|VUV|bi,en,fr
WS|Samoa|WST|sm,en
XK|Kosovo|EUR|sq,sr
YE|Yemen|YER|ar
ZA|South Africa|ZAR|en,af,zu
ZM|Zambia|ZMW|en
ZW|Zimbabwe|USD|en
"""

COUNTRIES: dict[str, dict] = {}
for _line in _RAW.strip().splitlines():
    _c, _n, _cur, _langs = _line.split("|")
    COUNTRIES[_c] = {"code": _c, "name": _n, "currency": _cur, "languages": _langs.split(",")}

_EU = "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
GROUPS: dict[str, list[str]] = {
    "EU": _EU,
    "EEA": _EU + ["IS", "LI", "NO"],
    "EUROZONE": [c for c in _EU if COUNTRIES[c]["currency"] == "EUR"],
    "DACH": ["DE", "AT", "CH"],
    "BENELUX": ["BE", "NL", "LU"],
    "NORDICS": ["DK", "FI", "IS", "NO", "SE"],
    "BALTICS": ["EE", "LV", "LT"],
    "IBERIA": ["ES", "PT"],
    "BALKANS": ["AL", "BA", "BG", "HR", "XK", "ME", "MK", "RO", "RS", "SI", "GR"],
    "CEE": ["BG", "CZ", "EE", "HR", "HU", "LT", "LV", "PL", "RO", "SI", "SK"],
    "UK_IE": ["GB", "IE"],
    "GCC": ["AE", "BH", "KW", "OM", "QA", "SA"],
    "MENA": ["AE", "BH", "DZ", "EG", "IQ", "IL", "JO", "KW", "LB", "LY", "MA", "OM", "PS", "QA", "SA", "SY", "TN", "YE", "IR", "TR"],
    "MAGHREB": ["DZ", "LY", "MA", "MR", "TN"],
    "LEVANT": ["IL", "JO", "LB", "PS", "SY"],
    "NORTH_AMERICA": ["US", "CA", "MX"],
    "CENTRAL_AMERICA": ["BZ", "CR", "SV", "GT", "HN", "NI", "PA"],
    "LATAM": ["AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "SV", "GT", "HN", "MX", "NI", "PA", "PY", "PE", "PR", "UY", "VE"],
    "ANDEAN": ["BO", "CO", "EC", "PE"],
    "MERCOSUR": ["AR", "BR", "PY", "UY"],
    "ANZ": ["AU", "NZ"],
    "ASEAN": ["BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "VN"],
    "EAST_ASIA": ["CN", "HK", "JP", "KR", "MO", "MN", "TW"],
    "SOUTH_ASIA": ["AF", "BD", "BT", "IN", "LK", "MV", "NP", "PK"],
    "CENTRAL_ASIA": ["KZ", "KG", "TJ", "TM", "UZ"],
    "CAUCASUS": ["AM", "AZ", "GE"],
    "CIS": ["AM", "AZ", "BY", "KZ", "KG", "MD", "RU", "TJ", "UZ"],
    "EAST_AFRICA": ["BI", "DJ", "ER", "ET", "KE", "RW", "SO", "SS", "TZ", "UG"],
    "WEST_AFRICA": ["BJ", "BF", "CV", "CI", "GM", "GH", "GN", "GW", "LR", "ML", "MR", "NE", "NG", "SN", "SL", "TG"],
    "SOUTHERN_AFRICA": ["BW", "LS", "NA", "SZ", "ZA", "ZM", "ZW", "MZ", "MW"],
    "G7": ["CA", "FR", "DE", "IT", "JP", "GB", "US"],
}
ALIASES = {"UK": "GB", "EL": "GR", "USA": "US", "UAE": "AE", "KSA": "SA"}
GLOBAL = "GLOBAL"
_BY_NAME = {v["name"].lower(): k for k, v in COUNTRIES.items()}
_BY_NAME.update({"czech republic": "CZ", "korea": "KR", "great britain": "GB", "england": "GB",
                 "united states of america": "US", "russian federation": "RU", "viet nam": "VN",
                 "turkiye": "TR", "türkiye": "TR", "ivory coast": "CI", "holland": "NL"})


class RegionError(ValueError):
    pass


def resolve(token: str) -> list[str]:
    """One token -> ISO codes. Raises RegionError for unknown tokens."""
    t = (token or "").strip()
    if not t:
        raise RegionError("empty region")
    up = t.upper().replace("-", "_").replace(" ", "_")
    if up == GLOBAL:
        return [GLOBAL]
    up = ALIASES.get(up, up)
    if up in COUNTRIES:
        return [up]
    if up in GROUPS:
        return list(GROUPS[up])
    code = _BY_NAME.get(t.lower())
    if code:
        return [code]
    raise RegionError(f"unknown region {token!r}: use ISO-3166 alpha-2 codes, a country name or a group ({', '.join(sorted(GROUPS))}, GLOBAL)")


def expand(tokens) -> list[str]:
    if isinstance(tokens, str):
        tokens = [x for x in tokens.replace(";", ",").split(",")]
    out: list[str] = []
    for tok in tokens:
        if not str(tok).strip():
            continue
        for c in resolve(str(tok)):
            if c not in out:
                out.append(c)
    if not out:
        raise RegionError("no regions given")
    return out


def info(code: str) -> dict:
    if code == GLOBAL:
        return {"code": GLOBAL, "name": "International", "currency": "USD", "languages": ["en"]}
    return COUNTRIES[code]


def languages_for(codes) -> list[str]:
    out: list[str] = []
    for c in codes:
        for lang in info(c)["languages"]:
            if lang not in out:
                out.append(lang)
    return out


def cctld(code: str) -> str | None:
    if code == GLOBAL:
        return None
    return ".uk" if code == "GB" else "." + code.lower()
