from db.list_codes import ALPHABET, LENGTH, generate_item_code, normalize_item_code


def test_alphabet_excludes_confusable_characters():
    for excluded in ('0', 'O', '1', 'I', 'L'):
        assert excluded not in ALPHABET


def test_alphabet_has_no_duplicates():
    assert len(ALPHABET) == len(set(ALPHABET))


def test_generated_code_has_expected_length():
    code = generate_item_code()
    assert len(code) == LENGTH == 6


def test_generated_code_only_uses_alphabet_characters():
    code = generate_item_code()
    assert all(ch in ALPHABET for ch in code)


def test_generate_item_code_is_reasonably_unique():
    codes = {generate_item_code() for _ in range(2000)}
    # With ~700M combinations, 2000 draws colliding would be astronomically
    # unlikely; this just guards against a degenerate/constant generator.
    assert len(codes) > 1990


def test_normalize_item_code_strips_and_uppercases():
    assert normalize_item_code(' ab3xyz ') == 'AB3XYZ'


def test_normalize_item_code_is_idempotent():
    code = generate_item_code()
    assert normalize_item_code(code) == code
    assert normalize_item_code(normalize_item_code(code)) == normalize_item_code(code)
