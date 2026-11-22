"""The product's public name, in one place.

Pages in `static/` write `{{BRAND}}` wherever the name appears (titles, the logo, footer
text); `page()` fills it in when serving them. Scripts read it from
`<meta name="application-name">`, so the name is never pasted into JavaScript. Set
BRAND_NAME to rebrand without touching code.
"""

from __future__ import annotations

import html
import os
from pathlib import Path

DEFAULT = "Ringcart"
STATIC = Path(__file__).parent / "static"


def name() -> str:
    return os.environ.get("BRAND_NAME", "").strip()[:40] or DEFAULT


def page(filename: str, **values: object) -> str:
    """A static page with the brand name (and any other `{{KEY}}` values passed) filled in,
    HTML-escaped."""
    text = (STATIC / filename).read_text()
    for key, value in {"BRAND": name(), **values}.items():
        text = text.replace("{{" + key + "}}", html.escape(str(value)))
    return text
