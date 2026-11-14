"""Regression checks for food/utensil boundaries and sales-unit quantities."""
import tempfile
import unittest
import json
from datetime import date
from pathlib import Path

from contracts.models import Availability, Candidate, Constraints, Customer, ParsedRequest, PriceInfo, RequestItem
from gateway.catalog import Catalog, RAW_PRODUCTS, build_slim, parse_size
from gateway.planner import _content, _recommend, build_offer, order_quantity


def product(pid, name, **overrides):
    p = {
        "id": pid, "name": name, "name_fi": "", "category": "food", "sales_unit": "ST",
        "base_unit": "ST", "units": {"ST": 1}, "content": 1.0, "content_unit": "L",
        "claims": ["FREE_FROM_LACTOSE"], "contains": ["AM"], "free_from": ["ML"],
        "substitutes": [],
    }
    p.update(overrides)
    return p


def raw_product(pid, name, sales="KI", base="ST", factor=30):
    return {
        "id": pid, "salesUnit": sales, "baseUnit": base,
        "units": [{"unitId": sales, "sizeInBaseUnits": factor}],
        "synkkaData": {"names": [{"language": "en", "value": name}]},
    }


class CatalogTests(unittest.TestCase):
    def test_food_search_excludes_jugs_but_explicit_jug_request_works(self):
        catalog = Catalog([
            product("milk", "Valio lactose free milk 1 l"),
            product("jug", "MILK/WATER JUG 1L", category="utensils"),
            product("cleaner", "Milk system cleaner 1 l", category="cleaning"),
        ])
        self.assertEqual([p["id"] for p, _ in catalog.search(["milk"])], ["milk"])
        self.assertEqual([p["id"] for p, _ in catalog.search(["milk jug"])], ["jug"])

    def test_finnish_compound_jug_is_not_milk(self):
        catalog = Catalog([
            product("milk", "Valio milk 1 l", name_fi="Valio maito"),
            product("jug", "Serving vessel 1 l", name_fi="Maitokannu 1 l", category="utensils"),
        ])
        self.assertEqual([p["id"] for p, _ in catalog.search(["maito"])], ["milk"])

    def test_slim_content_counts_explicit_multipack_once(self):
        raw = [
            raw_product("bun", "Burger buns 30x80g"),
            raw_product("milk", "Milk 1 l", factor=5),
            raw_product("milk_pack", "Milk 5x1 l", factor=5),
            raw_product("one_pack", "Milk 5x1 l", sales="ST", factor=1),
            raw_product("conflicting", "Milk 5x1 l", factor=10),
            raw_product("weight_base", "Sauce 500 g", base="KG", factor=6),
            raw_product("jug", "Milk jug 1 l", factor=6),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / RAW_PRODUCTS).write_text(json.dumps(raw))
            slim = {p["id"]: p for p in build_slim(path)}
        self.assertEqual(slim["bun"]["content"], 2.4)
        self.assertEqual(slim["milk"]["content"], 5)
        self.assertEqual(slim["milk_pack"]["content"], 5)
        self.assertEqual(slim["one_pack"]["content"], 5)
        self.assertIsNone(slim["conflicting"]["content"])
        self.assertEqual(slim["weight_base"]["content"], 6)
        self.assertEqual(slim["weight_base"]["content_unit"], "KG")
        self.assertIsNone(slim["jug"]["content"])

    def test_parse_size_keeps_standard_and_multipack_conversion(self):
        self.assertEqual(parse_size("Milk 1 l"), (1, "L"))
        self.assertEqual(parse_size("Milk 5x1 l"), (5, "L"))
        self.assertEqual(parse_size("Buns 30x80g"), (2.4, "KG"))
        self.assertIsNone(parse_size("Milk 0 l"))

    def test_quantity_uses_correct_pack_content(self):
        req = RequestItem(text="100 litres milk", product_query="milk", quantity=100, unit="L")
        case = product("milk", "Milk 5x1l", sales_unit="KI", units={"KI": 5})
        self.assertEqual(order_quantity(req, case, 5, "L"), (20, ""))
        buns = product("bun", "Burger buns 30x80g", sales_unit="KI", units={"KI": 30})
        req = RequestItem(text="4.8kg buns", product_query="bun", quantity=4.8, unit="KG")
        self.assertEqual(order_quantity(req, buns, 2.4, "KG"), (2, ""))

    def test_capacity_cannot_supply_requested_ingredient_volume(self):
        jug = product("jug", "MILK/WATER JUG 1L")
        price = PriceInfo(unit_price=5, comparison_price=5, comparison_unit="L")
        self.assertEqual(_content(jug, price), (None, None))
        req = RequestItem(text="100 litres milk", product_query="milk", quantity=100, unit="L")
        qty, reason = order_quantity(req, jug, 1, "L")
        self.assertIsNone(qty)
        self.assertIn("capacity", reason)
        req.unit = "PCS"
        self.assertEqual(order_quantity(req, jug, None, None), (100, ""))

    def test_pieces_count_what_a_pack_holds(self):
        eggs = RequestItem(text="300 eggs", product_query="egg", quantity=300, unit="PCS")
        box = product("e10", "Kieku Barn egg M10 580g", sales_unit="RAS", base_unit="RAS", units={"RAS": 1, "KI": 24},
                      content=0.58, content_unit="KG")
        self.assertEqual(order_quantity(eggs, box, 0.58, "KG"), (30, ""))  # 30 boxes of 10, not 300 boxes
        case = product("e6", "20 x Kieku Barn egg L6 408g", sales_unit="KI", base_unit="RAS", units={"KI": 20, "RAS": 1})
        self.assertEqual(order_quantity(eggs, case, None, None), (3, ""))  # 120 eggs a case
        napkins = product("n", "Duni 40cm 3-ply napkin 125pcs", sales_unit="PAK", base_unit="PAK", units={"PAK": 1})
        req = RequestItem(text="500 napkins", product_query="napkin", quantity=500, unit="PCS")
        self.assertEqual(order_quantity(req, napkins, None, None), (4, ""))
        aubergine = product("a", "Aubergine L", name_fi="Munakoiso L", sales_unit="RAS", base_unit="RAS", units={"RAS": 1})
        req = RequestItem(text="10 aubergines", product_query="aubergine", quantity=10, unit="PCS")
        self.assertEqual(order_quantity(req, aubergine, None, None), (10, ""))

    def test_a_multi_word_exclusion_rules_out_that_product(self):
        catalog = Catalog([
            product("masc", "Castelli Mascarpone cream cheese 250g", category="cheese"),
            product("edam", "Valio Edam cheese 1 kg", category="cheese"),
        ])
        found = [p["id"] for p, _ in catalog.search(["cheese"], ["cream cheese"])]
        self.assertEqual(found[0], "edam")
        self.assertTrue(catalog.excluded(catalog.by_id["masc"], ["cheese"], ["cream cheese"]))
        self.assertFalse(catalog.excluded(catalog.by_id["edam"], ["cheese"], ["cream cheese"]))
        # what is searched for is never excluded
        self.assertFalse(catalog.excluded(catalog.by_id["masc"], ["cream cheese"], ["cream cheese", "spread"]))

    def test_missing_piece_count_is_not_assumed_one(self):
        req = RequestItem(text="100 pieces", product_query="milk", quantity=100, unit="PCS")
        case = product("milk", "Milk 1 l", sales_unit="KI", units={})
        self.assertIsNone(order_quantity(req, case, 5, "L")[0])

    def test_unknown_content_can_be_cross_checked_by_price(self):
        p = product("pack", "Buns 30x80g", content=None, content_unit=None)
        self.assertEqual(_content(p, PriceInfo(unit_price=12, comparison_price=5, comparison_unit="KG")), (2.4, "KG"))

    def test_nonpositive_quantity_never_produces_one_sales_unit(self):
        # Simulate legacy in-memory data; the request schema also rejects zero at the boundary.
        req = RequestItem.model_construct(text="0 litres", product_query="milk", quantity=0, unit="L")
        self.assertIsNone(order_quantity(req, product("milk", "Milk 1 l"), 1, "L")[0])

    def test_no_default_recommendation_for_unmet_or_unavailable_options(self):
        def candidate(pid, status="ok", meets=True):
            return Candidate(product_id=pid, name=pid, sales_unit="ST", quantity=1, line_total=1,
                             price=PriceInfo(unit_price=1), availability=Availability(status=status), meets_constraints=meets)
        exceptions = [candidate("partial", "partial"), candidate("late", "late"), candidate("mismatch", meets=False)]
        self.assertIsNone(_recommend(exceptions))
        self.assertEqual(_recommend([*exceptions, candidate("feasible")]), "feasible")


class FakeValio:
    base_url = "https://synthetic.test"

    def __init__(self):
        self.queried = []

    async def price(self, ids, requested, customer_id):
        self.queried += ids
        return {pid: {"catalogPrice": {"value": 1.4}, "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L"} for pid in ids}, {}

    async def availability(self, ids, requested, customer_id):
        return {pid: {"scheduleLines": [{"availableAmount": 100000, "availableDate": requested.isoformat()}]} for pid in ids}, {}


class OfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_supplier_substitution_does_not_cross_food_utensil_boundary(self):
        from trust.signing import Signer
        milk = product("milk", "Valio milk 1 l", substitutes=["jug"])
        jug = product("jug", "Milk jug 1 l", category="utensils", contains=[], free_from=[], claims=[])
        customer = Customer(id="test", name="Test", segment="hotel", business_id="", business_id_valid=False, status="trusted")
        parsed = ParsedRequest(items=[RequestItem(text="100L milk", product_query="milk", quantity=100, unit="L")], delivery_date=date(2026, 11, 16))
        valio = FakeValio()
        with tempfile.TemporaryDirectory() as directory:
            offer = await build_offer(customer=customer, parsed=parsed, request_text="100L milk", parse_ms=0,
                                      catalog=Catalog([milk, jug]), valio=valio, signer=Signer(Path(directory) / "key.pem"), ttl_minutes=30)
        self.assertEqual(valio.queried, ["milk"])
        self.assertEqual([c.product_id for c in offer.items[0].candidates], ["milk"])
        self.assertEqual(offer.items[0].recommended_product_id, "milk")
        self.assertEqual(offer.items[0].candidates[0].quantity, 100)

    async def test_conflicting_milk_free_milk_request_cannot_recommend_a_jug(self):
        from trust.signing import Signer
        milk = product("milk", "Valio milk 1 l")
        jug = product("jug", "MILK/WATER JUG 1L", contains=[], free_from=[], claims=[])
        customer = Customer(id="test", name="Test", segment="hotel", business_id="", business_id_valid=False, status="trusted")
        item = RequestItem(text="milk free milk", product_query="milk", quantity=100, unit="L", constraints=Constraints(free_from_allergens=["AM"]))
        parsed = ParsedRequest(items=[item], delivery_date=date(2026, 11, 16))
        valio = FakeValio()
        with tempfile.TemporaryDirectory() as directory:
            offer = await build_offer(customer=customer, parsed=parsed, request_text=item.text, parse_ms=0,
                                      catalog=Catalog([milk, jug]), valio=valio, signer=Signer(Path(directory) / "key.pem"), ttl_minutes=30)
        self.assertEqual(valio.queried, [])
        self.assertEqual(offer.items[0].candidates, [])
        self.assertIsNone(offer.items[0].recommended_product_id)


if __name__ == "__main__":
    unittest.main()
