import json
from pathlib import Path

import pytest

from phone_agent.catalog import CatalogError, parse_catalog
from phone_agent.catalog_check import check_catalog
from phone_agent.order import Order, OrderError
from phone_agent.prompts import build_greeting, build_system_prompt
from phone_agent.settings_schema import settings_for
from phone_agent.templates import TEMPLATES, get_template
from phone_agent.tools import CallSession, run_tool, tools_for

STARTERS = Path(__file__).resolve().parent.parent / "data" / "templates"


def starter(template_id):
    return json.loads((STARTERS / f"{template_id}.json").read_text())


@pytest.mark.parametrize("template_id", ["restaurant", "retail"])
def test_every_available_template_has_a_valid_starter(template_id):
    raw = starter(template_id)
    result = check_catalog(raw)
    assert result["errors"] == [] and result["summary"]["template"] == template_id
    assert parse_catalog(raw).template.id == template_id


def test_catalogs_without_a_template_are_restaurants():
    raw = json.loads(open("data/businesses/hyderabad-house.json").read())
    assert "template" not in raw and parse_catalog(raw).template_id == "restaurant"


def test_retail_wording_reaches_the_agent():
    menu = parse_catalog(starter("retail"))
    prompt = build_system_prompt(menu, payment_links=True)
    assert "a shop. You take orders for pickup." in prompt
    assert "CATALOG (item_id" in prompt and "MENU (" not in prompt
    assert "Never tell the caller to call the shop" in prompt and "restaurant" not in prompt
    assert build_greeting(menu) == "Thanks for calling Example Flowers & Gifts! How can I help you today?"
    pay_first = parse_catalog({**starter("retail"), "settings": {
        **starter("retail")["settings"], "payment": {"required_before_cooking": True}}})
    assert "starts preparing the order only after the link is paid" in build_system_prompt(pay_first, True)


def test_retail_order_flow_and_out_of_stock():
    raw = starter("retail")
    raw["items"][0]["available"] = False
    menu = parse_catalog(raw)
    session = CallSession(order=Order(catalog=menu))
    _, err = run_tool(session, "add_item", {"item_id": "seasonal_bouquet", "options": {"size": "deluxe"}})
    assert not err and session.order.subtotal == 50.0
    text, err = run_tool(session, "add_item", {"item_id": "dozen_roses", "options": {"color": "red"}})
    assert err and "out of stock right now" in text
    assert "OUT OF STOCK RIGHT NOW: can't be ordered" in menu.to_prompt_text()


def test_coming_soon_template_cant_be_used():
    salon = get_template("salon")
    assert salon.status == "coming_soon" and salon.modules == ("bookings",)
    with pytest.raises(CatalogError, match="coming soon"):
        parse_catalog({**starter("retail"), "template": "salon"})
    assert any("coming soon" in e for e in check_catalog({**starter("retail"), "template": "salon"})["errors"])
    with pytest.raises(CatalogError, match="unknown template"):
        parse_catalog({**starter("retail"), "template": "spaceship"})


def test_tools_come_from_the_modules():
    names = [t["name"] for t in tools_for(("orders",))]
    assert names[0] == "add_item" and "place_order" in names and names[-2:] == ["end_call", "wait_for_caller"]
    assert [t["name"] for t in tools_for(())] == ["end_call", "wait_for_caller"]


def test_settings_labels_follow_the_template():
    restaurant = {f["path"]: f for f in settings_for(TEMPLATES["restaurant"])}
    retail = {f["path"]: f for f in settings_for(TEMPLATES["retail"])}
    assert restaurant["name"]["label"] == "Restaurant name" and retail["name"]["label"] == "Business name"
    assert restaurant["payment.required_before_cooking"]["label"] == "Wait for payment before cooking"
    assert retail["payment.required_before_cooking"]["label"] == "Wait for payment before preparing orders"
    assert set(restaurant) == set(retail)  # same settings, different words
    assert "How can I help you today?" in retail["greeting"]["help"]
    assert "What can I get started" in restaurant["greeting"]["help"]


def test_manage_editor_sends_the_template(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from phone_agent import server
    from .fakes import FakeClient

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setenv("ADMIN_API_TOKEN", "optok")
    server.app.state.client = FakeClient([])
    server.app.state.payments = None
    auth = {"Authorization": "Bearer optok"}
    try:
        with TestClient(server.app) as c:
            rid = server.app.state.store.add_business("flowers", "+15550004444", starter("retail"))
            ed = c.get(f"/api/manage/businesses/{rid}/editor", headers=auth).json()
            assert ed["template"]["id"] == "retail" and ed["template"]["words"]["item"] == "product"
            assert "Gift-wrappable." in ed["template"]["tags"]
            assert next(f for f in ed["schema"] if f["path"] == "name")["label"] == "Business name"
            listed = {t["id"]: t["status"] for t in c.get("/api/manage/templates", headers=auth).json()}
            assert listed == {"restaurant": "available", "retail": "available", "events": "available", "salon": "coming_soon"}
            r = c.post("/voice", data={"To": "+15550004444"})
            assert "How can I help you today?" in r.text
    finally:
        del server.app.state.client, server.app.state.payments


def test_restaurant_prompt_keeps_its_wording():
    menu = parse_catalog(json.loads(open("data/businesses/hyderabad-house.json").read()))
    prompt = build_system_prompt(menu, payment_links=True)
    assert prompt.startswith("You are the phone ordering assistant for Hyderabad House. You are speaking")
    for line in ("answer menu questions from the menu below only",
                 "If the caller wants something you can't do (delivery, reservations, complaints)",
                 "Never tell the caller to call the restaurant; this call is the restaurant.",
                 "- Stay on the topic of ordering from this restaurant.",
                 "\nRestaurant: Hyderabad House\n", "MENU (item_id | name | base price | description)"):
        assert line in prompt
    pay_first = parse_catalog({**json.loads(open("data/businesses/hyderabad-house.json").read())})
    pay_first.settings["payment"] = {"required_before_cooking": True}
    assert "This restaurant starts cooking only after the link is paid" in build_system_prompt(pay_first, True)
