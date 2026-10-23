from pricewatch.telegram.app import _operation_id, _price


def test_price_uses_integer_minor_units_without_float_rounding():
    assert _price(1235, "EUR") == "12.35 EUR"
    assert _price(1, "USD") == "0.01 USD"
    assert _price(-1, "USD") == "-0.01 USD"


def test_operation_id_is_short_and_unique_for_callback_data():
    first, second = _operation_id(), _operation_id()
    assert first != second
    assert len(f"flow:create:{first}") < 64
