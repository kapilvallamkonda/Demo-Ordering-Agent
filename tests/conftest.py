import json
from pathlib import Path

# Tests use a small fixed menu so they don't break when the real menu (data/menu.json) changes.
FIXTURE_MENU = Path(__file__).parent / "fixtures" / "menu.json"
BELLA_PHONE = "+15550009999"  # the test restaurant's Twilio number


def seed_restaurant(store, slug="bella-cucina", phone=BELLA_PHONE, menu=None) -> int:
    """Register a restaurant (default: the fixture menu) and return its id."""
    return store.add_restaurant(slug, phone, menu or json.loads(FIXTURE_MENU.read_text()))
