"""Readable names for the catalog's product category codes.

The challenge data gives each product a category code (349 of them) but no category names. This script
proposes a name per code from the words that are typical of its products' names (frequent in the
category, rare elsewhere), then applies the hand-checked corrections in OVERRIDES. The result is
gateway/category_names.json, which the gateway uses wherever a category is shown.

    python scripts/name_categories.py            # write gateway/category_names.json
    python scripts/name_categories.py --show     # print code, size, name and sample products
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.catalog import load_catalog  # noqa: E402
from gateway.config import settings  # noqa: E402

OUT = ROOT / "gateway" / "category_names.json"

STOP = set("""
a an and the of with in for to on per or by from no without approx ca app apx pcs pc pack packs piece pieces
g kg l ml dl cl ltr x st ps rs ka kpl n bag box can tin jar bottle tray sack bucket carton novobox vac vp
frozen fresh chilled dried natural organic eu fi finnish fin nl se de es it fr pl dk be ee lt lv uk usa
lactose free gluten vegan msc asc cl i ii iii new mini maxi big small large extra premium classic original
""".split())

# Hand-checked names for every code, reviewed against each category's products. The word statistics
# above are only a fallback for a code that is not listed here.
OVERRIDES: dict[str, str] = {
    "17300": "Ready meals & casseroles",
    "22630": "Spices & seasonings",
    "41763": "Frozen cakes",
    "64004": "Takeaway containers & lids",
    "41760": "Frozen bread",
    "63235": "Napkins, large",
    "41755": "Frozen ready meals & pizza",
    "22601": "Baking ingredients & decorations",
    "21917": "Sauces & dressings, bulk",
    "63114": "Kitchen cleaning agents",
    "64001": "Disposable cups & glasses",
    "41764": "Frozen buns & doughnuts",
    "63120": "Cleaning cloths & brushes",
    "22700": "Soft drinks",
    "52210": "Candy bags",
    "10412": "Yoghurt",
    "18720": "Beef",
    "21596": "Pickled peppers & vegetables",
    "41753": "Frozen vegetable patties",
    "21380": "Pasta",
    "41705": "Frozen meatballs & meat patties",
    "41820": "Ice cream, catering",
    "41731": "Frozen vegetables",
    "41762": "Frozen pasties & savoury pastries",
    "10600": "Cheese blocks & salad cheese",
    "41710": "Frozen chicken",
    "63001": "Hand soap & hand care",
    "64003": "Foil trays, picks & doilies",
    "22140": "Syrups",
    "41800": "Ice cream sticks & cones",
    "17306": "Tofu & plant proteins",
    "41720": "Frozen fish portions",
    "41761": "Frozen rolls & burger buns",
    "20012": "Gastronorm pans & lids",
    "63100": "Dishwashing detergents",
    "63232": "Tablecloths & table covers",
    "21902": "Bouillon & fonds",
    "20022": "Kitchen utensils",
    "41702": "Kebab & grill meats, frozen",
    "22300": "Ground coffee",
    "41801": "Ice cream tubs",
    "17210": "Cured & smoked meats",
    "18750": "Chicken",
    "22530": "Tea",
    "20031": "Plates, bowls & cups",
    "18710": "Pork",
    "17443": "Ready-cut vegetables",
    "17301": "Ready-made salads",
    "17200": "Sausages",
    "21140": "Biscuits & cookies",
    "21450": "Jams",
    "20032": "Drinking glasses",
    "21403": "Juices",
    "63231": "Napkins, dinner",
    "52200": "Chocolate bars",
    "10400": "Milk",
    "52290": "Chocolate boxes & pralines",
    "10602": "Sliced cheese",
    "64005": "Bags & heat-seal bags",
    "21400": "Juice drinks & nectars",
    "21910": "Mayonnaise",
    "20033": "Jugs & dispensers",
    "63115": "Descalers & special cleaners",
    "17203": "Ham & cold cuts",
    "11030": "Cakes & sponge cakes",
    "21918": "Salad dressings",
    "41765": "Frozen pies & sweet pastries",
    "18701": "Fresh fish",
    "52240": "Pastilles & liquorice",
    "63230": "Napkins, small",
    "21590": "Tomato products",
    "41751": "Fries & frozen potato products",
    "22701": "Energy drinks",
    "63071": "First aid & gloves",
    "10603": "Grated & pizza cheese",
    "20021": "Knives & slicers",
    "41723": "Frozen fish dishes",
    "21370": "Cereal flakes",
    "10901": "Fresh loaves",
    "21150": "Snack bars",
    "17303": "Sandwiches & paninis",
    "41766": "Frozen doughs & pastry",
    "10402": "Plant drinks & coconut milk",
    "21340": "Rice & grain mixes",
    "22620": "Vinegars",
    "21595": "Olives",
    "41742": "Frozen fruit purées",
    "22641": "Curry & spice pastes",
    "21460": "Nut & dried fruit mixes",
    "17206": "Salami",
    "22706": "Mineral water",
    "52209": "Pick & mix candy",
    "64010": "Candles",
    "21401": "Juice concentrates",
    "21461": "Dried fruit",
    "20011": "Pots, pans & coffee pots",
    "18705": "Herring & preserved fish",
    "21310": "Flours & breadcrumbs",
    "52201": "Chocolate tablets",
    "21904": "Cooking sauces",
    "21994": "Salsa & Tex-Mex sauces",
    "41701": "Cooked meats, sous vide",
    "17454": "Cress",
    "21600": "Baby food",
    "21594": "Canned beans",
    "10635": "Cream cheese",
    "20035": "Cutlery",
    "10401": "Cream",
    "63113": "Laundry detergents",
    "41721": "Shrimps & shellfish, frozen",
    "21905": "Sauce bases",
    "21130": "Wafers & gluten-free biscuits",
    "21404": "Smoothies",
    "17445": "Ready-cut fruit",
    "21990": "Potato chips",
    "41732": "Frozen vegetable mixes",
    "17431": "Root vegetables",
    "10430": "Quark & curd products",
    "21591": "Pickled cucumbers",
    "17209": "Cooked meat strips",
    "90011": "Office filing",
    "17442": "Ready-cut root vegetables",
    "21980": "Dessert powders & porridges",
    "10900": "Rye bread",
    "20034": "Tongs & ladles",
    "22702": "Tonic & mixers",
    "10636": "Feta & salad cheese",
    "41754": "Plant-based meat alternatives",
    "10413": "Plant-based yoghurt & snacks",
    "63272": "Cling film & foil",
    "41712": "Breaded chicken",
    "22610": "Mustard",
    "10531": "Olive oil",
    "90012": "Pens & markers",
    "17404": "Cabbage & cauliflower",
    "63121": "Bin bags",
    "22410": "Coffee beans",
    "17302": "Chilled ready meals",
    "21920": "Asian & pasta sauces",
    "21580": "Canned fruit",
    "21993": "Savoury snacks & dips",
    "10515": "Margarine & vegetable fats",
    "64011": "Outdoor lights & lanterns",
    "63240": "Paper towels",
    "10431": "Protein drinks & snacks",
    "17441": "Ready-cut salads",
    "17403": "Lettuce",
    "21921": "Soy sauce",
    "21320": "Flour & baking mixes",
    "22600": "Salt",
    "22100": "Sugar",
    "63233": "Dispenser napkins",
    "52202": "Chocolate sweets",
    "41830": "Pancakes & crêpes",
    "21981": "Dessert toppings & sauces",
    "41703": "Reindeer, frozen",
    "63241": "Paper dispensers",
    "63210": "Toilet paper",
    "90022": "Office supplies",
    "17201": "Frankfurters",
    "20036": "Bar equipment",
    "21451": "Fruit purées",
    "63300": "Workwear & aprons",
    "21992": "Nuts",
    "21462": "Seeds",
    "64000": "Disposable plates",
    "41740": "Frozen berries",
    "52208": "Lollipops",
    "20014": "Storage bags & jars",
    "21120": "Crackers",
    "21912": "Ketchup",
    "21991": "Tortillas & tortilla chips",
    "64002": "Disposable cutlery",
    "17415": "Exotic fruit",
    "21211": "Crispbread",
    "17455": "Edible flowers",
    "17423": "Cooked potatoes",
    "90024": "Printer ink & office electronics",
    "17305": "Hummus & spreads",
    "63271": "Coffee filters",
    "21381": "Noodles",
    "41704": "Lamb, frozen",
    "21402": "Berry soups & kissels",
    "90015": "Notebooks & paper pads",
    "85001": "Alcohol-free beer & drinks",
    "10406": "Plant-based cooking cream",
    "22670": "Gelatin & setting agents",
    "20023": "Scoops & scrapers",
    "17401": "Tomatoes",
    "21452": "Bakery fillings & curds",
    "17451": "Fresh herbs",
    "64007": "Baking cases & paper",
    "10419": "Sour cream",
    "21300": "Wheat flour",
    "21390": "Lentils & chickpeas",
    "21360": "Starch",
    "21373": "Muesli & granola",
    "17406": "Chillies",
    "22703": "Still drinks & vitamin water",
    "21382": "Couscous, bulgur & quinoa",
    "22540": "Cocoa & chocolate drinks",
    "22413": "Ready-to-drink coffee",
    "10641": "Plant-based spreads & slices",
    "85028": "Alcohol-free wine",
    "21530": "Canned fish",
    "10640": "Cheese platters & cooking cheese",
    "20013": "Baking tins",
    "17205": "Sliced cold cuts",
    "63002": "Personal hygiene",
    "41771": "Frozen cheese",
    "21371": "Semolina & grits",
    "10632": "Blue cheese",
    "21550": "Canned mushrooms & vegetables",
    "10530": "Cooking oils & frying fats",
    "17405": "Onions & garlic",
    "10631": "Speciality cheese",
    "22412": "Instant coffee",
    "22531": "Loose tea",
    "10411": "Fermented milk products",
    "17402": "Cucumbers & squash",
    "17407": "Fresh vegetables, other",
    "17432": "Peeled vegetables",
    "17471": "Fresh mushrooms",
    "64006": "Carrier bags",
    "63072": "Wet wipes",
    "52260": "Chewing gum",
    "21372": "Breakfast cereals",
    "64301": "Lamps, fuses & batteries",
    "22532": "Iced tea",
    "63130": "Food storage boxes",
    "17202": "Bacon",
    "10592": "Egg products",
    "17421": "Potatoes",
    "21919": "Asian sauces & condiments",
    "10500": "Butter",
    "10633": "Processed cheese",
    "22150": "Honey",
    "22646": "Seasoning purées",
    "18761": "Stew & roast meat",
    "63122": "Waste bins",
    "64300": "Thermometers",
    "18703": "Fresh shellfish",
    "18760": "Minced meat",
    "18708": "Cold-smoked fish",
    "63000": "Hair care",
    "10532": "Speciality oils",
    "22705": "Spring water",
    "17481": "Fresh berries",
    "10637": "Mozzarella",
    "41770": "Ice cubes",
    "41741": "Frozen smoothie mixes",
    "22640": "Seasoning mixes",
    "17411": "Citrus fruit",
    "17412": "Apples & pears",
    "21510": "Canned meals & soups",
    "17453": "Micro herbs",
    "63234": "Napkin dispensers",
    "17204": "Sliced poultry",
    "17207": "Liver pâté",
    "10902": "Fresh rolls & buns",
    "10432": "Dessert jellies & slush",
    "10639": "Halloumi",
    "18702": "Hot-smoked fish",
    "17444": "Ready-cut onions",
    "21453": "Sweet spreads & nut butters",
    "84966": "Alcohol-free beer",
    "22704": "Kombucha",
    "10405": "Milk & cream powders",
    "64500": "Gift wrapping",
    "21901": "Instant soups",
    "32990": "Lighters & matches",
    "64008": "Insulated boxes",
    "63273": "Roasting & oven bags",
    "90021": "Mailing bags",
    "17414": "Stone fruit, grapes & avocados",
    "63900": "Buffet & warming equipment",
    "21900": "Pea soup & canned soups",
    "17452": "Micro greens",
    "63070": "Sanitary products",
    "64100": "Grill & outdoor supplies",
    "41733": "Frozen herbs",
    "22707": "Soft drink concentrates",
    "17424": "Ready-cut potatoes",
    "90014": "Receipt rolls",
    "21200": "Rye crisps",
    "22190": "Sweeteners",
    "21463": "Seaweed & dried vegetables",
    "17422": "Peeled potatoes",
    "21593": "Pickled beetroot",
    "63131": "Buckets & lids",
    "10630": "Goat & sheep cheese",
    "17408": "Fresh peas & beans",
    "21592": "Sauerkraut & red cabbage",
    "41711": "Duck & poultry, frozen",
    "10903": "Karelian pasties",
    "10590": "Eggs",
    "63242": "Facial tissues",
    "21394": "Mashed potato powder",
    "21540": "Gourmet preserves",
    "41722": "Fish roe",
    "21950": "Vitamins & health drinks",
    "41752": "Frozen speciality dishes",
    "41734": "Frozen vegetable purées",
    "22660": "Yeast",
    "67800": "Seasonal decorations",
    "10517": "Baking & frying fats",
    "10404": "Condensed milk",
    "90013": "Printer paper",
    "21903": "Liquid bouillon",
    "21393": "Soy & pea protein mince",
    "10516": "Portion margarine",
    "17413": "Bananas",
    "41750": "Frozen sides & pâtés",
    "63200": "Kitchen roll",
    "18704": "Seaweed products",
    "67730": "Newspapers",
    "84988": "Alcohol-free long drinks",
    "21391": "Malt drinks",
    "17472": "Wild mushrooms",
    "10518": "Baking margarines & improvers",
    "17208": "Aspic & brawn",
    "20024": "Food thermometers",
    "84967": "Alcohol-free cider",
    "21982": "Sweetened condensed milk",
    "22411": "Espresso",
    "20000": "Pet food & treats",
    "17433": "Vegetable pulp",
    "64310": "Lamp oil & washer fluid",
    "10418": "Plant-based crème fraîche",
    "21100": "Rusks & crisp rolls",
    "21405": "Flavoured alcohol-free beer",
    "17456": "Sprouts",
    "21121": "Croutons",
    "18706": "Trout fillets",
    "18740": "Lamb",
    "10501": "Portion butter",
    "30010": "Garden & pest control",
    "17304": "Fresh pasta",
    "18707": "Grilled fish",
    "17457": "Banana leaves",
    "63270": "Twine & ties",
    "64130": "Pet supplies",
    "77722": "Carbon dioxide & gases",
    "17473": "Salted mushrooms",
    "21952": "Thickeners & dietary products",
    "10502": "Flavoured butter",
    "18751": "Duck fillets",
    "18730": "Reindeer",
    "85034": "Alcohol-free cider, French",
}


def words(name: str, brand: str | None) -> list[str]:
    text = (name or "").lower()
    if brand:
        for b in re.findall(r"[\wäöå]+", brand.lower()):
            text = re.sub(rf"\b{re.escape(b)}\b", " ", text)
    tokens = re.findall(r"[a-zäöåé]+", text)
    return [t for t in tokens if len(t) > 2 and t not in STOP]


def propose(products: list[dict]) -> dict[str, dict]:
    groups = defaultdict(list)
    for p in products:
        groups[p.get("category") or "?"].append(p)
    df_global = Counter()
    docs = {}
    for p in products:
        ws = words(p["name"], p.get("brand"))
        terms = set(ws) | {f"{a} {b}" for a, b in zip(ws, ws[1:])}
        docs[p["id"]] = terms
        df_global.update(terms)
    total = len(products)
    out = {}
    for code, items in groups.items():
        df = Counter()
        for p in items:
            df.update(docs[p["id"]])
        n = len(items)

        def score(term: str) -> float:
            return (df[term] / n) * math.log(total / df_global[term])

        ranked = sorted(df, key=score, reverse=True)
        bigrams = [t for t in ranked if " " in t and df[t] / n >= 0.25]
        unigrams = [t for t in ranked if " " not in t]
        if bigrams and score(bigrams[0]) >= 0.8 * score(unigrams[0]):
            name = bigrams[0]
        else:
            name = unigrams[0] if unigrams else code
            second = next((t for t in unigrams[1:4] if df[t] / n >= 0.2 and t not in name), None)
            if second:
                name = f"{name}, {second}"
        out[code] = {"name": name[:1].upper() + name[1:], "products": n,
                     "examples": [p["name"] for p in items[:3]]}
    return out


def main() -> None:
    catalog = load_catalog(settings.valio_dir)
    proposed = propose(catalog.products)
    names = {code: OVERRIDES.get(code, row["name"]) for code, row in proposed.items()}
    if "--show" in sys.argv:
        for code, row in sorted(proposed.items(), key=lambda kv: -kv[1]["products"]):
            print(f"{code}\t{row['products']}\t{names[code]}\t| " + " | ".join(e[:40] for e in row["examples"]))
        return
    OUT.write_text(json.dumps(dict(sorted(names.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(names)} category names -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
