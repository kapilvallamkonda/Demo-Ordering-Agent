"""System prompt for the phone agent.

Built from parts: the template's role and how to speak on the phone (every business),
each of the business's modules' sections (modules/: how to take an order, the payment
rule ...), and the business's catalog. See templates.py.
"""

from . import languages as langs
from .catalog import Catalog
from .modules import modules_for

_LANGUAGES = """\
Languages: callers can also be served in {names}. The greeting told them how to switch \
({keys}). If the caller speaks one of these languages or asks for it, call switch_language \
first (unless the call is already in it), then reply only in that language, written in its own \
script. Keep replies especially short in these languages: one or two sentences. Keep dish and \
product names exactly as they appear in the {catalog} (in English), and say prices and the \
order number clearly. If the caller wants English again, call switch_language with en-US."""

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
    offered = langs.for_business(catalog.settings)
    if offered:
        parts.append(_LANGUAGES.format(
            names=", ".join(f"{lang.label} ({lang.code})" for lang in offered),
            keys=", ".join(f"press {lang.key} for {lang.label}" for lang in offered),
            catalog=t.words["catalog"]))
    parts.append(catalog.to_prompt_text())
    return "\n\n".join(parts) + "\n"


def build_greeting(catalog: Catalog) -> str:
    custom = str(catalog.settings.get("greeting") or "").strip()
    greeting = custom or catalog.template.greeting.format(name=catalog.settings.get("name", "us"))
    offer = langs.greeting_offer(langs.for_business(catalog.settings))
    return f"{greeting} {offer}" if offer else greeting
