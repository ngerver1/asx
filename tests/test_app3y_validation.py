from datetime import datetime, timezone

from asx.parse.app3y import App3YParser


def _doc(**kw):
    d = {
        "doc_id": 1,
        "doc_class": "app_3y",
        "entity_id": 1,
        "lodged_at": datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc),
        "ticker_as_lodged": "XYZ",
    }
    d.update(kw)
    return d


def _payload(**kw):
    p = {
        "company_name": "Xyz Mining Limited",
        "ticker": "XYZ",
        "director_name": "Jane Citizen",
        "date_of_change": "2026-03-06",
        "interest_nature": "direct",
        "indirect_detail": None,
        "is_amendment": False,
        "securities": [{
            "security_class": "Ordinary shares",
            "qty_acquired": 100000,
            "qty_disposed": None,
            "consideration_text": "On-market purchase $25,000",
            "consideration_aud": 25000,
            "held_before": 400000,
            "held_after": 500000,
        }],
        "extraction_notes": None,
    }
    p.update(kw)
    return p


def test_clean_payload_validates():
    result = App3YParser().validate(_payload(), _doc())
    assert result.ok and not result.warnings


def test_held_after_arithmetic_enforced():
    payload = _payload()
    payload["securities"][0]["held_after"] = 499999
    result = App3YParser().validate(payload, _doc())
    assert any("arithmetic" in e for e in result.errors)


def test_missing_holdings_warns_not_errors():
    payload = _payload()
    payload["securities"][0]["held_before"] = None
    result = App3YParser().validate(payload, _doc())
    assert result.ok
    assert any("unverifiable" in w for w in result.warnings)


def test_change_date_after_lodgement_is_error():
    # Invariant 2: knowable_at (lodgement) can never precede the event it
    # discloses being... disclosed. A future-dated change is a misread.
    result = App3YParser().validate(_payload(date_of_change="2026-03-11"), _doc())
    assert any("after lodgement" in e for e in result.errors)


def test_long_lodgement_lag_warns():
    result = App3YParser().validate(_payload(date_of_change="2026-02-01"), _doc())
    assert result.ok
    assert any("lag" in w for w in result.warnings)


def test_missing_director_and_securities_error():
    result = App3YParser().validate(_payload(director_name=None, securities=[]), _doc())
    assert "director_name missing" in result.errors
    assert "no securities entries extracted" in result.errors


def test_missing_lodgement_timestamp_is_error():
    result = App3YParser().validate(_payload(), _doc(lodged_at=None))
    assert any("knowable_at undefined" in e for e in result.errors)


def test_negative_quantity_is_error():
    payload = _payload()
    payload["securities"][0]["qty_acquired"] = -5
    result = App3YParser().validate(payload, _doc())
    assert any("negative" in e for e in result.errors)


def test_3z_skips_arithmetic_and_change_date():
    payload = _payload(date_of_change=None)
    payload["securities"][0].update(
        qty_acquired=None, held_before=None, held_after=750000,
        consideration_text=None, consideration_aud=None,
    )
    result = App3YParser().validate(payload, _doc(doc_class="app_3z"))
    assert result.ok
