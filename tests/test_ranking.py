"""Ranking: at (nearly) the same price, the lower carbon footprint goes first."""
import unittest

from contracts.models import Availability, Candidate, PriceInfo
from gateway.planner import _rank, _recommend


def option(pid, total, co2, *, status="ok", meets=True, match=5.0, reliability=0.95):
    return Candidate(product_id=pid, name=pid, sales_unit="ST", quantity=10, line_total=total,
                     price=PriceInfo(unit_price=total / 10), availability=Availability(status=status),
                     co2_per_kg=co2, match_score=match, meets_constraints=meets, delivery_reliability=reliability)


class Co2TieBreakTests(unittest.TestCase):
    def ranked(self, *cands):
        cands = list(cands)
        _rank(cands)
        return [c.product_id for c in cands], cands

    def test_same_price_lower_co2_goes_first_and_is_recommended(self):
        order, cands = self.ranked(option("beef", 100.0, 20.0), option("chicken", 101.0, 4.0))
        self.assertEqual(order, ["chicken", "beef"])
        self.assertEqual(_recommend(cands), "chicken")
        self.assertIn("lower CO₂e than a similarly priced option", cands[0].rank_reason)

    def test_price_difference_above_two_percent_keeps_the_cheaper_first(self):
        order, _ = self.ranked(option("beef", 100.0, 20.0), option("chicken", 105.0, 4.0))
        self.assertEqual(order, ["beef", "chicken"])

    def test_not_a_reason_to_promote_a_worse_option(self):
        cases = {
            "arrives late": dict(status="late"),
            "doesn't match the request": dict(meets=False),
            "much less relevant": dict(match=1.0),
            "much less reliable": dict(reliability=0.2),
        }
        for why, worse in cases.items():
            with self.subTest(why):
                order, _ = self.ranked(option("a", 100.0, 20.0), option("b", 100.0, 4.0, **worse))
                self.assertEqual(order[0], "a")

    def test_missing_or_equal_co2_changes_nothing(self):
        self.assertEqual(self.ranked(option("a", 100.0, None), option("b", 100.5, 4.0))[0], ["a", "b"])
        self.assertEqual(self.ranked(option("a", 100.0, 1.1), option("b", 100.5, 1.1))[0], ["a", "b"])

    def test_several_close_options_are_ordered_by_co2(self):
        order, _ = self.ranked(option("a", 100.0, 9.0), option("b", 100.5, 3.0), option("c", 101.0, 6.0))
        self.assertEqual(order, ["b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
