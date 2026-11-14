"""Rule-based parser: always available, deterministic, English + basic Finnish.

    "I need 1,000 litres of lactose-free milk by Monday"
 -> item(product="milk", quantity=1000, unit=L, claims=[FREE_FROM_LACTOSE]), delivery=next Monday
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from contracts.models import Constraints, ParsedRequest, RequestItem

# ----------------------------------------------------------------------------- dates

_WEEKDAYS = [
    r"monday|maanantai\w*",
    r"tuesday|tiistai\w*",
    r"wednesday|keskiviik\w*",
    r"thursday|torstai\w*",
    r"friday|perjantai\w*",
    r"saturday|lauantai\w*",
    r"sunday|sunnuntai\w*",
]
_WEEKDAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_CUE = r"(?:(?:by|on|for|until|before|delivered|delivery|deliver|next|this|toimitus|mennessä)\s+)*"
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def next_delivery_day(today: date) -> date:
    """Next Monday-Friday after today (the default when no date is given)."""
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _next_weekday(today: date, wd: int) -> date:
    ahead = (wd - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _future(d: date, today: date) -> date:
    """A date written without a year: this year, or next year when it is long past ("5.1." in December).
    A date that passed recently ("10.11." on 14.11.) is kept, so it is flagged instead of moved a year."""
    return d if d >= today or (today - d).days <= 180 else d.replace(year=d.year + 1)


def _day_of_month(day: int, today: date) -> date:
    """"the 25th": this month if still ahead, otherwise next month."""
    if day >= today.day:
        return date(today.year, today.month, day)
    return date(today.year + (today.month == 12), today.month % 12 + 1, day)


def extract_date(text: str, today: date) -> tuple[date | None, str]:
    """Return (date, text without the date phrase)."""
    patterns: list[tuple[re.Pattern, callable]] = []
    for i, wd in enumerate(_WEEKDAYS):
        patterns.append((re.compile(rf"\b{_CUE}(?:{wd})\b", re.I), lambda m, i=i: _next_weekday(today, i)))
    for i, ab in enumerate(_WEEKDAY_ABBR):
        patterns.append((re.compile(rf"\b(?:by|on|for|until|before)\s+(?:next\s+)?{ab}\b\.?", re.I), lambda m, i=i: _next_weekday(today, i)))
    patterns += [
        (re.compile(rf"\b{_CUE}(?:the\s+)?day after tomorrow\b|\bylihuomenna\b", re.I), lambda m: today + timedelta(days=2)),
        (re.compile(rf"\b{_CUE}tomorrow\b|\bhuomenna\b|\bhuomiseksi\b", re.I), lambda m: today + timedelta(days=1)),
        (re.compile(rf"\b{_CUE}today\b|\btänään\b", re.I), lambda m: today),
        (re.compile(rf"\b{_CUE}next week\b|\bensi viikoksi\b|\bensi viikolla\b", re.I), lambda m: _next_weekday(today, 0)),
        (re.compile(r"\bin\s+(\d{1,2})\s+days?\b", re.I), lambda m: today + timedelta(days=int(m.group(1)))),
        (re.compile(rf"\b{_CUE}(\d{{4}})-(\d{{2}})-(\d{{2}})\b", re.I), lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (re.compile(rf"\b{_CUE}(\d{{1,2}})\.(\d{{1,2}})\.(\d{{4}})?", re.I),
         lambda m: _future(date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1))), today)),
        (re.compile(rf"\b{_CUE}(\d{{1,2}})(?:st|nd|rd|th)?\s+({'|'.join(_MONTHS)})[a-z]*\b", re.I),
         lambda m: _future(date(today.year, _MONTHS.index(m.group(2).lower()[:3]) + 1, int(m.group(1))), today)),
        (re.compile(rf"\b{_CUE}({'|'.join(_MONTHS)})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I),
         lambda m: _future(date(today.year, _MONTHS.index(m.group(1).lower()[:3]) + 1, int(m.group(2))), today)),
        # "on the 25th" (an ordinal is required, so "25 kg" is never a date)
        (re.compile(rf"\b{_CUE}(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)\b", re.I), lambda m: _day_of_month(int(m.group(1)), today)),
    ]
    for rx, resolve in patterns:
        m = rx.search(text)
        if m:
            try:
                d = resolve(m)
            except ValueError:
                continue
            return d, (text[: m.start()] + " " + text[m.end():])
    return None, text


# ----------------------------------------------------------------------------- quantities

_UNITS: list[tuple[str, str, float]] = [
    (r"litres?|liters?|litraa?|ltr|l", "L", 1.0),
    (r"dl|desilitraa?", "L", 0.1),
    (r"cl", "L", 0.01),
    (r"ml", "L", 0.001),
    (r"tonnes?|tons?|tonnia?", "KG", 1000.0),
    (r"kilograms?|kilos?|kiloa|kg", "KG", 1.0),
    (r"grams?|grammaa?|g", "KG", 0.001),
    (r"pieces?|pcs|pc|kpl|kappaletta|units?", "PCS", 1.0),
    (r"cases?|boxes?|crates?|laatikkoa?|packs?|packages?|pakettia?|bottles?|pulloa?|cartons?|bags?|pussia?|sacks?|säkkiä|trays?", "SALES_UNIT", 1.0),
]
_UNIT_ALT = "|".join(u for u, _, _ in _UNITS)
_QTY_RE = re.compile(rf"(?P<qty>[+-]?\d+(?:\.\d+)?)(?:\s*(?P<unit>{_UNIT_ALT})(?![a-zäöå]))?", re.I)
_SERVINGS_RE = re.compile(r"\b(?:for\s+)?(\d+)\s*(?:people|persons|pupils|students|guests|portions|servings|covers|henkilöä|hengelle|annosta|oppilaalle|oppilasta)\b", re.I)


def _unit_of(token: str) -> tuple[str, float]:
    for pattern, unit, factor in _UNITS:
        if re.fullmatch(pattern, token, re.I):
            return unit, factor
    return "SALES_UNIT", 1.0


def normalise_numbers(text: str) -> str:
    text = re.sub(r"(?<=\d)[  ](?=\d{3}\b)", "", text)  # 1 000 -> 1000
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", text)  # 1,000 -> 1000
    return re.sub(r"(\d),(\d)", r"\1.\2", text)  # 2,5 -> 2.5


# ----------------------------------------------------------------------------- order controls

# Remove order controls before looking for product quantities. A monetary amount is
# never an extra product, and an instruction forbidding a substitute is not an item.
_CONTROL_TAIL = r".*?(?=\.(?!\d)|[!?;\n,]|\band\b(?=\s+(?:\d|budget\b|budjetti\b))|$)"
_NO_SUBSTITUTION = re.compile(
    rf"\b(?:(?:do\s+not|don't|never)\s+(?:substitut\w*|replace\w*)|"
    rf"(?:no|without)\s+(?:substitutions?|substitutes?|replacements?)|"
    rf"ei\s+(?:korvaavia\s+tuotteita|korvauksia)){_CONTROL_TAIL}", re.I,
)
_ALLOW_SUBSTITUTION = re.compile(r"\b(?:substitutions?|substitutes?|replacements?)\s+(?:are\s+)?(?:allowed|acceptable|ok)\b", re.I)
_MONEY_TOKEN = r"(?:€|EUR\b|euros?\b|\$|USD\b|dollars?\b|£|GBP\b|pounds?\b|SEK\b|NOK\b|DKK\b)"
_AMOUNT_TOKEN = r"[+-]?\d+(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?:({_MONEY_TOKEN})\s*({_AMOUNT_TOKEN})|({_AMOUNT_TOKEN})\s*({_MONEY_TOKEN}))(?=\s|$)", re.I,
)
_UNPARSED_MONEY = re.compile(
    rf"(?:{_MONEY_TOKEN}\s*{_AMOUNT_TOKEN}|{_AMOUNT_TOKEN}\s*{_MONEY_TOKEN})", re.I,
)
_BUDGET = re.compile(
    rf"\b(?:(?:(?:maximum|max|total)\s+)?budget\b|budjetti\b|"
    rf"spend\s+(?:no\s+more\s+than|at\s+most|up\s+to)|"
    rf"(?:up\s+to|under|within|at\s+most|no\s+more\s+than)(?=\s+(?:{_MONEY_TOKEN}\s*{_AMOUNT_TOKEN}|{_AMOUNT_TOKEN}\s*{_MONEY_TOKEN}))){_CONTROL_TAIL}", re.I,
)
_UNSUPPORTED_NEGATION = re.compile(rf"\b(?:do\s+not|don't|must\s+not|never|no\s+(?!added\s+sugar\b))\b{_CONTROL_TAIL}", re.I)


def extract_controls(text: str) -> tuple[str, float | None, bool, bool, list[str]]:
    """Return product text, EUR cap, substitution permission, clarification flag and notes.

    The deterministic fallback supports explicit EUR caps and a conservative no-
    substitution switch. Unsupported controls stop ordering instead of disappearing.
    """
    warnings: list[str] = []
    clarification = False
    substitution_allowed = not bool(_NO_SUBSTITUTION.search(text))
    allow_stated = bool(_ALLOW_SUBSTITUTION.search(text))
    if not substitution_allowed and allow_stated:
        clarification = True
        warnings.append("Conflicting substitution instructions: please confirm whether substitutes are allowed")
    work = _NO_SUBSTITUTION.sub(" ", text)
    work = _ALLOW_SUBSTITUTION.sub(" ", work)
    budgets: list[float] = []

    def budget_clause(match: re.Match) -> str:
        nonlocal clarification
        clause = match.group(0).strip()
        body = re.sub(
            r"^(?:(?:maximum|max|total)\s+)?budget\b|^budjetti\b|"
            r"^spend\s+(?:no\s+more\s+than|at\s+most|up\s+to)|"
            r"^(?:up\s+to|under|within|at\s+most|no\s+more\s+than)", "", clause, flags=re.I,
        )
        body = re.sub(r"^\s*(?:(?:is|of|limit|maximum|max|on|under|within|up\s+to|at\s+most|for\s+the\s+order)\s+)*[:=]?\s*", "", body, flags=re.I)
        money = _MONEY.match(body)
        if not money:
            clarification = True
            warnings.append(f"Budget needs an explicit EUR amount: “{clause}”")
            return " "
        currency = (money.group(1) or money.group(4)).upper()
        amount = float(money.group(2) or money.group(3))
        if currency not in ("€", "EUR", "EURO", "EUROS"):
            clarification = True
            warnings.append(f"Only EUR budgets are supported; please clarify “{clause}”")
            return " "
        if not math.isfinite(amount) or amount <= 0:
            clarification = True
            warnings.append("Budget must be a positive, finite EUR amount")
            return " "
        budgets.append(amount)
        suffix = body[money.end():].strip(" :")
        if suffix and not re.fullmatch(r"(?:excl(?:uding)?\.?\s+VAT|before\s+VAT|and)", suffix, re.I):
            clarification = True
            warnings.append(f"Please clarify this budget condition (totals exclude VAT): “{clause}”")
        return " "

    work = _BUDGET.sub(budget_clause, work)
    if len(set(budgets)) > 1:
        clarification = True
        warnings.append("Conflicting budget amounts: please confirm one maximum EUR total")
    if _UNPARSED_MONEY.search(work):
        clarification = True
        warnings.append("A monetary condition could not be read: please state an explicit EUR budget")
        work = _UNPARSED_MONEY.sub(" ", work)
    unsupported = _UNSUPPORTED_NEGATION.findall(work)
    if unsupported:
        clarification = True
        warnings += [f"Please clarify the unsupported restriction: “{clause.strip()}”" for clause in unsupported]
        work = _UNSUPPORTED_NEGATION.sub(" ", work)
    return work, min(budgets) if budgets else None, substitution_allowed, clarification, warnings


# ----------------------------------------------------------------------------- requirements

_DIET: list[tuple[str, str, object]] = [
    (r"lactose[- ]?free|laktoositon\w*", "claim", "FREE_FROM_LACTOSE"),
    (r"gluten[- ]?free|gluteeniton\w*", "claim", "FREE_FROM_GLUTEN"),
    (r"vegan|vegaani\w*", "claim", "VEGAN"),
    (r"vegetarian|kasvis\w*", "claim", "VEGETARIAN"),
    (r"(?:no|without) added sugar|sugar[- ]?free|sokeroimat\w*", "claim", "NO_ADDED_SUGARS"),
    (r"egg[- ]?free|kananmunaton\w*", "allergen", ["AE"]),
    (r"nut[- ]?free|pähkinätön\w*", "allergen", ["AN", "AP"]),
    (r"milk[- ]?free|dairy[- ]?free|maidoton\w*", "allergen", ["AM"]),
    (r"fish[- ]?free", "allergen", ["AF"]),
    (r"organic|luomu\w*", "label", "EU_ORGANIC_FARMING"),
    (r"finnish|domestic|kotimai\w*|suomalai\w*", "origin", "fi"),
]

_FILLER = set(
    """i we us our my me you need needs needed want wants would like to order orders buy get find please kindly an a the of
    some about around approximately approx roughly at least minimum min max maximum more than option options that meets meet
    requirements requirement based on price prices and availability available best cheapest good quality for kitchen delivered
    delivery deliver by can could total in with which is are be it this these those also too just only fresh our new can't
    we'd i'd let's let us hello hi thanks thank enough per week weekly each every all everything must should products product
    actually make change instead add extra sorry meant mean
    tarvitsen tarvitsemme tarvitaan haluaisin haluamme tilaan tilata tilaamme noin vähintään kiitos meille toimitus toimitettuna
    mennessä ja sekä myös hyvä halvin""".split()
)

# A segment without a quantity only becomes an item if it names something we sell.
_PRODUCT_WORDS = set(
    """milk cream butter cheese yogurt yoghurt quark egg eggs flour sugar salt pepper oil vinegar potato tomato onion carrot cucumber
    lettuce salad cabbage broccoli cauliflower garlic mushroom apple banana orange lemon lime berry strawberry blueberry lingonberry
    raspberry chicken beef pork lamb meat mince minced sausage ham bacon salmon fish tuna shrimp prawn rice pasta noodle bread roll bun
    oat oats porridge coffee tea juice water soda tofu bean beans lentil lentils pea peas corn jam honey chocolate dessert cake sauce
    soup stock spice herb dill parsley basil napkin napkins glove gloves foil detergent drink smoothie fruit vegetable vegetables
    maito kerma voi juusto jogurtti rahka kananmuna muna jauho sokeri suola öljy peruna tomaatti sipuli porkkana kurkku salaatti
    kaali omena banaani appelsiini sitruuna marja mansikka mustikka puolukka vadelma kana broileri nauta sika liha jauheliha makkara
    kinkku pekoni lohi kala tonnikala katkarapu riisi pasta leipä sämpylä kaura puuro kahvi tee mehu vesi hillo hunaja suklaa
    jäätelö kastike keitto liemi tilli persilja""".split()
)

# Finnish inflected forms -> base form, and a few English <-> Finnish pairs for wider recall.
_FI_FORMS = {
    "maitoa": "maito", "kermaa": "kerma", "voita": "voi", "juustoa": "juusto", "jogurttia": "jogurtti", "rahkaa": "rahka",
    "kananmunia": "kananmuna", "munia": "kananmuna", "perunoita": "peruna", "perunaa": "peruna", "tomaatteja": "tomaatti",
    "tomaattia": "tomaatti", "kurkkua": "kurkku", "porkkanoita": "porkkana", "porkkanaa": "porkkana", "sipulia": "sipuli",
    "omenoita": "omena", "leipää": "leipä", "jauhelihaa": "jauheliha", "lohta": "lohi", "kalaa": "kala", "kanaa": "broileri",
    "broileria": "broileri", "riisiä": "riisi", "pastaa": "pasta", "kahvia": "kahvi", "teetä": "tee", "mehua": "mehu",
    "jauhoja": "jauho", "sokeria": "sokeri", "suolaa": "suola", "öljyä": "öljy", "mansikoita": "mansikka", "mustikoita": "mustikka",
}
_EN_FI = {
    "milk": "maito", "cream": "kerma", "cheese": "juusto", "yogurt": "jogurtti", "egg": "kananmuna", "potato": "peruna",
    "tomato": "tomaatti", "onion": "sipuli", "carrot": "porkkana", "cucumber": "kurkku", "chicken": "broileri",
    "salmon": "lohi", "bread": "leipä", "flour": "vehnäjauho", "minced meat": "jauheliha", "strawberry": "mansikka",
}
_FI_EN = {v: k for k, v in _EN_FI.items()} | {"voi": "butter", "kerma": "cream", "juusto": "cheese"}
_KEEP_S = {"swiss", "hummus", "asparagus", "couscous", "brussels", "molasses", "glass", "oats", "peas", "beans", "chips", "lentils"}


def _singular(word: str) -> str:
    if word in _KEEP_S or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _requirements(text: str, constraints: Constraints) -> str:
    for pattern, kind, value in _DIET:
        rx = re.compile(rf"\b(?:{pattern})\b", re.I)
        if rx.search(text):
            if kind == "claim" and value not in constraints.required_claims:
                constraints.required_claims.append(value)
            elif kind == "allergen":
                constraints.free_from_allergens += [v for v in value if v not in constraints.free_from_allergens]
            elif kind == "label" and value not in constraints.required_labels:
                constraints.required_labels.append(value)
            elif kind == "origin":
                constraints.origin_country = value
            text = rx.sub(" ", text)
    return text


def _product_phrase(text: str) -> str:
    words = [w for w in re.findall(r"[a-zäöåé'-]+", text.lower()) if w not in _FILLER and not w.isdigit()]
    words = [_FI_FORMS.get(w, w) for w in words]
    return " ".join(_singular(w) for w in words).strip(" -'")


def _is_product(phrase: str) -> bool:
    for w in phrase.split():
        if w in _PRODUCT_WORDS or _singular(w) in _PRODUCT_WORDS or any(w.startswith(p) for p in _PRODUCT_WORDS if len(p) >= 5):
            return True
    return False


def _search_phrases(phrase: str) -> list[str]:
    phrases = [phrase]
    if phrase in _EN_FI:
        phrases.append(_EN_FI[phrase])
    elif phrase in _FI_EN:
        phrases.append(_FI_EN[phrase])
    return phrases


# ----------------------------------------------------------------------------- main entry


def query_phrases(query: str) -> list[str]:
    """Free-text catalog search ("Finnish potatoes") -> search phrases (["potato", "peruna"])."""
    phrase = _product_phrase(_requirements(query, Constraints())) or query.strip().lower()
    return _search_phrases(phrase)


def parse(text: str, today: date) -> ParsedRequest:
    warnings: list[str] = []
    work = normalise_numbers(text)
    delivery, work = extract_date(work, today)
    if delivery is not None and delivery < today:
        warnings.append(f"The delivery date {delivery:%d.%m.%Y} has passed: please choose another day")
        delivery = None  # ask again rather than order for a past date (or silently for next year)
    work, budget, substitutions, clarification, control_warnings = extract_controls(work)
    warnings.extend(control_warnings)
    assumed = delivery is None
    if assumed:
        delivery = next_delivery_day(today)
        warnings.append("No delivery date given: assumed the next weekday")

    items: list[RequestItem] = []
    global_constraints = Constraints()
    for sentence in re.split(r"(?<!\d)[.!?]+(?!\d)|\n+", work):
        sentence_constraints = Constraints()
        sentence_items: list[RequestItem] = []
        for segment in re.split(r"\s*(?:,|;|\band\b|\bja\b|\bsekä\b|\bplus\b|&|\+)\s*", sentence, flags=re.I):
            if not segment.strip():
                continue
            original = segment.strip()
            servings = _SERVINGS_RE.search(segment)
            if servings:
                segment = segment[: servings.start()] + " " + segment[servings.end():]
            constraints = Constraints()
            segment = _requirements(segment, constraints)
            qty_match = _QTY_RE.search(segment)
            quantity = unit = None
            assumption = ""
            if qty_match:
                quantity = float(qty_match.group("qty"))
                if qty_match.group("unit"):
                    unit, factor = _unit_of(qty_match.group("unit"))
                    quantity = round(quantity * factor, 4)
                else:
                    unit = "SALES_UNIT"
                    assumption = "No unit given: assumed sales units (cases or pieces)"
                segment = segment[: qty_match.start()] + " " + segment[qty_match.end():]
                if not math.isfinite(quantity) or quantity <= 0:
                    quantity = unit = None
                    assumption = "Quantity must be positive and finite: please state the amount"
                    clarification = True
                    warnings.append(assumption)
            phrase = _product_phrase(segment)
            if not phrase or (quantity is None and not _is_product(phrase)):
                # requirements stated on their own ("everything must be nut-free") apply to the whole sentence
                if not phrase and (constraints.required_claims or constraints.free_from_allergens or constraints.required_labels or constraints.origin_country):
                    sentence_constraints = _merge(sentence_constraints, constraints)
                elif phrase:
                    warnings.append(f"Ignored “{original}” (not recognised as a product)")
                continue
            if servings and quantity is None:
                assumption = f"Amount given as {servings.group(1)} servings: please state the quantity"
            sentence_items.append(
                RequestItem(text=original, product_query=phrase, search_phrases=_search_phrases(phrase),
                            quantity=quantity, unit=unit, constraints=constraints, assumption=assumption)
            )
        for it in sentence_items:
            it.constraints = _merge(it.constraints, sentence_constraints)
        if not sentence_items:
            global_constraints = _merge(global_constraints, sentence_constraints)
        items += sentence_items

    for it in items:
        it.constraints = _merge(it.constraints, global_constraints)
        if it.quantity is None:
            warnings.append(f"No quantity for “{it.product_query}”")
    if not items:
        warnings.append("No products recognised: try e.g. “200 litres of lactose-free milk by Monday”")
    return ParsedRequest(
        items=items, delivery_date=delivery, delivery_date_assumed=assumed,
        budget_eur=budget, substitution_allowed=substitutions, clarification_required=clarification,
        parser="rules", warnings=warnings,
    )


def stated_amounts(text: str, today: date) -> list[tuple[float, str]]:
    """Amounts with an explicit unit, in order: "200 litres and 5 kg" -> [(200.0, "L"), (5.0, "KG")].

    For follow-up answers that name no product. Dates and money are removed first.
    """
    work = normalise_numbers(text)
    _, work = extract_date(work, today)
    work = extract_controls(work)[0]
    out: list[tuple[float, str]] = []
    for m in _QTY_RE.finditer(work):
        if not m.group("unit"):
            continue
        unit, factor = _unit_of(m.group("unit"))
        quantity = round(float(m.group("qty")) * factor, 4)
        if math.isfinite(quantity) and quantity > 0:
            out.append((quantity, unit))
    return out


def _merge(a: Constraints, b: Constraints) -> Constraints:
    return Constraints(
        required_claims=list(dict.fromkeys(a.required_claims + b.required_claims)),
        free_from_allergens=list(dict.fromkeys(a.free_from_allergens + b.free_from_allergens)),
        required_labels=list(dict.fromkeys(a.required_labels + b.required_labels)),
        origin_country=a.origin_country or b.origin_country,
    )
