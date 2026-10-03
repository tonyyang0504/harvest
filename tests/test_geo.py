import json

import pytest

from harvest_ai import geo, templates
from harvest_ai.normalize import Normalizer


@pytest.fixture(autouse=True)
def fresh_index(monkeypatch):
    monkeypatch.delenv("HARVEST_GAZETTEER", raising=False)
    geo.reload()
    yield
    geo.reload()


@pytest.mark.parametrize("raw,cc,want", [
    ("Lisboa", "PT", "Lisbon"), ("LISBOA", "PT", "Lisbon"), ("Lisbon", "PT", "Lisbon"),
    ("Тбилиси", "GE", "Tbilisi"), ("თბილისი", "GE", "Tbilisi"), ("г. Тбилиси", "GE", "Tbilisi"),
    ("Bakı", "AZ", "Baku"), ("Баку", "AZ", "Baku"), ("Алматы", "KZ", "Almaty"), ("Nur-Sultan", "KZ", "Astana"),
    ("Setúbal", "PT", "Setubal"), ("Donostia", "ES", "San Sebastian"), ("München", "DE", "Munich"), ("東京都", "JP", "Tokyo"),
    ("Loures", "PT", "Loures"), ("Unknownville", "PT", "Unknownville"), ("  Lisboa  ", None, "Lisbon"),
])
def test_builtin_gazetteer(raw, cc, want):
    assert geo.canonical_city(raw, cc) == want


def test_country_scoping_and_empty_values():
    assert geo.canonical_city("Valencia", "ES") == "Valencia"
    assert geo.canonical_city("València", "ES") == "Valencia"
    assert geo.canonical_city("Porto", "PT") == "Porto" and geo.canonical_city("Oporto", "PT") == "Porto"
    assert geo.canonical_city(None, "PT") is None and geo.canonical_city("", "PT") == ""
    assert geo.known("GE") >= 5


def test_json_and_geonames_files(tmp_path, monkeypatch):
    j = tmp_path / "extra.json"
    j.write_text(json.dumps({"GE": {"Mtskheta": ["Мцхета", "მცხეთა"]}, "PT": {"Lisbon": ["Lisbona"]}}), encoding="utf-8")
    gn = tmp_path / "cities500.txt"
    # geonameid, name, asciiname, alternatenames, lat, lon, feature class, feature code, country code, ...
    gn.write_text("611717\tTbilisi\tTbilisi\tTiflis,Tbilisis,Тбилиси,Tbilissi\t41.69\t44.83\tP\tPPLC\tGE\n"
                  "2267057\tLisbon\tLisbon\tLisboa,Lisbonne,Lizbona\t38.71\t-9.13\tP\tPPLC\tPT\n"
                  "999\tSomeRiver\tSomeRiver\tRio X\t0\t0\tH\tSTM\tPT\n", encoding="utf-8")
    monkeypatch.setenv("HARVEST_GAZETTEER", f"{j}:{gn}")
    geo.reload()
    assert geo.canonical_city("мцхета", "GE") == "Mtskheta"
    assert geo.canonical_city("Lisbona", "PT") == "Lisbon" and geo.canonical_city("Lizbona", "PT") == "Lisbon"
    assert geo.canonical_city("Tbilissi", "GE") == "Tbilisi"
    assert geo.canonical_city("Rio X", "PT") == "Rio X"  # non-populated features are ignored


def test_normalizer_canonicalises_city_and_keeps_raw():
    """Pilot: rent-pt stored 'Lisbon' (96) and 'Lisboa' (34) as two cities."""
    n = Normalizer(templates.get("real_estate_rent"), source_id="s", region="PT")
    rec, errs, _ = n.normalize({"source_id": "1", "url": "https://x.pt/1", "title": "T2", "price": "900 €", "city": "Lisboa"})
    assert errs == [] and rec["city"] == "Lisbon" and rec["extra"] == {"city_raw": "Lisboa"}
    rec, _, _ = n.normalize({"source_id": "2", "url": "https://x.pt/2", "title": "T2", "price": "900 €", "city": "Lisbon"})
    assert rec["city"] == "Lisbon" and "extra" not in rec
    ge = Normalizer(templates.get("vehicles"), source_id="c", region="GE")
    rec, _, _ = ge.normalize({"source_id": "3", "url": "https://c.ge/3", "make": "Toyota", "price": "9 000 $", "city": "Тбилиси"})
    assert rec["city"] == "Tbilisi" and rec["extra"]["city_raw"] == "Тбилиси"
