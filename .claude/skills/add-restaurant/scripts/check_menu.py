"""Check a restaurant menu file before it goes live.

    .venv/bin/python .claude/skills/add-restaurant/scripts/check_menu.py data/restaurants/<slug>.json

Runs the same checks as the /manage editor (ordering_agent/menu_check.py) and reports:
  ERROR    the app can't use it (bad structure, duplicate ids, bad prices or options,
           settings out of range)
  BLOCKER  callers would hear wrong or missing information (placeholder prices, settings
           listed in restaurant.placeholders, no tax rate)
  WARNING  worth confirming with the restaurant (hours/address not set, no notes)

Exit code: 0 = no errors or blockers, 1 = blockers only, 2 = errors.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from ordering_agent.menu_check import check_menu  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def check(path: Path) -> int:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR    can't read {path}: {e}")
        return 2
    result = check_menu(raw)
    errors = result["errors"]
    if not SLUG_RE.match(path.stem):
        errors.append(f"file name {path.name!r} should be the slug, e.g. hyderabad-house.json")

    s = result["summary"]
    if s:
        print(f"{s['name']}: {s['items']} items in {len(s['categories'])} categories")
        for cat, n in s["categories"].items():
            print(f"  {n:>3}  {cat}")
        print(f"Greeting: \"{s['greeting']}\"")
        print(f"Tax {s['tax_rate']}, pickup {s['pickup_minutes']} min, "
              f"pay before cooking: {s['pay_before_cooking']}")
        print()
    for label in ("errors", "blockers", "warnings"):
        for m in result[label]:
            print(f"{label[:-1].upper():<8} {m}")
    if not (errors or result["blockers"] or result["warnings"]):
        print("OK       nothing to fix")
    return 2 if errors else 1 if result["blockers"] else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(check(Path(sys.argv[1])))
