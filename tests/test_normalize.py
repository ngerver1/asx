from asx.ids.normalize import name_norm, person_name_norm


def test_uppercase_and_whitespace():
    assert name_norm("  acme   corp ") == "ACME CORP"


def test_strips_trailing_corporate_suffixes_repeatedly():
    assert name_norm("Acme Holdings Pty Ltd") == "ACME"
    assert name_norm("Acme Limited") == "ACME"
    assert name_norm("Acme Proprietary Limited") == "ACME"
    assert name_norm("Western Mines NL") == "WESTERN MINES"


def test_suffix_only_in_trailing_position():
    # 'Holdings' mid-name is content, not a suffix.
    assert name_norm("Holdings Management Group") == "HOLDINGS MANAGEMENT GROUP"


def test_never_strips_to_empty():
    assert name_norm("Limited") == "LIMITED"
    assert name_norm("Pty Ltd") == "PTY"


def test_unicode_fold():
    assert name_norm("Café Résources Ltd") == "CAFE RESOURCES"


def test_punctuation_stripped():
    assert name_norm("J.P. Morgan Nominees (Australia)") == "J P MORGAN NOMINEES AUSTRALIA"


def test_same_string_same_output():
    # Pure function: identical inputs give identical outputs across calls.
    assert name_norm("BHP Group Limited") == name_norm("BHP Group Limited")


def test_person_norm_keeps_corporate_words():
    # A director surnamed 'Holdings' must not vanish.
    assert person_name_norm("Jane Holdings") == "JANE HOLDINGS"
    assert person_name_norm("O'Brien, Séan") == "O BRIEN SEAN"
