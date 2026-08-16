from asx.ingest.classifier import TAXONOMY, classify


def test_taxonomy_matches_spec():
    assert set(TAXONOMY) >= {
        "app_3y", "app_3z", "app_3b", "app_2a", "lr_3_10a_notice",
        "substantial_603", "substantial_604", "substantial_605",
        "annual_report", "half_year", "quarterly_4c_5b", "capital_reorg",
        "notice_of_meeting", "prospectus", "cleansing_notice", "other",
    }


def test_standard_forms():
    cases = {
        "Appendix 3Y - Change of Director's Interest Notice": "app_3y",
        "Change of Director's Interest Notice x 3": "app_3y",
        "Change in Director's Interest Notice": "app_3y",
        "Director's Interest Notice": "app_3y",
        "Appendix 3Z - Final Director's Interest Notice": "app_3z",
        "Final Director's Interest Notice": "app_3z",
        "Appendix 3B - Proposed issue of securities": "app_3b",
        "Appendix 2A - Application for quotation of securities": "app_2a",
        "Notification of cessation of restricted securities": "lr_3_10a_notice",
        "Release from escrow of restricted securities": "lr_3_10a_notice",
        "Form 603 - Notice of becoming a substantial holder": "substantial_603",
        "Form 604 - Notice of change in substantial holding": "substantial_604",
        "Form 605 - Ceasing to be a substantial shareholder": "substantial_605",
        "Annual Report to shareholders": "annual_report",
        "Appendix 4D and Half-Year Financial Report": "half_year",
        "Appendix 4C - Quarterly Cash Flow Report": "quarterly_4c_5b",
        "Quarterly Activities Report and Appendix 5B": "quarterly_4c_5b",
        "Notice of Annual General Meeting": "notice_of_meeting",
        "Share Consolidation - Notification of consolidation of capital": "capital_reorg",
        "Cleansing Notice under section 708A": "cleansing_notice",
        "Prospectus - fully underwritten rights issue": "prospectus",
        "Trading Halt": "other",
        "Investor Presentation": "other",
    }
    for title, expected in cases.items():
        got, _method = classify(title)
        assert got == expected, f"{title!r}: got {got}, want {expected}"


def test_3z_takes_priority_over_3y():
    # 'Final director's interest' must not fall through to the 3Y pattern.
    assert classify("Final Director's Interest Notice")[0] == "app_3z"


def test_llm_fallback_used_only_when_rules_miss():
    got, method = classify("Mysterious untitled document", llm=lambda t: "annual_report")
    assert (got, method) == ("annual_report", "llm")
    got, method = classify("Appendix 3Y lodgement", llm=lambda t: "annual_report")
    assert (got, method) == ("app_3y", "rules")


def test_llm_fallback_output_constrained_to_taxonomy():
    got, method = classify("Untitled", llm=lambda t: "not_a_real_class")
    assert (got, method) == ("other", "default")
