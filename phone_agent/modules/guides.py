"""The guides module: the business's longer write-ups, looked up when the agent needs them.

A guide is something a caller's question needs more than a one-line answer for: "what's
needed for a Telugu wedding", "garland sizes and who wears which", "how far ahead to
order", "caring for fresh flowers". Owners write them in Settings (settings.guides, a list
of {"title", "text"}; the menu reader can draft them from the business's own pages).

Only the titles go in the system prompt; the agent calls read_guide for the text. That
keeps every call's prompt short however many guides a business writes, and the guide is
read fresh, word for word, when it matters.
"""

from __future__ import annotations

from ..session import CallSession, ToolError, req, tool

MAX_GUIDES = 30
MAX_TITLE = 120
MAX_TEXT = 6000

TOOLS: list[dict] = [
    tool(
        "read_guide",
        "Read one of the business's guides (listed by title in the prompt) before advising the "
        "caller on its topic. Answer from what it says; don't add to it.",
        {"title": {"type": "string", "description": "The guide's title as listed."}},
        ["title"],
    ),
]


def guides_of(catalog) -> list[dict]:
    """The business's usable guides (with a title and some text), in the owner's order."""
    out = []
    for g in (catalog.settings.get("guides") or [])[:MAX_GUIDES]:
        if isinstance(g, dict) and str(g.get("title") or "").strip() and str(g.get("text") or "").strip():
            out.append({"title": str(g["title"]).strip(), "text": str(g["text"]).strip()})
    return out


def find(guides: list[dict], title: str) -> dict | None:
    """By title: exact (ignoring case), else the one guide whose title contains it, or that
    shares the most words with it (speech and the model both paraphrase)."""
    want = " ".join(title.lower().split())
    if not want:
        return None
    for g in guides:
        if g["title"].lower() == want:
            return g
    containing = [g for g in guides if want in g["title"].lower() or g["title"].lower() in want]
    if len(containing) == 1:
        return containing[0]
    words = set(want.split())
    scored = sorted(((len(words & set(g["title"].lower().split())), i) for i, g in enumerate(guides)), reverse=True)
    if scored and scored[0][0] and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return guides[scored[0][1]]
    return None


def handle(session: CallSession, name: str, args: dict) -> dict:
    if name != "read_guide":
        raise KeyError(name)
    guides = guides_of(session.catalog)
    if not guides:
        raise ToolError("This business has no guides; answer from the catalog and common questions only.")
    g = find(guides, req(args, "title", str))
    if g is None:
        raise ToolError("No guide by that title. The guides are: " + "; ".join(x["title"] for x in guides) + ".")
    return {"title": g["title"], "text": g["text"]}


def prompt(catalog, payment_links: bool) -> str:
    guides = guides_of(catalog)
    if not guides:
        return ""
    return ("Guides: the business has written these guides. When the caller asks about one of these "
            "topics, or you're advising them on it, call read_guide first and use what it says:\n"
            + "\n".join(f"- {g['title']}" for g in guides))
