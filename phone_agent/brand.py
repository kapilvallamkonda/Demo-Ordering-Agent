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


def page(filename: str) -> str:
    """A static page with the brand name filled in (HTML-escaped)."""
    return (STATIC / filename).read_text().replace("{{BRAND}}", html.escape(name()))
