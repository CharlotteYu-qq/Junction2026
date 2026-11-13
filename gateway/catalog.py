"""Static product data: build a slim index once, then search it in memory.

The raw product file is ~87 MB. On first start we extract only the fields the gateway needs
(names, units, allergens, claims, labels, sizes) and merge in delivery reliability and the
carbon-footprint averages. The result is cached as data/valio/catalog_slim.json.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

RAW_PRODUCTS = "valio_aimo_junction26_product_data.json"
RAW_RELIABILITY = "valio_aimo_junction26_delivery_reliability.csv"
RAW_CO2 = "valio_aimo_junction26_carbon_footprint.csv"
SLIM = "catalog_slim.json"
SLIM_VERSION = 2

TEMPERATURE_LABELS = {"warmTemperature", "coolTemperature", "freezeTemperature"}
_SIZE_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|ltr|l|kg|gr|g)\b", re.IGNORECASE)
_SIZE_FACTOR = {
    "ml": ("L", 0.001), "cl": ("L", 0.01), "dl": ("L", 0.1), "l": ("L", 1.0), "ltr": ("L", 1.0),
    "g": ("KG", 0.001), "gr": ("KG", 0.001), "kg": ("KG", 1.0),
}

# Words that usually mean "a different product" unless the request itself mentions them.
DERIVATIVE_WORDS = {
    "chocolate", "cocoa", "coffee", "café", "cafe", "powder", "flour", "flourmix", "mix", "bread", "cake",
    "biscuit", "biscuits", "cookie", "cookies", "sauce", "soup", "ice", "dessert", "pudding", "yoghurt",
    "yogurt", "quark", "cheese", "fermented", "buttermilk", "kefir", "porridge", "snack", "bar", "candy",
    "sweet", "blend", "based", "vegetable", "plant", "oat", "soy", "rice", "almond", "coconut", "portion",
    "drinkable", "smoothie", "shake", "protein", "baby", "pet", "cat", "dog", "sour", "curd", "whey",
    "condensed", "evaporated", "skyr", "lassi", "spread", "filling", "flavoured", "flavored",
    # prepared dishes made from the ingredient ("potato casserole" when you asked for potatoes)
    "casserole", "creamed", "gratin", "mash", "mashed", "puree", "purée", "fries", "chips", "crisps", "salad",
    "wedges", "baked", "meal", "pizza", "pie", "pastry", "sandwich", "dressing", "marinade", "seasoning",
    "ketchup", "paste", "juice", "concentrate",
    # non-food items that share words with food ("shower cream", "napkin with milk print")
    "shower", "soap", "lotion", "shampoo", "hand", "skin", "body", "hair", "detergent", "cleaner", "cleaning",
    "dishwasher", "napkin", "napkins", "candle", "glove", "gloves", "basic",
}


def parse_size(name: str) -> tuple[float, str] | None:
    """'Valio milk 1 l' -> (1.0, 'L'); '100x2 cl' -> (2.0, 'L'); '200 g' -> (0.2, 'KG')."""
    if not name:
        return None
    m = _SIZE_RE.search(name)
    if not m:
        return None
    count = int(m.group(1)) if m.group(1) else 1
    value = float(m.group(2).replace(",", "."))
    unit, factor = _SIZE_FACTOR[m.group(3).lower()]
    return round(count * value * factor, 4), unit


def _read_csv_map(path: Path, column: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[row["productId"].strip()] = float(row[column])
            except (KeyError, ValueError, TypeError):
                continue
    return out


def build_slim(valio_dir: Path) -> list[dict]:
    raw_path = valio_dir / RAW_PRODUCTS
    if not raw_path.exists():
        raise FileNotFoundError(
            f"{raw_path} not found. Download the challenge data first: python scripts/download_data.py"
        )
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    reliability = _read_csv_map(valio_dir / RAW_RELIABILITY, "deliveryReliability")
    co2 = _read_csv_map(valio_dir / RAW_CO2, "AVG kg CO2-eq/kg")

    products = []
    for p in raw:
        if p.get("deleted"):
            continue
        s = p.get("synkkaData") or {}
        names = {n.get("language"): (n.get("value") or "").strip() for n in s.get("names") or [] if n.get("value")}
        groups = {c.get("name"): c.get("values") or [] for c in s.get("classifications") or []}
        allergens = groups.get("allergen", [])
        contains = sorted({v["id"] for v in allergens if v.get("unit") == "CONTAINS"})
        may = sorted({v["id"] for v in allergens if v.get("unit") == "MAY_CONTAIN"} - set(contains))
        free_from = sorted({v["id"] for v in groups.get("nonAllergen", []) if v.get("unit") == "FREE_FROM"})
        claims = sorted({v["id"] for v in groups.get("nutritionalClaim", [])})
        labels = sorted({v["id"] for v in groups.get("packagingMarkedLabel", []) if v["id"] not in TEMPERATURE_LABELS})
        units = {u["unitId"]: u.get("sizeInBaseUnits") for u in p.get("units") or [] if u.get("unitId")}
        sales_unit, base_unit = p.get("salesUnit"), p.get("baseUnit")

        content = content_unit = None
        if sales_unit == "KG":
            content, content_unit = 1.0, "KG"
        else:
            size = parse_size(names.get("en", "")) or parse_size(names.get("fi", ""))
            if size:
                factor = 1.0 if sales_unit == base_unit else units.get(sales_unit)
                if factor:
                    content, content_unit = round(size[0] * factor, 4), size[1]

        products.append(
            {
                "id": p["id"],
                "name": names.get("en") or names.get("fi") or p["id"],
                "name_fi": names.get("fi"),
                "name_sv": names.get("sv"),
                "brand": s.get("brand"),
                "vendor": p.get("vendorName"),
                "category": p.get("category"),
                "sales_unit": sales_unit,
                "base_unit": base_unit,
                "lot": p.get("allowedLotSize") or 1,
                "units": units,
                "temp": p.get("temperatureCondition"),
                "country": p.get("countryOfOrigin"),
                "contains": contains,
                "may_contain": may,
                "free_from": free_from,
                "claims": claims,
                "labels": labels,
                "content": content,
                "content_unit": content_unit,
                "substitutes": [x["substituteProduct"] for x in p.get("substitutions") or [] if x.get("product") == p["id"]],
                "reliability": reliability.get(p["id"]),
                "co2": co2.get(p["id"]),
            }
        )
    return products


class Catalog:
    def __init__(self, products: list[dict]):
        self.products = products
        self.by_id = {p["id"]: p for p in products}
        self._index = []
        for p in products:
            en = (p["name"] or "").lower()
            fi = (p.get("name_fi") or "").lower()
            self._index.append(
                (
                    p,
                    en,
                    tuple(re.findall(r"[\wäöåé]+", en)),
                    fi,
                    tuple(re.findall(r"[\wäöå]+", fi)),
                    (p.get("name_sv") or "").lower(),
                    (p.get("brand") or "").lower(),
                )
            )

    def __len__(self) -> int:
        return len(self.products)

    @staticmethod
    def _penalised(word: str, penalties: set[str]) -> bool:
        return word in penalties or (word.endswith("s") and word[:-1] in penalties) or (word.endswith("es") and word[:-2] in penalties)

    def search(self, phrases: list[str], exclude_words: list[str] | None = None, limit: int = 60) -> list[tuple[dict, float]]:
        """Rank products for a list of alternative phrases (words inside one phrase are ANDed).

        Scoring per word: English whole word (plural ok) 3 · Finnish head of a compound ("kevyt*maito*") 2.5 ·
        Swedish 1.5 · Finnish modifier ("*maito*suklaa" = milk chocolate) 1.5 · English compound ("milkshake") 1.
        Words that usually mean another product (chocolate, powder, ...) and "<query>-free" are penalised,
        and anything scoring below half of the best match is dropped.
        """
        phrase_words = [[w for w in re.findall(r"[\wäöåé-]+", ph.lower()) if len(w) > 1] for ph in phrases if ph]
        phrase_words = [ws for ws in phrase_words if ws]
        if not phrase_words:
            return []
        query_words = {w for ws in phrase_words for w in ws}
        penalties = (DERIVATIVE_WORDS | set(w.lower() for w in exclude_words or [])) - query_words
        compiled = [
            [(w, re.compile(rf"\b{re.escape(w)}(?:e?s)?\b"), re.compile(rf"\b{re.escape(w)}")) for w in ws] for ws in phrase_words
        ]
        # "milk free" / "milk-free" means the opposite of what a "milk" search wants
        negated = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(query_words)) + r")[- ]?free\b")

        scored: list[tuple[dict, float]] = []
        for p, en, en_words, fi, fi_words, sv, brand in self._index:
            best = 0.0
            for words in compiled:
                total = 0.0
                for w, whole, prefix in words:
                    if whole.search(en):
                        s = 3.0
                    elif any(fw.endswith(w) for fw in fi_words) or any(ew.endswith(w) for ew in en_words):
                        s = 2.5  # head of a compound: kevyt-maito, halloumi-burger
                    elif w in sv:
                        s = 1.5
                    elif w in fi:
                        s = 1.5  # modifier only: maito-suklaa (milk chocolate)
                    elif prefix.search(en) or w in en or w in brand:
                        s = 1.0  # milk-shake, milk-fed
                    else:
                        total = 0.0
                        break
                    total += s
                if total / len(words) < 2.0:
                    total = 0.0  # the query only appears inside another product's name
                best = max(best, total)
            if best <= 0:
                continue
            hits = sum(1 for w in en_words if self._penalised(w, penalties))
            best -= min(3.0 * hits, 6.0)
            if negated.search(en):
                best -= 4.0
            best -= 0.05 * max(0, len(en_words) - 4)
            if best > 0:
                scored.append((p, best))
        if not scored:
            return []

        # The intended product group tends to dominate the strongest matches: give it a nudge.
        scored.sort(key=lambda t: t[1], reverse=True)
        top_categories = Counter(p["category"] for p, _ in scored[:30]).most_common(2)
        bonus = {cat: b for (cat, _), b in zip(top_categories, (1.5, 0.5))}
        scored = [(p, round(s + bonus.get(p["category"], 0.0), 3)) for p, s in scored]
        scored.sort(key=lambda t: t[1], reverse=True)
        floor = 0.5 * scored[0][1]
        return [t for t in scored if t[1] >= floor][:limit]


def load_catalog(valio_dir: Path) -> Catalog:
    slim_path = valio_dir / SLIM
    if slim_path.exists():
        with open(slim_path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") == SLIM_VERSION:
            return Catalog(payload["products"])
    print("Building slim catalog from the raw product file (one-time, ~20 s)...", flush=True)
    products = build_slim(valio_dir)
    with open(slim_path, "w", encoding="utf-8") as f:
        json.dump({"version": SLIM_VERSION, "products": products}, f, ensure_ascii=False)
    return Catalog(products)
