"""Business templates: what kind of business an agent is for.

The core (phone calls, routing, transcripts, staff sign-in, the /manage editor, payment
links, dashboard, POS) works for any business. A template adds what differs by kind of
business:

  - modules     what the agent can do on a call. "orders" (cart, pickup, payment) is
                built; "bookings" (services, staff, appointments) is coming soon.
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

    @property
    def status(self) -> str:
        from .modules import BUILT  # here, not at the top: modules import the catalog, which imports this

        return "available" if set(self.modules) <= BUILT else "coming_soon"

    def public(self) -> dict:
        """What the /manage page needs."""
        return {"id": self.id, "label": self.label, "description": self.description,
                "modules": list(self.modules), "status": self.status, "words": self.words,
                "tags": list(self.tags)}


RESTAURANT = Template(
    id="restaurant",
    label="Restaurant",
    description="Phone orders for pickup from a menu: dishes with choices, extras and kitchen notes.",
    modules=("orders",),
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
        "notes": {"help": "Anything the agent should know: cuisine, how spice levels work, allergy "
                          "guidance, what's only sold in person. One note per line."},
    },
)

RETAIL = Template(
    id="retail",
    label="Shop (products)",
    description="Phone orders for pickup from a product catalog: bakeries, florists, liquor, "
                "gift and convenience stores.",
    modules=("orders",),
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
    declines="shipping or delivery, returns, special orders, complaints",
    tags=("Bestseller.", "New.", "Gift-wrappable.", "Seasonal.", "Limited stock.", "Local.",
          "Contains alcohol: ID required at pickup."),
    setting_overrides={
        "pickup_minutes": {"label": "Typical time until ready for pickup (minutes)"},
        "notes": {"help": "Anything the agent should know: what you sell, policies (age checks, "
                          "deposits), what's only available in the shop. One note per line."},
    },
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

TEMPLATES: dict[str, Template] = {t.id: t for t in (RESTAURANT, RETAIL, SALON)}
DEFAULT = "restaurant"


def get_template(template_id: str | None) -> Template:
    """The template by id (missing means restaurant). Raises KeyError if unknown."""
    return TEMPLATES[template_id or DEFAULT]
