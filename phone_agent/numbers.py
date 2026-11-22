"""Buying and wiring phone numbers for businesses (Twilio).

Self-serve go-live (onboarding.py) buys a US local number in the requested area code,
points its voice webhook at our /voice, and names it after the business. Releasing a
number isn't automated yet (numbers keep costing money until released in Twilio).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("phone_agent.numbers")


class NumberError(Exception):
    """Couldn't get a number; the message is shown to the business owner."""


@dataclass(frozen=True)
class BoughtNumber:
    phone_number: str  # E.164
    sid: str


class NumberProvider(Protocol):
    def buy(self, area_code: str | None, friendly_name: str, voice_url: str) -> BoughtNumber: ...


class TwilioNumbers:
    def __init__(self, account_sid: str, auth_token: str, client=None):
        if client is None:
            from twilio.rest import Client

            client = Client(account_sid, auth_token)
        self.client = client

    @classmethod
    def from_env(cls) -> "TwilioNumbers | None":
        sid, token = os.environ.get("TWILIO_ACCOUNT_SID", ""), os.environ.get("TWILIO_AUTH_TOKEN", "")
        return cls(sid, token) if sid and token else None

    def buy(self, area_code: str | None, friendly_name: str, voice_url: str) -> BoughtNumber:
        from twilio.base.exceptions import TwilioRestException

        try:
            search = {"voice_enabled": True, "sms_enabled": True, "limit": 1}
            if area_code:
                search["area_code"] = int(area_code)
            available = self.client.available_phone_numbers("US").local.list(**search)
            if not available:
                raise NumberError(f"No numbers are available in area code {area_code}. Try a nearby one."
                                  if area_code else "No numbers are available right now. Try again later.")
            bought = self.client.incoming_phone_numbers.create(
                phone_number=available[0].phone_number, voice_url=voice_url, voice_method="POST",
                friendly_name=friendly_name[:64])
        except TwilioRestException as e:
            log.error("buying a number failed: %s", e)
            raise NumberError("We couldn't get a phone number right now. Please try again in a few minutes.")
        log.info("bought %s for %s", bought.phone_number, friendly_name)
        return BoughtNumber(bought.phone_number, bought.sid)
