import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_default_languages(monkeypatch):
    """Production offers Telugu by default (DEFAULT_LANGUAGES); tests start without it so
    each one is about its own subject. test_languages.py covers the default."""
    monkeypatch.setenv("DEFAULT_LANGUAGES", "")

# Tests use a small fixed catalog so they don't break when the real catalog (data/businesses/) changes.
FIXTURE_CATALOG = Path(__file__).parent / "fixtures" / "catalog.json"
BELLA_PHONE = "+15550009999"  # the test business's Twilio number


def seed_business(store, slug="bella-cucina", phone=BELLA_PHONE, catalog=None) -> int:
    """Register a business (default: the fixture catalog) and return its id."""
    return store.add_business(slug, phone, catalog or json.loads(FIXTURE_CATALOG.read_text()))
