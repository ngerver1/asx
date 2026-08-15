from asx.parse.llm import field_disagreements


def test_identical_payloads_agree():
    a = {"x": 1, "y": [{"z": "abc"}], "extraction_notes": "pass one"}
    b = {"x": 1, "y": [{"z": "abc"}], "extraction_notes": "different notes ok"}
    assert field_disagreements(a, b) == []


def test_scalar_disagreement_located():
    a = {"director_name": "Jane Doe", "securities": [{"held_after": 100}]}
    b = {"director_name": "Jane Doe", "securities": [{"held_after": 200}]}
    assert field_disagreements(a, b) == ["securities[0].held_after"]


def test_numeric_string_vs_number_agree():
    # "10,000" printed with separators vs 10000 as a number is the same fact.
    assert field_disagreements({"q": "10,000"}, {"q": 10000}) == []


def test_list_length_mismatch():
    a = {"securities": [{"c": 1}]}
    b = {"securities": [{"c": 1}, {"c": 2}]}
    assert field_disagreements(a, b) == ["securities[len 1 != 2]"]


def test_null_vs_value_disagrees():
    # One pass read a field the other could not: that is a disagreement, and
    # disagreement routes to review (SPEC §6).
    assert field_disagreements({"q": None}, {"q": 5}) == ["q"]
