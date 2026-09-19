"""BenefitService 的领域流程测试：冻结、并发额度、冲正、争议、复算。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from benefits.errors import ConflictError, ValidationError
from benefits.registry import Registry
from benefits.service import BenefitService
from data.seed import load_seed


def fresh_service():
    registry = Registry()
    load_seed(registry)
    return BenefitService(registry)


class FreezeTests(unittest.TestCase):
    def test_decision_freezes_evidence_and_versions(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "BOT-01",
            "apply_on": "2025-06-01", "route": "PURCHASE",
        })
        snap = result["snapshot"]
        self.assertEqual(snap["policy"]["version"], "HZ-2025")
        self.assertEqual(snap["catalog"]["version"], "HZ-CAT-2025")
        self.assertEqual(snap["evidence"]["age"], 80)
        self.assertEqual(snap["device"]["price_cents"], 1_200_000)

        # 档案后来变化，不影响已冻结决定
        svc.registry.upsert_applicant({
            "applicant_id": "A001", "name": "王桂英", "birth_date": "1945-03-10",
            "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
            "care_level": 0, "low_income": False,
            "assessment_ref": "PG-CHANGED", "income_ref": "",
        })
        again = svc.get_application(result["application_id"])
        self.assertEqual(again["snapshot"]["evidence"]["applicant"]["care_level"], 3)
        self.assertEqual(again["decision"]["subsidy_cents"], 300_000)


class QuotaConcurrencyTests(unittest.TestCase):
    def test_concurrent_applications_never_overrun_personal_cap(self):
        svc = fresh_service()
        # A002 在 HZ-2026 的个人年度上限是 8000 元；每笔 3 个月租赁
        # 补贴 350×3=1050 元，40 个并发线程只能成 7 笔（7350 元）。
        barrier = threading.Barrier(40)

        def one(_):
            barrier.wait()
            try:
                return svc.submit({
                    "applicant_id": "A002", "device_id": "BOT-01",
                    "apply_on": "2026-03-01", "route": "RENT",
                    "months": 3, "lease_start": "2026-03-01",
                })
            except ConflictError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=40) as pool:
            outcomes = list(pool.map(one, range(40)))

        approved = [o for o in outcomes if isinstance(o, dict)]
        rejected = [o for o in outcomes if isinstance(o, str)]
        used = svc.quota_status(applicant_id="A002", year=2026)["personal"]["used_cents"]
        self.assertLessEqual(used, 800_000)
        self.assertEqual(used, 105_000 * len(approved))
        self.assertEqual(len(approved), 7)
        self.assertTrue(all(code == "personal_cap_exhausted" for code in rejected))

    def test_concurrent_applications_never_overrun_region_budget(self):
        svc = fresh_service()
        # CD 2026 预算收紧到 6000 元；注册 60 名老人并发申请，只能成 24 笔。
        svc.registry.set_budget("CD", 2026, 600_000)
        for i in range(60):
            svc.registry.upsert_applicant({
                "applicant_id": f"C{i:03d}", "name": f"川籍老人{i}",
                "birth_date": "1945-01-01", "region": "CD", "hukou_region": "CD",
                "has_local_residency": True, "care_level": 3, "low_income": False,
            })
        barrier = threading.Barrier(60)

        def one(idx):
            barrier.wait()
            try:
                return svc.submit({
                    "applicant_id": f"C{idx:03d}", "device_id": "BOT-CD01",
                    "apply_on": "2026-02-01", "route": "RENT",
                    "months": 1, "lease_start": f"2026-02-0{(idx % 9) + 1}",
                })
            except ConflictError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=60) as pool:
            outcomes = list(pool.map(one, range(60)))
        used = svc.quota_status(region="CD", year=2026)["region"]["used_cents"]
        budget = svc.registry.get_budget("CD", 2026)
        self.assertLessEqual(used, budget)
        rejected = [o for o in outcomes if isinstance(o, str)]
        self.assertTrue(rejected)
        self.assertIn("region_budget_exhausted", rejected)

    def test_batch_is_all_or_nothing(self):
        svc = fresh_service()
        # 两笔各 4200 元且租期恰在 2026 自然年内，单看都低于 8000 上限，
        # 合计 8400 才超：必须在同一批内累加预估占用，整批拒绝且零占用。
        requests = [
            {"applicant_id": "A002", "device_id": "BOT-01", "apply_on": "2026-01-01",
             "route": "RENT", "months": 12, "lease_start": "2026-01-01"},
            {"applicant_id": "A002", "device_id": "BOT-01", "apply_on": "2026-01-02",
             "route": "RENT", "months": 12, "lease_start": "2026-01-01"},
        ]
        with self.assertRaises(ConflictError) as ctx:
            svc.submit_batch(requests)
        self.assertEqual(ctx.exception.code, "personal_cap_exhausted")
        self.assertEqual(
            svc.quota_status(applicant_id="A002", year=2026)["personal"]["used_cents"], 0)

    def test_batch_tentative_accumulation_also_covers_region_budget(self):
        svc = fresh_service()
        # HZ 2026 地区预算收紧到 7000 元；不同老人各申请全额落在 2026
        # 年度的 4200 元租赁补贴，单笔都够、合计超额，整批必须拒绝。
        svc.registry.set_budget("HZ", 2026, 700_000)
        requests = [
            {"applicant_id": "A001", "device_id": "BOT-01", "apply_on": "2026-01-01",
             "route": "RENT", "months": 12, "lease_start": "2026-01-01"},
            {"applicant_id": "A002", "device_id": "BOT-01", "apply_on": "2026-01-01",
             "route": "RENT", "months": 12, "lease_start": "2026-01-01"},
        ]
        with self.assertRaises(ConflictError) as ctx:
            svc.submit_batch(requests)
        self.assertEqual(ctx.exception.code, "region_budget_exhausted")
        self.assertEqual(svc.quota_status(region="HZ", year=2026)["region"]["used_cents"], 0)

    def test_unregistered_year_budget_is_open_ended_but_personal_cap_holds(self):
        svc = fresh_service()
        # 租期跨入未登记预算的 2027 年：不得误判为零预算拒批，个人上限仍生效。
        result = svc.submit({
            "applicant_id": "A001", "device_id": "BOT-01",
            "apply_on": "2026-06-01", "route": "RENT",
            "months": 12, "lease_start": "2026-06-05",
        })
        self.assertEqual(result["status"], "APPROVED")
        years = {p["year"] for p in result["decision"]["annual_split"]}
        self.assertIn(2027, years)
        quota = svc.quota_status(region="HZ", year=2027)["region"]
        self.assertIsNone(quota["budget_cents"])
        self.assertIsNone(quota["available_cents"])

    def test_quota_proof_shows_before_and_after(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "EXO-01",
            "apply_on": "2025-11-20", "route": "RENT",
            "months": 6, "lease_start": "2025-12-01",
        })
        proof = result["decision"]["quota_proof"]
        years = {(p["scope"], p["year"]): p for p in proof}
        self.assertEqual(years[("personal", 2025)]["used_before_cents"], 0)
        self.assertEqual(years[("personal", 2025)]["reserve_cents"], 40_000)
        self.assertEqual(years[("region", 2026)]["used_before_cents"], 0)
        self.assertEqual(years[("region", 2026)]["free_after_cents"],
                         2_000_000 - 200_000)


class ReversalTests(unittest.TestCase):
    def test_withdraw_releases_reserved_quota(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "EXO-01",
            "apply_on": "2025-11-20", "route": "RENT",
            "months": 6, "lease_start": "2025-12-01",
        })
        app_id = result["application_id"]
        svc.withdraw(app_id, "老人改变主意")
        self.assertEqual(svc.get_application(app_id)["status"], "WITHDRAWN")
        self.assertEqual(
            svc.quota_status(applicant_id="A001", year=2025)["personal"]["used_cents"], 0)
        self.assertEqual(
            svc.quota_status(applicant_id="A001", year=2026)["personal"]["used_cents"], 0)
        # 原决定分录仍在
        types = [e.type for e in svc.ledger.all()]
        self.assertIn("DECISION", types)
        self.assertIn("WITHDRAWAL", types)

    def test_early_return_prorates_and_releases_remainder(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "EXO-01",
            "apply_on": "2025-11-20", "route": "RENT",
            "months": 6, "lease_start": "2025-12-01",
        })
        app_id = result["application_id"]
        total = result["decision"]["subsidy_cents"]
        svc.record_event(app_id, {"type": "ACTIVATION", "occurred_on": "2025-12-01",
                                  "device_id": "EXO-01"})
        svc.record_event(app_id, {"type": "RETURN", "occurred_on": "2026-02-14",
                                  "device_id": "EXO-01", "condition": "正常"})
        settled = svc.settle(app_id, as_of="2026-02-14")
        self.assertEqual(settled["status"], "SETTLED")
        settled_sum = sum(s["amount_cents"] for s in settled["new_segments"])
        self.assertLess(settled_sum, total)  # 只用了两个半月
        # 地区年度占用 = 实际结算净额（未用部分已释放）
        used_2025 = svc.quota_status(region="HZ", year=2025)["region"]["used_cents"]
        used_2026 = svc.quota_status(region="HZ", year=2026)["region"]["used_cents"]
        self.assertEqual(used_2025 + used_2026, settled_sum)
        self.assertEqual(used_2025, 40_000)

    def test_recheck_failure_claws_back_settled_amount(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A002", "device_id": "BOT-01",
            "apply_on": "2026-03-01", "route": "RENT",
            "months": 3, "lease_start": "2026-03-05",
        })
        app_id = result["application_id"]
        svc.record_event(app_id, {"type": "ACTIVATION", "occurred_on": "2026-03-05",
                                  "device_id": "BOT-01"})
        svc.settle(app_id, as_of="2026-06-04")
        used_before = svc.quota_status(region="HZ", year=2026)["region"]["used_cents"]
        self.assertGreater(used_before, 0)

        svc.registry.upsert_applicant({
            "applicant_id": "A002", "name": "李建国", "birth_date": "1955-07-20",
            "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
            "care_level": 0, "low_income": False, "assessment_ref": "PG-DOWN",
        })
        outcome = svc.recheck(app_id, False, "复核降为能力完好")
        self.assertEqual(outcome["result"], "FAILED")
        self.assertIn("CARE_LEVEL", outcome["failure_codes"])
        self.assertEqual(svc.get_application(app_id)["status"], "REVOKED")
        used_after = svc.quota_status(region="HZ", year=2026)["region"]["used_cents"]
        self.assertEqual(used_after, 0)
        clawbacks = [e for e in svc.ledger.all() if e.type == "CLAWBACK"]
        self.assertTrue(clawbacks)


class FulfillmentAndDisputeTests(unittest.TestCase):
    def _active_rent(self, svc, applicant="A002", apply_on="2026-03-01", months=6):
        result = svc.submit({
            "applicant_id": applicant, "device_id": "BOT-01",
            "apply_on": apply_on, "route": "RENT",
            "months": months, "lease_start": "2026-03-05",
        })
        app_id = result["application_id"]
        svc.record_event(app_id, {"type": "ACTIVATION", "occurred_on": "2026-03-05",
                                  "device_id": "BOT-01"})
        return app_id

    def test_only_delivery_events_accepted(self):
        svc = fresh_service()
        app_id = self._active_rent(svc)
        stored = svc.record_event(app_id, {"type": "MAINTENANCE",
                                           "occurred_on": "2026-04-01",
                                           "device_id": "BOT-01",
                                           "maintenance_type": "年检"})
        self.assertEqual(stored["maintenance_type"], "年检")
        with self.assertRaises(ValidationError):
            svc.record_event(app_id, {"type": "HEALTH_SYNC", "occurred_on": "2026-04-01"})

    def test_health_fields_are_refused_at_the_door(self):
        svc = fresh_service()
        app_id = self._active_rent(svc)
        for payload in (
            {"type": "MAINTENANCE", "heart_rate": 80},
            {"type": "FAULT", "note": "设备上报血压异常"},
            {"type": "ACTIVATION", "device": {"vital_signs": {"spo2": 98}}},
        ):
            with self.assertRaises(ValidationError) as ctx:
                svc.record_event(app_id, payload)
            self.assertEqual(ctx.exception.code, "health_data_forbidden")

    def test_events_require_delivery_first(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A002", "device_id": "BOT-01",
            "apply_on": "2026-03-01", "route": "RENT",
            "months": 3, "lease_start": "2026-03-05",
        })
        with self.assertRaises(ConflictError):
            svc.record_event(result["application_id"],
                             {"type": "FAULT", "occurred_on": "2026-03-06"})

    def test_dispute_freezes_settlement_but_keeps_replacement_going(self):
        svc = fresh_service()
        app_id = self._active_rent(svc)
        fault = svc.record_event(app_id, {"type": "FAULT", "occurred_on": "2026-04-10",
                                          "device_id": "BOT-01", "fault_code": "MOTOR"})
        svc.open_dispute(app_id, ["VENDOR", "FAMILY"], "责任分歧")
        replacement = svc.request_replacement(app_id, "BOT-01-R", fault["event_id"])
        self.assertEqual(replacement["replacement_device_id"], "BOT-01-R")
        # 替代设备可记录启用事件
        svc.record_event(app_id, {"type": "ACTIVATION", "occurred_on": "2026-04-12",
                                  "device_id": "BOT-01-R"})
        with self.assertRaises(ConflictError) as ctx:
            svc.settle(app_id, as_of="2026-04-30")
        self.assertEqual(ctx.exception.code, "dispute_frozen")

        outcome = svc.close_dispute(app_id, "VENDOR", "厂商负责维修",
                                    as_of="2026-04-30")
        self.assertEqual(outcome["dispute"]["responsible_party"], "VENDOR")
        # 解冻后到期分段自动补结
        self.assertTrue(outcome["post_close_settlement"])

    def test_return_during_dispute_settles_after_resolution(self):
        svc = fresh_service()
        app_id = self._active_rent(svc)
        svc.record_event(app_id, {"type": "FAULT", "occurred_on": "2026-04-10",
                                  "device_id": "BOT-01"})
        svc.open_dispute(app_id, ["VENDOR", "COMMUNITY"], "争议")
        svc.record_event(app_id, {"type": "RETURN", "occurred_on": "2026-04-20",
                                  "device_id": "BOT-01", "condition": "损坏"})
        # 争议期归还不触发结算
        self.assertEqual(svc.get_application(app_id)["status"], "ACTIVE")
        outcome = svc.close_dispute(app_id, "VENDOR", "定责")
        self.assertEqual(outcome["status"], "SETTLED")


class ReplayTests(unittest.TestCase):
    def test_replay_historical_with_new_policy_is_read_only(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "BOT-01",
            "apply_on": "2025-06-01", "route": "PURCHASE",
        })
        app_id = result["application_id"]
        entries_before = len(svc.ledger)
        replay = svc.replay(app_id, policy_version="HZ-2026",
                            catalog_version="HZ-CAT-2026")
        self.assertEqual(len(svc.ledger), entries_before)
        self.assertTrue(replay["no_write"])
        self.assertEqual(replay["policy_version"], "HZ-2026")
        self.assertTrue(replay["diff_vs_original"]["changed"])
        # 逐项说明财政责任与家庭自付
        purchase = next(i for i in replay["items"] if i["route"] == "PURCHASE")
        self.assertIn("fiscal_subsidy_yuan", purchase)
        self.assertIn("household_paid_yuan", purchase)
        # 财政责任 + 家庭自付 = 优惠后基数（1100 元）
        self.assertEqual(
            purchase["fiscal_subsidy_cents"] + purchase["household_paid_cents"],
            1_100_000,
        )

    def test_replay_sample_family(self):
        svc = fresh_service()
        replay = svc.replay(sample={
            "applicant_id": "A003", "device_id": "BOT-01",
            "apply_on": "2026-03-01", "route": "RENT",
            "months": 6, "lease_start": "2026-03-10",
        })
        self.assertTrue(replay["no_write"])
        self.assertEqual(replay["mode"], "sample")
        self.assertEqual(replay["winning_route"], "RENT")
        # A003 轻度失能，购置应被拒绝并给出原因
        purchase = next(i for i in replay["items"] if i["route"] == "PURCHASE")
        self.assertFalse(purchase["eligible"])
        self.assertTrue(purchase["reject_reasons"])

    def test_replay_rejected_application_explains_all_reasons(self):
        svc = fresh_service()
        result = svc.submit({"applicant_id": "A004", "device_id": "BOT-01",
                             "apply_on": "2026-03-01"})
        self.assertEqual(result["status"], "DENIED")
        replay = svc.replay(result["application_id"])
        codes = {i["route"]: i["reject_codes"] for i in replay["items"]}
        self.assertIn("HUKOU", codes["PURCHASE"])
        self.assertIn("HUKOU", codes["INSTITUTION"])


class LedgerIntegrityTests(unittest.TestCase):
    def test_chain_verifies_after_many_operations(self):
        svc = fresh_service()
        result = svc.submit({
            "applicant_id": "A001", "device_id": "EXO-01",
            "apply_on": "2025-11-20", "route": "RENT",
            "months": 6, "lease_start": "2025-12-01",
        })
        app_id = result["application_id"]
        svc.record_event(app_id, {"type": "ACTIVATION", "occurred_on": "2025-12-01",
                                  "device_id": "EXO-01"})
        svc.withdraw(app_id)
        report = svc.verify_ledger()
        self.assertTrue(report["ok"], report)

    def test_tampering_is_detected(self):
        svc = fresh_service()
        svc.submit({"applicant_id": "A001", "device_id": "BOT-01",
                    "apply_on": "2026-03-01", "route": "PURCHASE"})
        entry = svc.ledger.all()[1]
        # 直接篡改内存中的冻结分录（模拟外部改写）
        object.__setattr__(entry, "payload", {**entry.payload, "subsidy_cents": 999_999})
        report = svc.verify_ledger()
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
