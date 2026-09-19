"""金额工具与资格引擎的纯单元测试。"""

import unittest
from datetime import date

from benefits.eligibility import evaluate
from benefits.models import (
    ApplicationSnapshot,
    Applicant,
    CatalogEntry,
    CatalogVersion,
    ContractTerm,
    EvidenceSnapshot,
    PolicyVersion,
    RouteRule,
    RuleTier,
)
from benefits.money import (
    cents_to_yuan,
    inclusive_days,
    largest_remainder_split,
    split_by_calendar_year,
    yuan_to_cents,
)
from benefits.registry import Registry
from data.seed import load_seed


class MoneyTests(unittest.TestCase):
    def test_yuan_conversion_is_exact(self):
        self.assertEqual(yuan_to_cents("0.07"), 7)
        self.assertEqual(yuan_to_cents("1234.5"), 123450)
        self.assertEqual(yuan_to_cents(3.99), 399)
        self.assertEqual(yuan_to_cents(-2.5), -250)
        self.assertEqual(cents_to_yuan(123456), "1234.56")
        self.assertEqual(cents_to_yuan(-7), "-0.07")
        with self.assertRaises(ValueError):
            yuan_to_cents("1.234")

    def test_largest_remainder_sums_to_total(self):
        for total in range(0, 500):
            parts = largest_remainder_split(total, [31, 28, 31, 30])
            self.assertEqual(sum(parts), total)
        self.assertEqual(largest_remainder_split(100, [1, 1, 1]), [34, 33, 33])

    def test_split_by_calendar_year_covers_each_day_once(self):
        pieces = split_by_calendar_year(date(2025, 12, 1), date(2026, 1, 31), 6200)
        self.assertEqual([p["year"] for p in pieces], [2025, 2026])
        self.assertEqual(sum(p["days"] for p in pieces),
                         inclusive_days(date(2025, 12, 1), date(2026, 1, 31)))
        self.assertEqual(sum(p["amount_cents"] for p in pieces), 6200)


class EligibilityFixture:
    """构造小型可控快照：购置价 2000 元，月租 100 元。"""

    @staticmethod
    def snapshot(*, age=75, care_level=2, low_income=False, hukou="T",
                 residency=True, region="T", months=0, lease_start=None,
                 policy=None, contract_cents=None, discount_cents=0):
        applicant = Applicant(
            applicant_id="X1", name="测试老人",
            birth_date=date(2000 - age, 6, 1),
            region=region, hukou_region=hukou, has_local_residency=residency,
            care_level=care_level, low_income=low_income,
        )
        if policy is None:
            policy = PolicyVersion(
                region="T", version="T-1", effective_from=date(2026, 1, 1),
                personal_annual_cap_cents=300_000,
                rules=(
                    RouteRule(route="PURCHASE", min_age=60, min_care_level=2,
                              require_local_hukou=True,
                              tiers=(
                                  RuleTier("基础档", 5000, one_time_cap_cents=100_000),
                                  RuleTier("低收入档", 7000, one_time_cap_cents=140_000,
                                           low_income_only=True),
                              )),
                    RouteRule(route="RENT", min_age=60, min_care_level=1,
                              require_residency=True, rent_min_months=1,
                              tiers=(RuleTier("租赁档", 4000, monthly_cap_cents=30_000),)),
                    RouteRule(route="INSTITUTION", min_age=70, min_care_level=2,
                              require_local_hukou=True, require_residency=True,
                              tiers=(RuleTier("机构档", 3000, monthly_cap_cents=25_000),)),
                ),
            )
        device = CatalogEntry(
            device_id="D1", name="测试设备", category="机器人",
            allowed_routes=frozenset(("PURCHASE", "RENT", "INSTITUTION")),
            price_cents=200_000, monthly_rent_cents=100_000, monthly_service_cents=100_000,
        )
        catalog = CatalogVersion("T", "TC-1", date(2026, 1, 1), (device,))
        terms = [
            ContractTerm("PURCHASE", date(2026, 3, 1), 200_000,
                         contract_cents or 200_000, discount_cents),
        ]
        if months:
            terms.append(ContractTerm(
                "RENT", date(2026, 3, 1), 100_000 * months,
                100_000 * months, 0, lease_start or date(2026, 3, 1),
                None, months,
            ))
            terms.append(ContractTerm(
                "INSTITUTION", date(2026, 3, 1), 100_000 * months,
                100_000 * months, 0, lease_start or date(2026, 3, 1),
                None, months,
            ))
        evidence = EvidenceSnapshot(applicant, age, date(2026, 3, 1), "PG-1", "LI-1")
        return ApplicationSnapshot(evidence, policy, catalog, device, tuple(terms))


class EligibilityTests(unittest.TestCase):
    def test_higher_ratio_tier_wins_for_low_income(self):
        normal = evaluate(EligibilityFixture.snapshot(low_income=False))
        poor = evaluate(EligibilityFixture.snapshot(low_income=True))
        # 2000 元 × 50% = 1000，× 70% = 1400（均未触顶）
        self.assertEqual(normal.evaluations["PURCHASE"].ratio_bp, 5000)
        self.assertEqual(poor.evaluations["PURCHASE"].ratio_bp, 7000)
        self.assertEqual(normal.evaluations["PURCHASE"].subsidy_cents, 100_000)
        self.assertEqual(poor.evaluations["PURCHASE"].subsidy_cents, 140_000)

    def test_one_time_cap_caps_subsidy(self):
        snapshot = EligibilityFixture.snapshot(low_income=True, contract_cents=1_000_000)
        # 10000 元 × 70% = 7000，超过单件上限 1400
        self.assertEqual(
            evaluate(snapshot).evaluations["PURCHASE"].subsidy_cents, 140_000)

    def test_only_one_route_is_chosen(self):
        snapshot = EligibilityFixture.snapshot(months=6)
        result = evaluate(snapshot)
        chosen = result.winning_route
        self.assertIn(chosen, ("PURCHASE", "RENT", "INSTITUTION"))
        winners = [r for r, ev in result.evaluations.items() if ev.eligible]
        self.assertGreater(len(winners), 1)  # 多条合格，但只有一个胜出
        self.assertEqual(result.winner.subsidy_cents,
                         max(ev.subsidy_cents for ev in result.evaluations.values()))

    def test_reject_reasons_cover_each_gate(self):
        young = evaluate(EligibilityFixture.snapshot(age=55))
        self.assertIn("AGE", young.evaluations["PURCHASE"].reject_codes)

        able = evaluate(EligibilityFixture.snapshot(care_level=0))
        self.assertIn("CARE_LEVEL", able.evaluations["PURCHASE"].reject_codes)
        self.assertIn("CARE_LEVEL", able.evaluations["INSTITUTION"].reject_codes)

        migrant = evaluate(EligibilityFixture.snapshot(hukou="OTHER", residency=False))
        self.assertIn("HUKOU", migrant.evaluations["PURCHASE"].reject_codes)
        self.assertIn("HUKOU", migrant.evaluations["INSTITUTION"].reject_codes)
        self.assertIn("RESIDENCY", migrant.evaluations["RENT"].reject_codes)

        # 户籍外地但持居住证：购置仍拒绝，租赁可以
        resident = evaluate(EligibilityFixture.snapshot(
            hukou="OTHER", residency=True, months=3))
        self.assertIn("HUKOU", resident.evaluations["PURCHASE"].reject_codes)
        self.assertTrue(resident.evaluations["RENT"].eligible)

    def test_discounts_reduce_base_before_ratio(self):
        snapshot = EligibilityFixture.snapshot(contract_cents=200_000, discount_cents=100_000)
        # 优惠后 1000 元 × 50% = 500 元
        self.assertEqual(
            evaluate(snapshot).evaluations["PURCHASE"].subsidy_cents, 50_000)

    def test_monthly_cap_and_split_sum(self):
        snapshot = EligibilityFixture.snapshot(months=2)
        rent = evaluate(snapshot).evaluations["RENT"]
        # 每月 1000 元 × 40% = 400 元，受月上限 300 元截断，两个月共 600 元
        self.assertEqual(rent.subsidy_cents, 60_000)
        self.assertEqual(sum(p["amount_cents"] for p in rent.annual_split), rent.subsidy_cents)

    def test_seed_policy_resolves_by_application_date(self):
        registry = Registry()
        load_seed(registry)
        self.assertEqual(registry.resolve_policy("HZ", date(2025, 6, 1)).version, "HZ-2025")
        self.assertEqual(registry.resolve_policy("HZ", date(2026, 1, 1)).version, "HZ-2026")
        with self.assertRaises(Exception):
            registry.resolve_policy("HZ", date(2024, 12, 31))


if __name__ == "__main__":
    unittest.main()
