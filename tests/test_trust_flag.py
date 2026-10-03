import pytest


def test_trusted_flag_survives_greenlets():
    """Pilot regression: the trust flag was a contextvar; Playwright's greenlet has its own context, so the
    sandbox audit hook blocked the browser driver ("subprocess.Popen is not allowed")."""
    greenlet = pytest.importorskip("greenlet")
    from harvest_ai.http import trusted, trusted_caller, trusted_section
    seen = []

    @trusted_caller
    def inside():
        with trusted_section():
            greenlet.greenlet(lambda: seen.append(trusted())).switch()
    inside()
    assert seen == [True] and not trusted()


def test_trusted_section_refuses_unregistered_callers():
    """Security review 2026-10: a scraper module could import trusted_section and switch the audit hook off."""
    from harvest_ai.http import trusted, trusted_section
    with pytest.raises(PermissionError):
        trusted_section()
    assert not trusted()
