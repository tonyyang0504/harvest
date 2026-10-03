import pytest

from harvest_ai import domains, regions, templates
from harvest_ai.project import Spec, cadence_hours


def test_regions_expand_codes_groups_names_aliases():
    assert regions.expand(["KZ", "ge"]) == ["KZ", "GE"]
    assert regions.expand("PT, ES") == ["PT", "ES"]
    assert regions.expand(["UK", "Portugal", "caucasus"]) == ["GB", "PT", "AM", "AZ", "GE"]
    assert set(regions.expand(["EU"])) >= {"DE", "FR", "PT", "ES"} and len(regions.expand(["EU"])) == 27
    assert regions.expand(["global"]) == ["GLOBAL"]
    assert regions.expand(["IBERIA", "ES"]) == ["ES", "PT"]  # de-duplicated, order kept
    with pytest.raises(regions.RegionError):
        regions.expand(["Atlantis"])
    with pytest.raises(regions.RegionError):
        regions.expand([])


def test_region_info_languages_and_cctld():
    assert regions.info("KZ")["currency"] == "KZT"
    assert regions.languages_for(["KZ", "GE"]) == ["kk", "ru", "ka"]
    assert regions.cctld("GB") == ".uk" and regions.cctld("PT") == ".pt"
    for code, c in regions.COUNTRIES.items():
        assert len(code) == 2 and len(c["currency"]) == 3 and c["languages"]


@pytest.mark.parametrize("url,expect", [
    ("https://m.kolesa.kz/cars/", "kolesa.kz"),
    ("https://www.autotrader.co.uk/x", "autotrader.co.uk"),
    ("shop.example.com.br", "example.com.br"),
    ("https://a.b.myhomes.ge", "myhomes.ge"),
    ("https://user.github.io/page", "user.github.io"),
    ("https://foo.bar.ck", "foo.bar.ck"),
    ("https://www.ck", "www.ck"),
    ("http://idealista.com:8080/pt", "idealista.com"),
    ("https://sub.example.co.id", "example.co.id"),
    ("https://xn--80ak6aa92e.com", "xn--80ak6aa92e.com"),
    ("https://пример.рф/путь", "xn--e1afmkfd.xn--p1ai"),
    ("127.0.0.1", "127.0.0.1"),
])
def test_registrable_domain(url, expect):
    assert domains.registrable_domain(url) == expect


def test_psl_file_is_honoured(tmp_path, monkeypatch):
    psl = tmp_path / "psl.dat"
    psl.write_text("// test\ncom\nblogspot.com\n*.kawasaki.jp\n!city.kawasaki.jp\n", encoding="utf-8")
    monkeypatch.setenv("HARVEST_PSL_FILE", str(psl))
    domains._rules.cache_clear()
    try:
        assert domains.registrable_domain("a.b.blogspot.com") == "b.blogspot.com"
        assert domains.registrable_domain("x.foo.kawasaki.jp") == "x.foo.kawasaki.jp"
        assert domains.registrable_domain("www.city.kawasaki.jp") == "city.kawasaki.jp"
    finally:
        domains._rules.cache_clear()


def test_templates_are_well_formed():
    assert set(templates.TEMPLATES) == {"vehicles", "real_estate_sale", "real_estate_rent", "rentals", "jobs", "products", "events", "businesses", "generic"}
    for rt, t in templates.TEMPLATES.items():
        assert t["label"]
        for name, f in t["fields"].items():
            assert f["type"] in templates.KNOWN_TYPES, (rt, name)
            if f["type"] == "money":
                assert f.get("currency_field") in t["fields"], (rt, name)
            if f.get("period_field"):
                assert t["fields"][f["period_field"]]["type"] == "period"
        for keys in t.get("dedup") or []:
            assert all(k in t["fields"] for k in keys), (rt, keys)
        for p in t.get("periods") or []:
            assert p["canonical"] in templates.PERIODS and all(a in t["fields"] for a in p["amounts"])
        assert "source_id" in t["fields"] and t["fields"]["url"]["required"]


def test_template_aliases_extra_fields_and_columns():
    assert templates.canonical_type("cars") == "vehicles" and templates.canonical_type("rental_apartments") == "real_estate_rent"
    t = templates.get("vehicles", ["dealer_rating", "warranty"])
    assert t["fields"]["dealer_rating"]["type"] == "string"
    t2 = templates.get("jobs", {"visa": {"type": "boolean"}})
    assert t2["fields"]["visa"]["type"] == "boolean"
    cols = templates.output_columns(templates.get("jobs"))
    assert "salary_min_annual" in cols and "salary_max_report" in cols
    with pytest.raises(ValueError):
        templates.get("vehicles", ["bad field!"])
    with pytest.raises(ValueError):
        templates.get("spaceships")


def test_spec_validation_and_cadence():
    s = Spec.build(name="cars-kz", target="used cars", regions="KZ, GE", record_type="cars")
    assert s.region_codes == ["KZ", "GE"] and s.record_type == "vehicles" and s.languages == ["kk", "ru", "ka"]
    with pytest.raises(ValueError):
        Spec.build(name="Bad Name", target="x", regions=["KZ"], record_type="vehicles")
    with pytest.raises(ValueError):
        Spec.build(name="ok", target="", regions=["KZ"], record_type="vehicles")
    with pytest.raises(ValueError):
        Spec.build(name="ok", target="x", regions=["KZ"], record_type="vehicles", cadence="sometimes")
    assert cadence_hours("daily") == 24 and cadence_hours("6h") == 6 and cadence_hours("2d") == 48 and cadence_hours("manual") == 0
