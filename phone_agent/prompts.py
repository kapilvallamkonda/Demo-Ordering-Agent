"""System prompt for the phone agent.

Built from parts: the template's role and how to speak on the phone (every business),
each of the business's modules' sections (modules/: how to take an order, the payment
rule ...), and the business's catalog. See templates.py.
"""

from .catalog import Catalog
from .modules import modules_for

_HEADER = """\
{role} You are speaking with a caller on \
the phone: everything you write is converted to speech.

How to speak:
- Write plain spoken sentences. No markdown, bullet points, emoji, or special symbols.
- Keep each reply to one to three short sentences, then let the caller talk.
- Say prices naturally, like "fourteen dollars" or "$14.50". Don't recite item_ids.
- Speech-to-text can mishear words. If something doesn't match the {catalog}, offer the \
closest items instead of guessing."""


def build_system_prompt(catalog: Catalog, payment_links: bool = False) -> str:
    """Phone rules, then each module's section, then the catalog."""
    t = catalog.template
    name = catalog.settings.get("name", f"the {t.words['business']}")
    parts = [_HEADER.format(role=t.role.format(name=name), catalog=t.words["catalog"])]
    parts += [m.prompt(catalog, payment_links) for m in modules_for(t.modules) if m.prompt]
    parts.append(catalog.to_prompt_text())
    return "\n\n".join(parts) + "\n"


def build_greeting(catalog: Catalog) -> str:
    custom = str(catalog.settings.get("greeting") or "").strip()
    if custom:
        return custom
    return catalog.template.greeting.format(name=catalog.settings.get("name", "us"))
