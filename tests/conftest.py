from pathlib import Path

# Tests use a small fixed menu so they don't break when the real menu (data/menu.json) changes.
FIXTURE_MENU = Path(__file__).parent / "fixtures" / "menu.json"
