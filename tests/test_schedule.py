"""Item ordering rules (days, notice, in person only), scheduled pickups and the business clock."""

from datetime import datetime, timezone

import pytest

from phone_agent.catalog import CatalogError, parse_catalog
from phone_agent.catalog_check import check_catalog
from phone_agent.modules import orders
from phone_agent.order import Order, OrderError
from phone_agent.payments import paid_text
from phone_agent.prompts import build_system_prompt
from phone_agent.session import CallSession
from phone_agent.settings_schema import validate_settings
from phone_agent.templates import get_template


def catalog(tz="America/Chicago"):
    return parse_catalog({
        "template": "restaurant",
        "settings": {"name": "Rosa's", "timezone": tz, "pickup_minutes": 20, "tax_rate": 0.0825,
                     "about": "Hyderabadi restaurant.", "faq": [{"q": "Do you deliver?", "a": "No, pickup only."},
                                                                {"q": "Parking?", "a": ""}]},
        "items": [
            {"id": "tea", "name": "Tea", "category": "Drinks", "price": 2.0},
            {"id": "pulav", "name": "Pulav", "category": "Rice", "price": 15.0, "days": ["sat", "sun"]},
            {"id": "cake", "name": "Cake", "category": "Bakery", "price": 30.0, "notice_days": 1},
            {"id": "beer", "name": "Beer", "category": "Bar", "price": 6.0, "in_person_only": True},
        ],
    })


# Friday 2026-11-27, 7:30 PM in Texas = Saturday 01:30 UTC: the business's day is what counts.
FRIDAY_EVENING = datetime(2026, 11, 28, 1, 30, tzinfo=timezone.utc)


def order(clock=FRIDAY_EVENING, tz="America/Chicago"):
    return Order(catalog=catalog(tz), clock=lambda: clock)


def test_days_follow_the_business_clock_not_utc():
    o = order()
    with pytest.raises(OrderError, match="weekends only; the pickup is on a Friday"):
        o.add_item("pulav")
    # Scheduling a Saturday pickup makes it orderable.
    o.set_pickup("2026-11-28", "13:00")
    o.add_item("pulav")
    assert o.summary()["pickup"] == "Saturday, Nov 28 at 1:00 PM"
    # Moving the pickup back to a weekday (or as soon as possible) is refused while it's on the order.
    with pytest.raises(OrderError, match="doesn't work for everything"):
        o.set_pickup("2026-12-01", "13:00")
    with pytest.raises(OrderError, match="as soon as possible"):
        o.set_pickup(None, None)


def test_notice_counts_calendar_days():
    o = order()
    with pytest.raises(OrderError, match="1 day ahead; the earliest pickup day for it is Saturday, Nov 28"):
        o.add_item("cake")
    o.set_pickup("2026-11-28", "09:00")  # ordered Friday evening, picked up Saturday morning: fine
    o.add_item("cake")


def test_in_person_only_items_cant_be_ordered():
    with pytest.raises(OrderError, match="in person only"):
        order().add_item("beer")


def test_pickup_time_limits_and_placing():
    o = order()
    with pytest.raises(OrderError, match="too soon"):
        o.set_pickup("2026-11-27", "19:35")  # ready time is 20 minutes
    with pytest.raises(OrderError, match="30 days ahead"):
        o.set_pickup("2027-01-15", "12:00")
    with pytest.raises(OrderError, match="YYYY-MM-DD"):
        o.set_pickup("Saturday", "noon")
    o.add_item("tea")
    o.set_customer("Ana")
    o.set_pickup("2026-11-28", "10:00")
    # By the time they confirm, the clock has moved past the pickup.
    o.clock = lambda: datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc)
    with pytest.raises(OrderError, match="already passed"):
        o.mark_placed()


def test_place_order_reports_the_scheduled_pickup():
    session = CallSession(catalog=catalog(), store=None, call_id="c", caller_phone=None, payments=None, pos=None)
    orders.start(session)
    session.order.clock = lambda: FRIDAY_EVENING
    orders.handle(session, "set_pickup_time", {"date": "2026-11-28", "time": "12:30"})
    orders.handle(session, "add_item", {"item_id": "pulav"})
    orders.handle(session, "set_customer_name", {"name": "Ana"})
    out = orders.handle(session, "place_order", {"caller_confirmed": True})
    assert out["pickup"].startswith("Saturday, Nov 28 at 12:30") and "ready_in_minutes" not in out
    assert paid_text(session.catalog, 7, out["pickup"]).endswith("Pickup " + out["pickup"] + ".")


def test_prompt_describes_rules_about_and_answered_questions():
    text = build_system_prompt(catalog())
    assert "| weekends only" in text and "| order at least 1 day ahead" in text
    assert "IN PERSON ONLY, CAN'T BE ORDERED BY PHONE" in text
    assert "About the restaurant: Hyderabadi restaurant." in text
    assert "Q: Do you deliver? A: No, pickup only." in text and "Parking?" not in text  # unanswered: left out
    assert "set_pickup_time" in text


def test_bad_rules_and_settings_are_refused():
    with pytest.raises(CatalogError, match="days must be"):
        parse_catalog({"settings": {"name": "X"}, "items": [
            {"id": "a", "name": "A", "category": "C", "price": 1, "days": ["someday"]}]})
    with pytest.raises(CatalogError, match="notice"):
        parse_catalog({"settings": {"name": "X"}, "items": [
            {"id": "a", "name": "A", "category": "C", "price": 1, "notice_days": 121}]})
    t = get_template("restaurant")
    base = {"name": "X", "pickup_minutes": 20, "tax_rate": 0.08}
    assert any("Time zone" in e for e in validate_settings({**base, "timezone": "Mars/Base"}, t))
    assert any("Common questions" in e for e in validate_settings({**base, "faq": ["just text"]}, t))
    assert validate_settings({**base, "timezone": "America/Denver", "faq": [{"q": "Q?", "a": "A."}]}, t) == []


def test_checks_flag_drafts_from_the_menu():
    raw = {"template": "restaurant",
           "settings": {"name": "X", "pickup_minutes": 20, "tax_rate": 0.08,
                        "payment": {"required_before_cooking": False},
                        "faq": [{"q": "Do you cater?", "a": "Yes.", "from_menu": True}, {"q": "Parking?", "a": ""}]},
           "items": [{"id": "a", "name": "Cake", "category": "C", "price": 30, "notice_days": 1,
                      "rules_from_menu": True}]}
    r = check_catalog(raw)
    assert any("drafted from your menu" in b and "Do you cater?" in b for b in r["blockers"])
    assert any("ordering rules read from your menu" in b for b in r["blockers"])
    assert any("no answer yet" in w for w in r["warnings"])
