from products.vetcostcheck.postprocess import postprocess_extraction


def test_qty_none_and_unit_none_get_defaults():
    data = {"items": [{"name": "X", "qty": None, "unit": None}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stück"


def test_qty_zero_becomes_one():
    data = {"items": [{"name": "X", "qty": 0, "unit": "Stk"}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stk"


def test_blank_unit_becomes_stueck():
    data = {"items": [{"name": "X", "qty": 2, "unit": "   "}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["unit"] == "Stück"


def test_populated_values_untouched():
    data = {"items": [{"name": "X", "qty": 3, "unit": "Tabletten"}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 3
    assert out["items"][0]["unit"] == "Tabletten"


def test_missing_items_key_is_noop():
    assert postprocess_extraction({}) == {}


def test_non_list_items_is_noop():
    data = {"items": None}
    assert postprocess_extraction(data) == {"items": None}


def test_non_dict_item_skipped():
    data = {"items": ["not a dict", {"qty": None, "unit": None}]}
    out = postprocess_extraction(data)
    assert out["items"][0] == "not a dict"
    assert out["items"][1]["qty"] == 1
