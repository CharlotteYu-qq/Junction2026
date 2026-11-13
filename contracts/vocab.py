"""Shared vocabulary for codes that appear in the Valio Aimo product data.

Only codes we are confident about are translated; anything else is shown as its raw code.
"""

# GS1 allergen type codes (allergen / nonAllergen classifications)
ALLERGENS = {
    "AC": "crustaceans",
    "AE": "eggs",
    "AF": "fish",
    "AM": "milk",
    "AN": "tree nuts",
    "AP": "peanuts",
    "AS": "sesame",
    "AU": "sulphites",
    "AW": "cereals containing gluten",
    "AY": "soy",
    "BC": "celery",
    "BM": "mustard",
    "NL": "lupin",
    "UM": "molluscs",
    "UW": "wheat",
    "GB": "barley",
    "GO": "oats",
    "NR": "rye",
    "ML": "lactose",
    "SA": "almonds",
    "SH": "hazelnuts",
    "SW": "walnuts",
}

# nutritionalClaim ids
CLAIMS = {
    "FREE_FROM_LACTOSE": "lactose-free",
    "LOW_LACTOSE": "low-lactose",
    "FREE_FROM_GLUTEN": "gluten-free",
    "FREE_FROM_WHEAT": "wheat-free",
    "VEGAN": "vegan",
    "VEGETARIAN": "vegetarian",
    "FREE_FROM_EGGS": "egg-free",
    "FREE_FROM_SOY": "soy-free",
    "NO_ADDED_SUGARS": "no added sugar",
    "FREE_FROM_PRESERVATIVES": "no preservatives",
    "FREE_FROM_ADDITIVES": "no additives",
    "FREE_FROM_COLOURING_AGENTS": "no colouring",
    "FREE_FROM_SWEETENERS": "no sweeteners",
    "HIGH_FIBRE": "high fibre",
    "HIGH_PROTEIN": "high protein",
    "LOW_SODIUM_SALT": "low salt",
    "LOW_FAT": "low fat",
    "FREE_FROM_FAT": "fat-free",
}

# packagingMarkedLabel ids (certificates & labels shown in the webshop)
LABELS = {
    "GOODS_FROM_FINLAND_BLUE_SWAN": "Goods from Finland (Blue Swan)",
    "MADE_IN_FINLAND_FLAG_WITH_KEY": "Made in Finland (key flag)",
    "PUHTAASTI_KOTIMAINEN": "Puhtaasti kotimainen",
    "FINNISH_HEART_SYMBOL": "Heart Symbol",
    "EU_ORGANIC_FARMING": "EU organic",
    "SVANEN": "Nordic Swan Ecolabel",
    "RAINFOREST_ALLIANCE": "Rainforest Alliance",
    "EU_ECO_LABEL": "EU Ecolabel",
    "MARINE_STEWARDSHIP_COUNCIL_LABEL": "MSC",
    "AQUACULTURE_STEWARDSHIP_COUNCIL": "ASC",
    "FAIR_TRADE_MARK": "Fairtrade",
    "KRAV_MARK": "KRAV",
    "OFFICIAL_ECO_LABEL_SUN": "Sun ecolabel",
    "SOIL_ASSOCIATION_ORGANIC_SYMBOL": "Soil Association organic",
    "DOLPHIN_SAFE": "Dolphin safe",
    "UTZ_CERTIFIED": "UTZ",
}

FINNISH_LABELS = {"GOODS_FROM_FINLAND_BLUE_SWAN", "MADE_IN_FINLAND_FLAG_WITH_KEY", "PUHTAASTI_KOTIMAINEN"}

# Sales-unit codes. The dataset says translations are not included; these are best guesses
# (SAP / Finnish abbreviations), so the UI always shows the code next to the word.
SALES_UNITS = {
    "ST": "piece",
    "KI": "case",
    "KG": "kg",
    "PAK": "pack",
    "BAG": "bag",
    "RAS": "box",
    "PRK": "tub",
    "BOT": "bottle",
    "SK": "sack",
    "CS": "case",
    "KRT": "carton",
    "KAN": "canister",
    "PAL": "pallet",
    "NIP": "bundle",
}

# Preferences a request can carry, grouped for the UI's "add requirement" menu.
REQUIREMENT_OPTIONS = [
    {"kind": "claim", "code": "FREE_FROM_LACTOSE", "label": "lactose-free"},
    {"kind": "claim", "code": "FREE_FROM_GLUTEN", "label": "gluten-free"},
    {"kind": "claim", "code": "VEGAN", "label": "vegan"},
    {"kind": "claim", "code": "VEGETARIAN", "label": "vegetarian"},
    {"kind": "allergen", "code": "AM", "label": "milk-free"},
    {"kind": "allergen", "code": "AE", "label": "egg-free"},
    {"kind": "allergen", "code": "AN", "label": "free from tree nuts"},
    {"kind": "allergen", "code": "AP", "label": "free from peanuts"},
    {"kind": "label", "code": "EU_ORGANIC_FARMING", "label": "EU organic"},
    {"kind": "origin", "code": "fi", "label": "Finnish origin"},
]


def as_dict() -> dict:
    return {
        "allergens": ALLERGENS,
        "claims": CLAIMS,
        "labels": LABELS,
        "sales_units": SALES_UNITS,
        "requirement_options": REQUIREMENT_OPTIONS,
    }
