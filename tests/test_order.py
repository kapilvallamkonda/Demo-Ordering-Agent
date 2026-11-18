import json
import pytest

from ordering_agent.menu import DEFAULT_MENU_PATH, load_menu

from .conftest import FIXTURE_MENU
from ordering_agent.order import Order, OrderError
from ordering_agent.storage import OrderStore
from ordering_agent.tools import CallSession, run_tool


@pytest.fixture
def menu():
    return load_menu(FIXTURE_MENU)


def test_add_item_prices_options_and_extras(menu):
    order = Order(menu=menu)
    line = order.add_item("margherita", 2, {"size": "16 inch"}, ["extra cheese"])
    assert line.unit_price == 14 + 4 + 2
    assert order.subtotal == 40.00
    assert order.tax == 3.20
    assert order.total == 43.20


def test_required_option_missing(menu):
    with pytest.raises(OrderError, match="needs a 'size'"):
        Order(menu=menu).add_item("margherita")


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
def test_add_item_rejects_bad_input(menu, kwargs, match):
    with pytest.raises(OrderError, match=match):
        Order(menu=menu).add_item(**kwargs)


def test_update_remove_and_place(menu):
    order = Order(menu=menu)
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


def test_tools_validate_types_and_place_saves(menu):
    store = OrderStore(":memory:")
    session = CallSession(order=Order(menu=menu), store=store, call_id="CA123")

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


def test_menu_prompt_text_is_deterministic(menu):
    assert menu.to_prompt_text() == load_menu(FIXTURE_MENU).to_prompt_text()
    assert "margherita | Margherita Pizza" in menu.to_prompt_text()


def test_real_menu_loads_with_unique_ids():
    raw = json.loads(DEFAULT_MENU_PATH.read_text())
    ids = [it["id"] for it in raw["items"]]
    assert len(ids) == len(set(ids))
    menu = load_menu()
    assert len(menu.items) == len(ids)
    for item in menu.items.values():
        assert item.price > 0
        for grp in item.options.values():
            assert grp.choices and min(grp.choices.values()) >= 0
