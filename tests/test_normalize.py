import datetime as dt

import pytest

from harvest_ai import templates
from harvest_ai.normalize import (
    FxTable,
    Normalizer,
    convert_period,
    detect_currency,
    parse_area,
    parse_datetime,
    parse_distance,
    parse_enum,
    parse_money,
    parse_number,
    parse_period,
    parse_weight,
    price_range,
    record_uid,
)


@pytest.mark.parametrize("text,expect", [
    ("1 250 000", 1250000), ("1 250 000 ₸", 1250000), ("1.250.000", 1250000), ("1,250,000", 1250000), ("12,34,567", 1234567),
    ("82,32", 82.32), ("1.872", 1872), ("1,250.50", 1250.5), ("1.250,50", 1250.5), ("1'250'000 CHF", 1250000),
    ("3.5万", 35000), ("1280万円 2LDK", 12800000), ("3億5000万円", 350000000), ("1억 2천만원", 120000000), ("2억", 200000000), ("5천만", 50000000),
    ("₹ 45 Lakh", 4500000), ("1.2 crore", 12000000), ("2 cr", 20000000), ("15k", 15000), ("1,5 млн", 1500000), ("150 тыс.", 150000),
    ("2.3 million", 2300000), ("15 mil", 15000), ("1,2 millones", 1200000), ("3 bn", 3e9), ("-5", -5), ("abc", None), ("", None), (None, None), (42, 42),
])
def test_parse_number(text, expect):
    assert parse_number(text) == expect


def test_currency_detection_and_money():
    assert detect_currency("5 000 000 ₸") == "KZT"
    assert detect_currency("25 000 ₾") == "GEL"
    assert detect_currency("$ 12,500", "MXN") == "MXN" and detect_currency("$ 12,500", "KZT") == "USD"
    assert detect_currency("¥ 3,000,000", "CNY") == "CNY" and detect_currency("¥ 3,000,000") == "JPY"
    assert detect_currency("950 kr", "NOK") == "NOK"
    assert detect_currency("R$ 45.000") == "BRL" and detect_currency("Rs 45,000", "LKR") == "LKR"
    assert detect_currency("1 200 EUR") == "EUR" and detect_currency("BMW 320 price 12000", "GEL") == "GEL"
    assert parse_money("€ 950 / mês") == (950.0, "EUR")
    assert parse_money({"price": "1200", "priceCurrency": "eur"}) == (1200.0, "EUR")
    assert parse_money("Free", "USD") == (0.0, "USD")
    assert parse_money(None, "USD") == (None, "USD")
    assert price_range("1 200 – 1 500 €") == (1200, 1500)


def test_fx_table():
    fx = FxTable({"KZT": 480, "GEL": 2.7, "EUR": 0.9})
    assert fx.convert(4800, "KZT", "USD") == pytest.approx(10)
    assert fx.convert(10, "EUR", "GEL") == pytest.approx(30)
    assert fx.convert(10, "XXX", "USD") is None and fx.convert(None, "EUR", "USD") is None
    assert FxTable.from_json({"base": "EUR", "rates": {"USD": 1.1}}).convert(11, "USD", "EUR") == pytest.approx(10)


def test_units():
    assert parse_area("82,32 m²") == (82.32, "sqm")
    assert parse_area("3-Zimmer | 67,25 m²")[0] == 67.25
    assert parse_area("1,250 sqft") == (116.13, "sqft")
    assert parse_area("30坪")[0] == pytest.approx(99.17)
    assert parse_area("25평")[0] == pytest.approx(82.64)
    assert parse_area("6 соток") == (600.0, "sotka")
    assert parse_area("2 ha") == (20000.0, "hectare")
    assert parse_area("54 кв.м")[0] == 54
    assert parse_area("5 marla") == (None, "marla")
    assert parse_area(70) == (70.0, "sqm") and parse_area(750, "sqft")[0] == pytest.approx(69.68)
    assert parse_distance("150 тыс. км") == (150000.0, "km")
    assert parse_distance("3.5万公里") == (35000.0, "km")
    assert parse_distance("60,000 miles")[0] == pytest.approx(96560.6)
    assert parse_distance(12000, "mi")[0] == pytest.approx(19312.1)
    assert parse_weight("2.5 lbs")[0] == pytest.approx(1.134, rel=1e-3) and parse_weight("500 g")[0] == 0.5


def test_periods_and_dates():
    assert parse_period("/mo") == "month" and parse_period("per week") == "week" and parse_period("950 €/mês") == "month"
    assert parse_period("за сутки") == "day" and parse_period("월세") is None and parse_period(None, "month") == "month"
    assert convert_period(100, "week", "month") == pytest.approx(433.33) and convert_period(50, "hour", "year") == 104000
    now = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
    assert parse_datetime("25.09.2026") == "2026-09-25T00:00:00+00:00"
    assert parse_datetime("09/25/2026", "US").startswith("2026-09-25") and parse_datetime("05/09/2026", "GB").startswith("2026-09-05")
    assert parse_datetime("вчера", now=now).startswith("2026-09-24")
    assert parse_datetime("3 days ago", now=now).startswith("2026-09-22")
    assert parse_datetime("hace 2 días", now=now).startswith("2026-09-23")
    assert parse_datetime("2026年9月20日").startswith("2026-09-20")
    assert parse_datetime(1758800000).startswith("2025-09-25")
    assert parse_datetime("2026-09-20", date_only=True) == "2026-09-20"
    assert parse_datetime("not a date") is None


def test_enums_with_synonyms():
    spec = templates.TEMPLATES["vehicles"]["fields"]["fuel_type"]
    assert parse_enum("Бензин", spec) == "petrol" and parse_enum("Gasolina", spec) == "petrol" and parse_enum("Diesel", spec) == "diesel"
    assert parse_enum("Plug-in hybrid", spec) == "plugin_hybrid" and parse_enum("steam", spec) == "other"


def _vehicle_norm(fx=None):
    return Normalizer(templates.get("vehicles"), source_id="s1", base_url="https://cars.example.kz/list", region="KZ",
                      fx=fx or FxTable({"KZT": 500, "GEL": 2.7}))


def test_normalizer_vehicle_row():
    rec, errs, warns = _vehicle_norm().normalize({"source_id": "a1", "url": "/cars/a1", "title": "Toyota Camry", "make": " Toyota ",
                                                  "year": "2018 г.", "price": "12 500 000 ₸", "mileage": "85 тыс. км", "fuel_type": "Бензин",
                                                  "transmission": "Автомат", "vin": "jtdkb20u093123456", "color": "white", "seller": "Auto Co"})
    assert errs == []
    assert rec["url"] == "https://cars.example.kz/cars/a1" and rec["make"] == "Toyota" and rec["year"] == 2018
    assert rec["price"] == 12500000 and rec["currency"] == "KZT" and rec["price_report"] == 25000
    assert rec["mileage"] == 85000 and rec["fuel_type"] == "petrol" and rec["transmission"] == "automatic"
    assert rec["vin"] == "JTDKB20U093123456" and rec["country"] == "KZ" and rec["extra"] == {"seller": "Auto Co"}


def test_normalizer_quarantines_bad_rows():
    n = _vehicle_norm()
    rec, errs, _ = n.normalize({"source_id": "x", "url": "https://c.kz/x", "make": "Lada", "price": "0"})
    assert rec is None and any("sanity bound" in e for e in errs)
    rec, errs, _ = n.normalize({"source_id": "x", "url": "https://c.kz/x", "price": "5000000"})
    assert rec is None and "missing required field make" in errs
    rec, errs, _ = n.normalize({"source_id": "x", "url": "https://c.kz/x", "make": "Lada", "price": "30", "year": 1850})
    assert rec is None and any("below 100 USD" in e for e in errs) and any("year" in e for e in errs)
    rec, errs, warns = n.normalize({"source_id": "x", "url": "https://c.kz/x", "make": "Lada", "price": "3 000 000", "vin": "short"})
    assert rec and rec["vin"] is None and any("pattern" in w for w in warns)
    assert n.normalize("nope")[0] is None


def test_normalizer_rent_periods_and_conflicts():
    n = Normalizer(templates.get("real_estate_rent"), source_id="r", region="PT", report_currency="EUR", fx=FxTable({"EUR": 0.9}))
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://x.pt/1", "title": "T2 arrendamento Lisboa", "price": "950 €/mês",
                                "area": "82,5 m²", "property_type": "Apartamento", "bedrooms": "2"})
    assert errs == [] and rec["rent_period"] == "month" and rec["price_per_month"] == 950 and rec["price_report"] == 950
    assert rec["area"] == 82.5 and rec["property_type"] == "apartment"
    rec, errs, _ = n.normalize({"source_id": "2", "url": "https://x.pt/2", "title": "Holiday flat", "price": "300", "rent_period": "per week"})
    assert rec["price_per_month"] == pytest.approx(1300) and rec["currency"] == "EUR"
    rec, errs, _ = n.normalize({"source_id": "3", "url": "https://x.pt/3", "title": "Apartamento T3 para venda", "price": "250000 €"})
    assert rec is None and "sale listing in a rent category" in errs
    rec, errs, _ = n.normalize({"source_id": "4", "url": "https://x.pt/4", "title": "Wohnung Mietkauf", "price": "900"})
    assert rec is None and any("rent-to-own" in e for e in errs)
    rec, errs, _ = n.normalize({"source_id": "5", "url": "https://x.pt/5", "title": "Big flat", "price": "900", "area": "8232 m²", "property_type": "flat"})
    assert rec is None and any("area" in e for e in errs)


def test_field_map_and_fingerprints():
    n = Normalizer(templates.get("vehicles"), source_id="s", region="GE", field_map={"make": "specs.brand", "price": "offer.amount", "currency": "=GEL"})
    rec, errs, _ = n.normalize({"source_id": "9", "url": "https://c.ge/9", "specs": {"brand": "BMW"}, "offer": {"amount": "25 000"}, "vin": "WBA3A5C51CF256985"})
    assert errs == [] and rec["make"] == "BMW" and rec["price"] == 25000 and rec["currency"] == "GEL"
    other = Normalizer(templates.get("vehicles"), source_id="t", region="GE")
    rec2, _, _ = other.normalize({"source_id": "z", "url": "https://d.ge/z", "make": "BMW", "price": "26000 GEL", "vin": "WBA3A5C51CF256985"})
    assert n.fingerprints(rec) == other.fingerprints(rec2) is not None
    assert record_uid("s", rec) != record_uid("t", rec2)


def test_unmapped_enum_values_are_kept():
    """Pilot: 'transmission: other' hid the site's actual wording, so synonyms could not be extended."""
    rec, errs, _ = _vehicle_norm().normalize({"source_id": "a", "url": "https://c.kz/a", "make": "BMW", "price": "3 000 000",
                                              "transmission": "Типтроник+", "fuel_type": "Бензин"})
    assert errs == [] and rec["transmission"] == "other" and rec["extra"] == {"transmission_raw": "Типтроник+"}


def test_clean_text_drops_scripts_and_keeps_comparisons():
    from harvest_ai.normalize import clean_text
    assert clean_text("<script>window.x=1</script>Hi <b>there</b><style>p{}</style>") == "Hi there"
    assert clean_text("a <SCRIPT type=x>never closed") == "a"
    assert clean_text("1 < 2 and 3 > 2") == "1 < 2 and 3 > 2"
    assert clean_text("<!-- c -->x<br/>y") == "x y"
    # an entity-encoded tag is text on the page, and stays text (the UI must escape it)
    assert clean_text("&lt;img src=x onerror=alert(1)&gt; ok") == "<img src=x onerror=alert(1)> ok"


# ---------------------------------------------------------------- trials 2026-10 (jobs DE/PL/NL, events GB/IE, leads AE/SA, phones IN/ID, offices SG/MY)
@pytest.mark.parametrize("text,expect", [
    ("Rp 2,5 jt", 2500000), ("Rp 850rb", 850000), ("3,2 juta", 3200000), ("150 ribu", 150000),
    ("٣٬٥٠٠", 3500), ("١٢٫٥", 12.5), ("۱۲۰۰", 1200),
])
def test_parse_number_trial_markets(text, expect):
    assert parse_number(text) == expect


def test_currency_glued_to_the_amount():
    """Trials 2026-10: 'RM799' parsed to no amount and 'Rp5.000.000' to 0.0 (the number regex refuses a digit after a letter)."""
    assert parse_money("RM799") == (799, "MYR") and parse_money("Rp5.000.000") == (5000000, "IDR")
    assert parse_money("USD1200") == (1200, "USD") and parse_money("Rs.500") == (500, "INR")
    assert parse_money("From RM799 / Seat / Month") == (799, "MYR") and parse_money("iPhone13 Rp 5 jt") == (5000000, "IDR")


def test_currency_trial_markets():
    assert parse_money("RM 2.80 per sq ft") == (2.8, "MYR")  # 'sq ft' is not Hungarian forint
    assert detect_currency("1,200 sq. ft. office", "SGD") == "SGD" and detect_currency("1500 Ft") == "HUF" and detect_currency("1500Ft") == "HUF"
    assert parse_money("٣٬٥٠٠ ريال") == (3500, "SAR") and detect_currency("٣٬٥٠٠ ريال", "QAR") == "QAR"
    assert parse_money("15,000 درهم") == (15000, "AED") and parse_money("Dhs 12,000") == (12000, "AED")
    assert detect_currency("950kr", "NOK") == "NOK"


@pytest.mark.parametrize("text,expect", [
    ("€ 4.000 - € 5.500 per maand", "month"), ("15 000 - 22 000 zł / mies. netto", "month"), ("120-160 zł/h netto", "hour"),
    ("RM 4,500 sebulan", "month"), ("Rp 5 jt/bulan", "month"), ("10 000 ريال شهريا", "month"), ("60k - 80k EUR p.a.", "year"),
    ("80.000 € brutto jährlich", "year"), ("5000 PLN/msc", "month"), ("€ 25/m²", None), ("€ 25/m2", None), ("S$ 4,500 /mo", "month"),
])
def test_periods_trial_markets(text, expect):
    assert parse_period(text) == expect


def test_dates_with_month_names_times_and_local_zones():
    now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    p = lambda s, c="GB": parse_datetime(s, c, now=now)  # noqa: E731
    assert p("Tue, 14 Oct 2026, 18:30") == "2026-10-14T18:30:00+01:00"  # BST
    assert p("Sat, Nov 8 · 10:00 AM", "IE") == "2026-11-08T10:00:00+00:00"  # back on GMT
    assert p("Wednesday 15th October 2026 6pm") == "2026-10-15T18:00:00+01:00"
    assert p("14 October 2026") == "2026-10-14T00:00:00+00:00" and p("Oct 14, 2026", "IE").startswith("2026-10-14")
    assert p("15.10.2026 18:00", "DE") == "2026-10-15T18:00:00+02:00"
    assert p("15 października 2026, 18:00", "PL") == "2026-10-15T18:00:00+02:00"
    assert p("3 mei 2027", "NL").startswith("2027-05-03") and p("12 Disember 2026 9.30 pagi", "MY") == "2026-12-12T09:30:00+08:00"
    assert p("Thu 16 Oct").startswith("2026-10-16") and p("2 Jan").startswith("2027-01-02")  # no year: the next such day
    assert p("2026-10-14T18:30:00") == "2026-10-14T18:30:00+01:00" and p("2026-10-14 09:00", "SG") == "2026-10-14T09:00:00+08:00"
    assert p("2026-10-14T18:30:00Z") == "2026-10-14T18:30:00+00:00" and p("2026-10-14T18:30:00", "BR") == "2026-10-14T18:30:00+00:00"
    assert p("Monday") is None and parse_datetime("14 Oct 2026", "GB", date_only=True) == "2026-10-14"
    assert p("hierarchy 2026-10-14") == "2026-10-14T00:00:00+00:00"  # 'hier' (fr: yesterday) inside a word is not a date


def test_normalizer_event_row_needs_a_parsed_start():
    n = Normalizer(templates.get("events"), source_id="e", region="GB", report_currency="GBP", fx=FxTable({"GBP": 0.75}))
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://x.uk/e/1", "title": "PyData London", "starts_at": "Tue, 14 Oct 2026, 18:30",
                                "city": "Greater London", "price_min": "Free"})
    assert errs == [] and rec["starts_at"] == "2026-10-14T18:30:00+01:00" and rec["city"] == "London" and rec["price_min"] == 0


def test_gazetteer_trial_markets():
    from harvest_ai import geo
    cases = [("Den Haag", "NL", "The Hague"), ("KL", "MY", "Kuala Lumpur"), ("Jakarta Selatan", "ID", "South Jakarta"), ("Gurgaon", "IN", "Gurugram"),
             ("Bangalore", "IN", "Bengaluru"), ("Breslau", "PL", "Wroclaw"), ("Al Khobar", "SA", "Khobar"), ("الرياض", "SA", "Riyadh"),
             ("Baile Átha Cliath", "IE", "Dublin"), ("Penang", "MY", "Penang"), ("New Delhi", "IN", "New Delhi")]
    for raw, cc, want in cases:
        assert geo.canonical_city(raw, cc) == want, raw


def test_multi_region_source_reads_the_country_from_the_row():
    """Trials 2026-10: a GB+IE source normalised every row with its first region, so a Dublin meetup became GB (and London
    time); a DE/PL/NL board priced Polish salaries in EUR."""
    n = Normalizer(templates.get("events"), source_id="m", region="GB", regions_=["GB", "IE"], fx=FxTable({"GBP": 0.75, "EUR": 0.88}))
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://m.example/1", "title": "Kafka meetup", "starts_at": "8 Oct 2026 18:00",
                                "city": "Dublin", "price_min": "10"})
    assert errs == [] and rec["country"] == "IE" and rec["currency"] == "EUR" and rec["starts_at"] == "2026-10-08T18:00:00+01:00"
    rec, _, _ = n.normalize({"source_id": "2", "url": "https://m.example/2", "title": "PyData", "starts_at": "8 Oct 2026 18:00",
                             "location": "Shoreditch, London", "price_min": "10"})
    assert rec["country"] == "GB" and rec["currency"] == "GBP"
    rec, _, warns = n.normalize({"source_id": "3", "url": "https://m.example/3", "title": "Online", "starts_at": "2026-10-08", "price_min": "10"})
    assert rec["country"] is None and rec["currency"] is None and rec["price_min_report"] is None and any(w.startswith("country") for w in warns)
    rec, _, _ = n.normalize({"source_id": "4", "url": "https://m.example/4", "title": "X", "starts_at": "2026-10-08", "country": "Ireland"})
    assert rec["country"] == "IE"
    jobs = Normalizer(templates.get("jobs"), source_id="w", region="NL", regions_=["NL", "DE", "PL"], fx=FxTable({"EUR": 0.88, "PLN": 3.85}))
    rec, _, _ = jobs.normalize({"source_id": "j", "url": "https://w.example/j", "title": "Java dev", "city": "Warszawa", "salary_min": "20 000"})
    assert rec["country"] == "PL" and rec["currency"] == "PLN" and rec["city"] == "Warsaw"
    rec, _, _ = jobs.normalize({"source_id": "k", "url": "https://w.example/k", "title": "Go dev", "salary_min": "60000"})
    assert rec["country"] is None and rec["currency"] is None  # EUR or PLN: unknown, not guessed
    eur = Normalizer(templates.get("jobs"), source_id="e", region="DE", regions_=["DE", "NL"])
    assert eur.normalize({"source_id": "z", "url": "https://e.example/z", "title": "Dev", "salary_min": "60000"})[0]["currency"] == "EUR"


def test_no_amount_no_default_period():
    """Trials 2026-10 (nofluffjobs): rows with a hidden salary got salary_period='year' from the default."""
    n = Normalizer(templates.get("jobs"), source_id="j", region="PL", fx=FxTable({"PLN": 3.85}))
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://j.pl/1", "title": "Java dev"})
    assert errs == [] and rec["salary_period"] is None and rec["salary_min_annual"] is None
    rec, _, _ = n.normalize({"source_id": "2", "url": "https://j.pl/2", "title": "Go dev", "salary_min": "15 000 zł / mies."})
    assert rec["salary_period"] == "month" and rec["salary_min_annual"] == 180000
    rec, _, _ = n.normalize({"source_id": "3", "url": "https://j.pl/3", "title": "Go dev", "salary_min": "180000"})
    assert rec["salary_period"] == "year"


def test_rows_outside_the_project_or_contradicting_their_city_are_quarantined():
    """Trials 2026-10: createwith/neventum stored Copenhagen, Las Vegas and Barcelona events in a GB/IE project; arbeitnow's
    module set country DE from the site's domain, so 33 London jobs were German."""
    n = Normalizer(templates.get("events"), source_id="c", region="GB", project_regions=["GB", "IE"],
                   now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    row = {"source_id": "1", "url": "https://c.example/1", "title": "GOTO Copenhagen", "starts_at": "2026-10-02", "country": "DK"}
    rec, errs, _ = n.normalize(row)
    assert rec is None and errs == ["country DK is outside the project's regions (GB, IE)"]
    rec, errs, _ = n.normalize({**row, "country": "Ireland", "city": "Dublin"})
    assert errs == [] and rec["country"] == "IE"
    jobs = Normalizer(templates.get("jobs"), source_id="a", region="DE", project_regions=["DE", "PL", "NL"])
    rec, errs, _ = jobs.normalize({"source_id": "j", "url": "https://a.example/j", "title": "PM", "country": "DE", "city": "London"})
    assert rec is None and "city London is in GB, not DE" in errs and any("outside" in e for e in errs) is False
    rec, errs, _ = jobs.normalize({"source_id": "k", "url": "https://a.example/k", "title": "Dev", "country": "DE", "city": "Springfield"})
    assert errs == [] and rec["city"] == "Springfield"  # unknown to the gazetteer: no claim either way
    glob = Normalizer(templates.get("events"), source_id="g", region="GB", project_regions=["GB", "GLOBAL"])
    assert glob.normalize({**row, "starts_at": "2026-12-01"})[1] == []


def test_ended_events_are_not_listings():
    n = Normalizer(templates.get("events"), source_id="e", region="IE", now=dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc))
    base = {"source_id": "1", "url": "https://e.example/1", "title": "Expo"}
    assert n.normalize({**base, "starts_at": "2009-05-01"})[1] == ["event already ended (2009-05-01)"]
    assert n.normalize({**base, "starts_at": "2026-09-28", "ends_at": "2026-10-02"})[1] == []  # still running
    assert n.normalize({**base, "starts_at": "2026-09-30T18:00:00+01:00"})[1] == []  # within the one-day grace
    assert n.normalize({**base, "starts_at": "2026-09-24", "ends_at": "2026-09-24"})[1] == ["event already ended (2026-09-24)"]


def test_event_fingerprints_match_across_date_only_and_timed_sources():
    a = Normalizer(templates.get("events"), source_id="a", region="GB", now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    b = Normalizer(templates.get("events"), source_id="b", region="GB", now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    ra, _, _ = a.normalize({"source_id": "1", "url": "https://a.example/1", "title": "PyData London", "starts_at": "2026-10-14", "city": "London"})
    rb, _, _ = b.normalize({"source_id": "x", "url": "https://b.example/x", "title": "PyData London ", "starts_at": "Tue, 14 Oct 2026, 18:30",
                            "city": "Greater London"})
    assert a.fingerprints(ra) == b.fingerprints(rb) is not None


def test_city_cleaning_variants():
    from harvest_ai import geo
    assert geo.canonical_city("Utrecht (NDW)", "NL") == "Utrecht" and geo.canonical_city("Paris, Île-de-France, France", "FR") == "Paris"
    assert geo.canonical_city("Bedford. MK45 2HZ", "GB") == "Bedford. MK45 2HZ"  # not in the gazetteer: kept as given
    assert geo.canonical_city("London EC2A 4NE", "GB") == "London" and geo.canonical_city("Den Haag (Zuid-Holland)", "NL") == "The Hague"
    assert geo.countries_of("München") == {"DE"} and geo.countries_of("Nowhere") == set()
    assert geo.countries_of("Belfast") == {"GB"} and geo.canonical_city("Berlin; Munich; Remote", "DE") == "Berlin"


def test_price_basis_marks_per_seat_and_per_area_rents():
    """Trials 2026-10: coworking rows 'From RM799 / Seat / Month' and agency rows '$35.00 PSF' were stored as if they were
    the rent of the whole unit."""
    from harvest_ai.normalize import price_basis
    assert price_basis("From RM799 / Seat / Month") == "per_person" and price_basis("RM600/pax/mo") == "per_person"
    assert price_basis("S$35.00 PSF") == "per_area" and price_basis("RM 2.80 per sq ft") == "per_area" and price_basis("€ 25/m²") == "per_area"
    assert price_basis("S$ 14,000 /mo") is None and price_basis("RM 4,500 sebulan") is None
    n = Normalizer(templates.get("real_estate_rent"), source_id="o", region="MY", fx=FxTable({"MYR": 4.08}))
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://o.my/1", "title": "Private office KL", "price": "From RM799 / Seat / Month",
                                "property_type": "office"})
    assert errs == [] and rec["price_basis"] == "per_person" and rec["price_per_month"] == 799 and rec["currency"] == "MYR"
    rec, _, _ = n.normalize({"source_id": "2", "url": "https://o.my/2", "title": "Office", "price": "RM 8,000", "price_basis": "unit"})
    assert rec["price_basis"] == "total"


def test_business_and_office_fingerprints_survive_cosmetic_differences():
    """Trials 2026-10: solar leads never matched across sources (raw website strings, legal suffixes), and offices never got
    a fingerprint because the real-estate key needed bedrooms."""
    a = Normalizer(templates.get("businesses"), source_id="a", region="AE")
    b = Normalizer(templates.get("businesses"), source_id="b", region="AE")
    ra, _, _ = a.normalize({"source_id": "1", "url": "https://yp.ae/1", "name": "Greentech Inspiring Environment", "website": "https://greentech.ae"})
    rb, _, _ = b.normalize({"source_id": "2", "url": "https://rb.ai/2", "name": "Greentech", "website": "http://www.greentech.ae/"})
    assert a.fingerprints(ra) == b.fingerprints(rb) is not None
    ra, _, _ = a.normalize({"source_id": "3", "url": "https://yp.ae/3", "name": "Buildfy General Contracting LLC OPC", "city": "Abu Dhabi"})
    rb, _, _ = b.normalize({"source_id": "4", "url": "https://rb.ai/4", "name": "Buildfy General Contracting L.L.C.", "city": "أبو ظبي"})
    assert a.fingerprints(ra) == b.fingerprints(rb) is not None
    o = Normalizer(templates.get("real_estate_rent"), source_id="o", region="SG")
    rec, _, _ = o.normalize({"source_id": "1", "url": "https://o.sg/1", "title": "Office", "price": "S$ 8,000 /mo", "area": "1,200 sqft",
                             "property_type": "office", "city": "Singapore"})
    assert o.fingerprints(rec) is not None
