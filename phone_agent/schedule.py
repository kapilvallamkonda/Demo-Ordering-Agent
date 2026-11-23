"""When things can be ordered: the business's clock and each item's rules.

Item rules (all optional, set per item in the catalog). Day based on purpose, no
times of day:
  days            ["sat", "sun"]: only picked up on these days (empty or missing: every day)
  notice_days     1: must be ordered at least this many days before the pickup day
                  (calendar days: ordered Friday, picked up any time Saturday)
  in_person_only  true: sold at the business, not by phone

Rules are checked against the pickup day (an order for Saturday can be placed on Friday),
in the business's time zone (settings.timezone), so "Saturday" is the business's Saturday. Order code (order.py) enforces them; the
prompt only describes them.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
             "sat": "Saturday", "sun": "Sunday"}
MAX_NOTICE_DAYS = 30
MAX_ADVANCE_DAYS = 30  # how far ahead a pickup can be scheduled

# Offered in the settings form (US first; any IANA name is accepted).
TIMEZONES = (
    ("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"),
    ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
)


def default_timezone() -> str:
    return os.environ.get("DEFAULT_TIMEZONE", "America/Chicago")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or default_timezone())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(default_timezone())


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def now_in(tz: ZoneInfo, clock=None) -> datetime:
    return (clock() if clock else datetime.now(timezone.utc)).astimezone(tz)


def say_time(when: datetime) -> str:
    """'Saturday, Nov 29 at 2:00 PM'"""
    return f"{when.strftime('%A, %b')} {when.day} at {when.strftime('%I:%M %p').lstrip('0')}"


def describe_days(days) -> str:
    days = [d for d in DAYS if d in set(days or ())]
    if not days or len(days) == 7:
        return ""
    if days == ["sat", "sun"]:
        return "weekends only"
    if days == ["mon", "tue", "wed", "thu", "fri"]:
        return "weekdays only"
    return "only on " + ", ".join(DAY_NAMES[d] for d in days)


def describe_rules(item) -> str:
    """Plain words for the prompt and the editor, e.g. 'weekends only; order 1 day ahead'."""
    parts = []
    if item.in_person_only:
        parts.append("in person only, can't be ordered by phone")
    d = describe_days(item.days)
    if d:
        parts.append(d)
    if item.notice_days:
        parts.append(f"order at least {_days(item.notice_days)} ahead")
    return "; ".join(parts)


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def check_item(item, pickup: datetime, now: datetime) -> str | None:
    """Why `item` can't be picked up at `pickup` (both in business time), or None if it can."""
    if item.in_person_only:
        return f"{item.name} is sold in person only and can't be ordered by phone."
    days = set(item.days or ())
    if days and len(days) < 7 and DAYS[pickup.weekday()] not in days:
        return f"{item.name} is {describe_days(item.days)}; the pickup is on a {pickup.strftime('%A')}."
    if item.notice_days:
        earliest = now.date() + timedelta(days=item.notice_days)
        if pickup.date() < earliest:
            return (f"{item.name} has to be ordered {_days(item.notice_days)} ahead; the earliest pickup "
                    f"day for it is {earliest.strftime('%A, %b')} {earliest.day}.")
    return None


def validate_item_rules(it: dict) -> list[str]:
    """Problems with one catalog item's rule fields (catalog JSON)."""
    errors, name = [], it.get("name") or it.get("id")
    days = it.get("days")
    if days is not None and (not isinstance(days, list) or any(d not in DAYS for d in days)):
        errors.append(f"{name}: days must be a list of {', '.join(DAYS)}")
    n = it.get("notice_days")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_NOTICE_DAYS):
        errors.append(f"{name}: notice must be a whole number of days, up to {MAX_NOTICE_DAYS}")
    if not isinstance(it.get("in_person_only", False), bool):
        errors.append(f"{name}: in_person_only must be true or false")
    return errors
