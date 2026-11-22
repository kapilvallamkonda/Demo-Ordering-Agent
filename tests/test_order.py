import json
import pytest

from phone_agent.catalog import DEFAULT_CATALOG_PATH, load_catalog

from .conftest import FIXTURE_CATALOG
from phone_agent.order import Order, OrderError
from phone_agent.storage import Store
from phone_agent.tools import CallSession, run_tool


@pytest.fixture
def catalog():
    return load_catalog(FIXTURE_CATALOG)


def test_add_item_prices_options_and_extras(catalog):
    order = Order(catalog=catalog)
    line = order.add_item("margherita", 2, {"size": "16 inch"}, ["extra cheese"])
    assert line.unit_price == 14 + 4 + 2
    assert order.subtotal == 40.00
    assert order.tax == 3.20
    assert order.total == 43.20


def test_required_option_missing(catalog):
    with pytest.raises(OrderError, match="needs a 'size'"):
        Order(catalog=catalog).add_item("margherita")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"item_id": "sushi"}, "Unknown item_id"),
        ({"item_id": "tiramisu", "quantity": 0}, "between 1 and"),
        ({"item_id": "tiramisu", "quantity": True}, "whole number"),
        ({"item_id": "wings", "options": {"sauce": "ketchup"}}, "isn't a valid sauce"),
        ({"item_id": "tiramisu", "extras": ["sprinkles"]}, "isn't available"),
        ({"item_id": "tiramisu", "options": {"size": "big"}}, "has no option"),
    ],
)
def test_add_item_rejects_bad_input(catalog, kwargs, match):
    with pytest.raises(OrderError, match=match):
        Order(catalog=catalog).add_item(**kwargs)


def test_update_remove_and_place(catalog):
    order = Order(catalog=catalog)
    a = order.add_item("garlic_knots")
    b = order.add_item("tiramisu")
    order.update_quantity(a.line_id, 3)
    order.remove_item(b.line_id)
    assert order.subtotal == 18.00
    with pytest.raises(OrderError, match="name"):
        order.mark_placed()
    order.set_customer("Sam")
    order.mark_placed()
    with pytest.raises(OrderError, match="already been placed"):
        order.add_item("tiramisu")


def test_tools_validate_types_and_place_saves(catalog):
    store = Store(":memory:")
    session = CallSession(order=Order(catalog=catalog), store=store, call_id="CA123")

    text, err = run_tool(session, "add_item", {"item_id": "soda", "quantity": "two"})
    assert err and "wrong type" in text
    text, err = run_tool(session, "add_item", ["not", "a", "dict"])
    assert err and "INVALID_JSON" in text

    _, err = run_tool(session, "add_item", {"item_id": "soda", "options": {"flavor": "cola"}})
    assert not err
    _, err = run_tool(session, "place_order", {"caller_confirmed": False})
    assert err
    run_tool(session, "set_customer_name", {"name": "Ana"})
    text, err = run_tool(session, "place_order", {"caller_confirmed": True})
    assert not err and '"order_number": 1' in text
    saved = store.recent()
    assert saved[0]["customer_name"] == "Ana" and saved[0]["call_id"] == "CA123"


def test_catalog_prompt_text_is_deterministic(catalog):
    assert catalog.to_prompt_text() == load_catalog(FIXTURE_CATALOG).to_prompt_text()
    assert "margherita | Margherita Pizza" in catalog.to_prompt_text()


def test_real_catalog_loads_with_unique_ids():
    raw = json.loads(DEFAULT_CATALOG_PATH.read_text())
    ids = [it["id"] for it in raw["items"]]
    assert len(ids) == len(set(ids))
    catalog = load_catalog()
    assert len(catalog.items) == len(ids)
    for item in catalog.items.values():
        assert item.price > 0
        for grp in item.options.values():
            assert grp.choices and min(grp.choices.values()) >= 0


def test_real_catalog_drinks_and_desserts_are_orderable():
    catalog = load_catalog()
    order = Order(catalog=catalog)
    order.add_item("soda", options={"flavor": "coke"})
    order.add_item("mango_lassi")
    order.add_item("gulab_jamun")
    assert order.subtotal == 12.0
    assert "Soda" in catalog.to_prompt_text() and "placeholder" not in catalog.to_prompt_text()
