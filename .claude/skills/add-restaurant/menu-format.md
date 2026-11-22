# Menu file format

One file per restaurant: `data/restaurants/<slug>.json`. The app's parser is
`ordering_agent/menu.py` (`parse_menu`); `data/restaurants/hyderabad-house.json` is a
complete real example. Keep one item per line in `items`, like that file, so diffs stay
readable.

```json
{
  "restaurant": {
    "name": "Hyderabad House",
    "hours": "Mon-Thu 11am-10pm, Fri-Sun 11am-11pm",
    "address": "123 Main St, Overland Park, KS",
    "order_types": ["pickup"],
    "pickup_minutes": 25,
    "tax_rate": 0.08,
    "payment": {"required_before_cooking": false, "pay_within_minutes": 30},
    "placeholders": ["tax_rate"],
    "notes": [
      "Indian restaurant (Andhra / Hyderabadi / South Indian / Indo-Chinese).",
      "Dishes are made at the spice level normal for the dish. If the caller wants it milder or spicier, put that in the item's notes."
    ]
  },
  "items": [
    {"id": "butter_chicken", "name": "Butter Chicken", "category": "Non-Veg Entrees (served with white rice)", "price": 18.0, "description": "Non-veg. Chef special."},
    {"id": "hyderabadi_dum_biryani", "name": "Hyderabadi Dum Biryani", "category": "Biryanis", "price": 15.0, "description": "Non-veg.", "options": {"protein": {"choices": {"veg": 0.0, "egg": 1.0, "chicken": 4.0, "goat": 6.0}, "required": true}}},
    {"id": "soda", "name": "Soda (can)", "category": "Drinks", "price": 2.0, "description": "Vegetarian.", "options": {"flavor": {"choices": {"coke": 0.0, "sprite": 0.0}, "required": true}}, "placeholder_price": true}
  ]
}
```

## `template`

Top-level key: the kind of business, `"restaurant"` (default when missing) or
`"retail"`. See `python -m ordering_agent.restaurants templates`; starters are in
`data/templates/`. It changes the agent's wording and rules, not the file format.

## `restaurant` block (the business's settings, for every template)

| Field | Rule |
|---|---|
| `name` | Required. Spoken in the greeting and texts. |
| `hours`, `address` | Free text the agent reads out. If unknown, leave the key out; the agent then says it doesn't have it. |
| `greeting` | Optional. The opening line; default "Thanks for calling <name>! What can I get started for you today?". |
| `order_types` | Always `["pickup"]`; delivery isn't supported. |
| `pickup_minutes` | Typical minutes until ready. |
| `tax_rate` | A fraction (`0.0825` for 8.25%), not a percent. |
| `payment.required_before_cooking` | `true`: kitchen waits until the payment link is paid; `false`: paying by link is optional. Ask the restaurant. |
| `payment.pay_within_minutes` | How long the link is valid; 30 to 1440. |
| `placeholders` | Names of settings you guessed (e.g. `["tax_rate", "pickup_minutes"]`). The checker blocks on these. Remove each one once the restaurant confirms it. |
| `notes` | Lines added to the agent's instructions: cuisine, how spice requests work, allergy guidance, anything sold in person but not by phone. Never tell the agent to send callers to "call the restaurant": they already are. |

## Items

| Field | Rule |
|---|---|
| `id` | Unique, lowercase with underscores (`chicken_65`). Never change an id once live; old orders refer to it. |
| `name` | As printed on the menu, distinct enough to say aloud (add "Veg"/"Chicken" if two dishes share a name). |
| `category` | The menu's section heading. Put what the section includes in brackets, e.g. `"Dosa Specials (with sambar and choice of chutney)"`. |
| `price` | Base price in dollars, more than 0. For dishes sold in versions, the cheapest version. |
| `description` | Short. End with tags the agent uses: `Vegetarian.` / `Non-veg.`, `Spicy.` / `Extra spicy.`, `Contains nuts.`, `Chef special.` |
| `options` | Choices the caller must or may make. `{"<name>": {"choices": {"<choice>": <price delta>}, "required": true}}`. Deltas are added to `price`; the cheapest choice is `0.0`. At least 2 choices. |
| `extras` | Optional paid add-ons: `{"extra cheese": 1.5}`. |
| `available` | Optional. `false` = sold out for now (staff toggle this in `/manage`); the agent won't take it. |
| `placeholder_price` | `true` when the price wasn't on the menu and you estimated it. The checker blocks on these. |

## Turning menu photos into items

- One item per dish. When a dish is printed several times with different proteins or
  sizes (Chicken 65 / Fish 65 / Paneer 65, or Small / Large), make one item with a
  required option and price deltas from the cheapest, unless the versions are clearly
  different dishes.
- If a section says every dish comes with something the caller must choose (a chutney,
  rice or naan, fried rice or noodles), add that as a required option on each item.
- Copy prices exactly. If a price is unreadable or missing, don't guess silently: either
  leave the item out and list it for the restaurant, or estimate it and set
  `placeholder_price: true`. Ask the user which.
- Daily specials and "market price" items: leave them out and add a note telling the
  agent they're available in person.
- Kids-only or time-limited items: say so in the category or description.
