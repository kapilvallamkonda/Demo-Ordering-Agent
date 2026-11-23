"""Business templates: what kind of business an agent is for.

The core (phone calls, routing, transcripts, staff sign-in, the /manage editor, payment
links, dashboard, POS) works for any business. A template adds what differs by kind of
business:

  - modules     what the agent can do on a call. Built: "orders" (cart, pickup, payment),
                "guides" (the business's longer write-ups, looked up on demand) and
                "inquiries" (custom requests staff quote on). "bookings" (services, staff,
                appointments) is coming soon.
  - words       how the editor and the agent talk about it (menu/dish vs catalog/product)
  - prompt      the agent's role, business rules and what it politely declines
  - settings    label/help changes and extra settings for the /manage form
  - tags        quick description tags in the editor (Vegetarian, Spicy / Gift-wrappable ...)

A business's template is the top-level "template" key of its catalog JSON (default
"restaurant"). To add a kind of business: add a Template to TEMPLATES, and a starter
catalog in data/templates/<id>.json. A template whose modules aren't all built has
status "coming_soon": it's listed, but a business can't use it yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field



@dataclass(frozen=True)
class Template:
    id: str
    label: str  # shown when choosing a business type
    description: str
    modules: tuple[str, ...]
    words: dict  # see RESTAURANT for the keys
    role: str  # "You are the phone ordering assistant for {name}."
    rules: tuple[str, ...] = ()  # extra prompt bullets about this kind of business
    declines: str = ""  # what the agent can't do, in a parenthesis
    greeting: str = "Thanks for calling {name}! How can I help you today?"
    tags: tuple[str, ...] = ()
    setting_overrides: dict = field(default_factory=dict)  # path -> {label/help/...}
    extra_settings: tuple[dict, ...] = ()
    # Questions callers often ask this kind of business, offered as starting points for the
    # owner's "Common questions" (settings.faq); the owner writes the answers.
    faq_suggestions: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        from .modules import BUILT  # here, not at the top: modules import the catalog, which imports this

        return "available" if set(self.modules) <= BUILT else "coming_soon"

    def public(self) -> dict:
        """What the /manage page needs."""
        return {"id": self.id, "label": self.label, "description": self.description,
                "modules": list(self.modules), "status": self.status, "words": self.words,
                "tags": list(self.tags), "faq_suggestions": list(self.faq_suggestions)}


RESTAURANT = Template(
    id="restaurant",
    label="Restaurant",
    description="Phone orders for pickup from a menu: dishes with choices, extras and kitchen notes.",
    modules=("orders", "guides", "inquiries"),
    words={
        "business": "restaurant",
        "catalog": "menu",           # "answer menu questions", MENU in the prompt
        "item": "dish", "items": "dishes",
        "section": "section",
        "unavailable": "sold out today",   # "<dish> is sold out today"
        "unavailable_short": "Sold out",   # the editor's switch and badge
        "prepare": "cooking",              # "starts cooking only after the link is paid"
    },
    role="You are the phone ordering assistant for {name}.",
    declines="delivery, reservations, complaints",
    greeting="Thanks for calling {name}! What can I get started for you today?",
    tags=("Vegetarian.", "Vegan.", "Non-veg.", "Spicy.", "Extra spicy.", "Contains nuts.",
          "Gluten-free.", "Chef special."),
    setting_overrides={
        "name": {"label": "Restaurant name"},
        "payment.required_before_cooking": {"label": "Wait for payment before cooking"},
        "about": {"help": "One or two sentences the agent uses to describe you, e.g. \"South Indian "
                          "and Hyderabadi restaurant with a bakery.\""},
    },
    faq_suggestions=("Do you deliver?", "Do you cater large orders?", "Can dishes be made less spicy?",
                     "Do you have vegan or gluten-free options?", "Are your dishes nut-free?",
                     "Is there parking?", "Can I order for a large group?"),
)

RETAIL = Template(
    id="retail",
    label="Shop (products)",
    description="Phone orders for pickup from a product catalog: bakeries, florists, liquor, "
                "gift and convenience stores.",
    modules=("orders", "guides", "inquiries"),
    words={
        "business": "shop",
        "catalog": "catalog",
        "item": "product", "items": "products",
        "section": "category",
        "unavailable": "out of stock right now",
        "unavailable_short": "Out of stock",
        "prepare": "preparing the order",
    },
    role="You are the phone ordering assistant for {name}, a shop. You take orders for pickup.",
    rules=("If the caller asks for a product that isn't in the catalog, say you don't carry it "
           "on this line and offer the closest products.",),
    declines="shipping or delivery, returns, complaints",
    tags=("Bestseller.", "New.", "Gift-wrappable.", "Seasonal.", "Limited stock.", "Local.",
          "Contains alcohol: ID required at pickup."),
    setting_overrides={
        "pickup_minutes": {"label": "Typical time until ready for pickup (minutes)"},
        "about": {"help": "One or two sentences the agent uses to describe you, e.g. \"Family florist "
                          "with fresh flowers, plants and gift baskets.\""},
    },
    faq_suggestions=("Do you deliver?", "Do you gift wrap?", "What's your return policy?",
                     "Do you check ID for age-restricted items?", "Can I order something custom?",
                     "Can I reserve an item and pick it up later?", "Is there parking?"),
)

EVENTS = Template(
    id="events",
    label="Event & wedding supplies",
    description="Orders and advice for weddings and other events: garlands, flowers, ceremony "
                "items and sets, with guides for what's needed and custom requests for quotes.",
    modules=("orders", "guides", "inquiries"),
    words={
        "business": "shop",
        "catalog": "catalog",
        "item": "item", "items": "items",
        "section": "category",
        "unavailable": "out of stock right now",
        "unavailable_short": "Out of stock",
        "prepare": "preparing the order",
    },
    role="You are the phone assistant for {name}, which supplies items for weddings and other "
         "events. You help callers work out what they need, take orders for pickup, and take down "
         "custom requests for the team to quote.",
    rules=(
        "Callers are often planning a wedding or ceremony. Before suggesting things, ask the event "
        "date and which ceremonies or occasions it's for. Then suggest what's usually needed, from "
        "the catalog and the guides, one or two items at a time rather than a long list.",
        "Confirm the quantity, size, colour or style and the date needed for every item. Schedule "
        "the pickup for when they need it (set_pickup_time), not as soon as possible, unless they "
        "want it now. Mention any item's ordering-ahead rule early.",
        "When the catalog has a complete set or package that covers what they described, offer it.",
    ),
    declines="shipping, returns, complaints",
    greeting="Thanks for calling {name}! Are you shopping for a wedding or another event?",
    tags=("Bestseller.", "Made to order.", "Imported from India.", "Order early.", "Customizable.",
          "Seasonal.", "Part of a set."),
    setting_overrides={
        "pickup_minutes": {"label": "Typical time until ready for pickup (minutes)"},
        "max_advance_days": {"help": "How far ahead callers can order for. Weddings are often "
                                     "planned months ahead; 180 days is a good start."},
        "about": {"help": "One or two sentences the agent uses to describe you, e.g. \"Telugu wedding "
                          "supplies in Dallas: fresh garlands, ceremony items and wedding packages.\""},
    },
    faq_suggestions=("Do you ship outside the city?", "How far ahead should I order garlands?",
                     "Can I customize colours or flowers?", "Do you have wedding packages?",
                     "Do you rent items?", "Where do I pick up?", "Are the flowers fresh or artificial?"),
)

SALON = Template(
    id="salon",
    label="Salon / services (coming soon)",
    description="Appointments for services with a length and a staff member: salons, barbers, "
                "spas, clinics. Needs the bookings module, which is coming soon.",
    modules=("bookings",),
    words={
        "business": "salon", "catalog": "services", "item": "service", "items": "services",
        "section": "category", "unavailable": "not available right now",
        "unavailable_short": "Unavailable", "prepare": "booking",
    },
    role="You are the phone booking assistant for {name}.",
    declines="complaints, refunds",
    tags=("Popular.", "New client special.", "Consultation required."),
)

TEMPLATES: dict[str, Template] = {t.id: t for t in (RESTAURANT, RETAIL, EVENTS, SALON)}
DEFAULT = "restaurant"


def get_template(template_id: str | None) -> Template:
    """The template by id (missing means restaurant). Raises KeyError if unknown."""
    return TEMPLATES[template_id or DEFAULT]
