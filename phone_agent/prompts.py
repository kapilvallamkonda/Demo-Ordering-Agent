"""System prompt for the phone agent.

Built from parts: the template's role and how to speak on the phone (every business),
each of the business's modules' sections (modules/: how to take an order, the payment
rule ...), and the business's catalog. See templates.py.
"""

import re

from . import languages as langs
from .catalog import Catalog
from .modules import modules_for

_LANGUAGES = """\
Languages: callers can also be served in {names}. The greeting asked which language they'd \
like. If they choose English, or just start talking in English (ordering straight away is \
fine), carry on in English: if they only chose the language, ask "{opening}" \
Speech-to-text can mishear the language names ("Kanata", "Tomo", "Hindy"): when it's \
clearly one of them, switch without remarking on it. If the caller speaks one of these \
languages or asks for it, call switch_language first (unless the call is already in it), then reply only in that language, written in its own script. Keep \
replies especially short in these languages: one or two sentences. Keep dish and product \
names exactly as they appear in the {catalog} (in English), and say prices (when you say \
them) and the order number clearly. If the caller wants English again, call switch_language with en-US. If they \
ask for any other language, say briefly, in English, that you don't speak it yet, and \
continue in English."""

_HEADER = """\
{role} You are speaking with a caller on \
the phone: everything you write is converted to speech.

How to speak:
- Write plain spoken sentences. No markdown, bullet points, emoji, or special symbols.
- Keep each reply to one to three short sentences, then let the caller talk.
- When you say a price, say it naturally, like "fourteen dollars" or "$14.50". Don't recite \
item_ids.
- Speech-to-text can mishear words. If something doesn't match the {catalog}, offer the \
closest items instead of guessing.
- You may also hear things not meant for you: the caller talking to someone in the room \
("what do you want on your pizza?"), a TV or radio, or noise turned into words. When what you \
heard clearly isn't addressed to you, call wait_for_caller and say nothing. Use what you \
overheard only if the caller then tells it to you. If it might be for you, reply as usual."""


def build_system_prompt(catalog: Catalog, payment_links: bool = False) -> str:
    """Phone rules, then each module's section, then the catalog."""
    t = catalog.template
    name = catalog.settings.get("name", f"the {t.words['business']}")
    parts = [_HEADER.format(role=t.role.format(name=name), catalog=t.words["catalog"])]
    parts += [p for m in modules_for(t.modules) if m.prompt for p in [m.prompt(catalog, payment_links)] if p]
    offered = langs.for_business(catalog.settings)
    if offered:
        parts.append(_LANGUAGES.format(
            names=", ".join(f"{lang.label} ({lang.code})" for lang in offered),
            opening=_split_greeting(t.greeting.format(name=name))[1] or "How can I help you today?",
            catalog=t.words["catalog"]))
    parts.append(catalog.to_prompt_text())
    return "\n\n".join(parts) + "\n"


def _split_greeting(text: str) -> tuple[str, str]:
    """("Thanks for calling X!", "What can I get started for you today?"): the welcome and
    the opening question after it."""
    m = re.search(r"[.!?]\s+", text)
    return (text[:m.end()].strip(), text[m.end():].strip()) if m else (text, "")


def build_greeting(catalog: Catalog) -> str:
    """The template's greeting, or the business's own. A business that offers other
    languages asks which one the caller would like, conversationally, in place of the
    template's opening question (the agent asks that once English is chosen)."""
    custom = str(catalog.settings.get("greeting") or "").strip()
    greeting = custom or catalog.template.greeting.format(name=catalog.settings.get("name", "us"))
    offered = langs.for_business(catalog.settings)
    if not offered:
        return greeting
    if not custom:
        greeting = _split_greeting(greeting)[0]
    return f"{greeting} {langs.greeting_question(offered)}"
