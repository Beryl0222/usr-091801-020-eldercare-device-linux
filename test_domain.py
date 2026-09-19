"""领域层全场景测试。

覆盖需求要点：
- 申请日冻结资格证据、政策版本与设备目录；
- 三条路径资格规则（年龄/照护/户籍/低收入/地区/生效期/目录/重复享受）；
- 比例就高不重复、单件上限、跨年按日分段；
- 年度额度并发占用不超占、批量原子、撤回/提前归还/复核失败冲正；
- 争议冻结结算但不中断替代设备；
- 健康数据边界与审核员视图；
- 政策调整后复算逐项对比与额度证明；台账只追加。
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from domain import fixtures
from domain.application import BenefitService
from domain.errors import (
    InvalidTransition,
    PayloadRejected,
    QuotaExceeded,
    SettlementFrozen,
    VersionConflict,
)
from domain.money import daily_from_monthly, ratio_amount, split_by_year, yuan
from domain.policy import (
    REASON_AGE_BELOW_MINIMUM,
    REASON_CARE_LEVEL_MISMATCH,
    REASON_DUPLICATE_BENEFIT,
    REASON_HUKOU_MISMATCH,
    REASON_LOW_INCOME_REQUIRED,
    REASON_POLICY_NOT_IN_FORCE,
)

LOCAL = {**fixtures.SAMPLE_LOCAL_FAMILY["evidence"]}
NONLOCAL = {**fixtures.SAMPLE_NONLOCAL_FAMILY["evidence"]}


def new_service():
    svc = BenefitService()
    fixtures.seed(svc)
    return svc


def rental_app(svc, app_id="app-1", evidence=None, sku="EXO-A1",
               start="2025-12-01", end="2026-04-01", apply_date="2025-11-20",
               household_id=None, programs=("rental",), region_code="330100"):
    ev = dict(evidence or LOCAL)
    if household_id:
        ev["household_id"] = household_id
    return svc.submit_application(
        {
            "application_id": app_id,
            "region_code": region_code,
            "application_date": apply_date,
            "sku": sku,
            "programs": list(programs),
            "evidence": ev,
            "term_start": start,
            "term_end": end,
        }
    )


def purchase_app(svc, app_id, sku="EXO-A1", evidence=None, apply_date="2025-11-20",
                 care_level=None):
    ev = dict(evidence or LOCAL)
    if care_level is not None:
        ev["care_level"] = care_level
    return svc.submit_application(
        {
            "application_id": app_id,
            "region_code": "330100",
            "application_date": apply_date,
            "sku": sku,
            "programs": ["purchase"],
            "evidence": ev,
        }
    )


class MoneyToolsTest(unittest.TestCase):
    def test_ratio_uses_half_up_integer_cents(self):
        # 122295 × 60% = 73377；全程整数
        self.assertEqual(ratio_amount(122_295, 600), 73_377)
        self.assertEqual(ratio_amount(100, 5), 1)  # 0.5 分四舍五入
        self.assertEqual(ratio_amount(3, 500), 2)

    def test_daily_rate_and_year_split(self):
        self.assertEqual(daily_from_monthly(120_000), 3945)  # 1200元/月 → 39.45元/日
        segs = split_by_year(date(2025, 12, 1), date(2026, 4, 1))
        self.assertEqual([(s[0], s[3]) for s in segs], [(2025, 31), (2026, 90)])
        with self.assertRaises(ValueError):
            split_by_year(date(2026, 4, 1), date(2025, 12, 1))

    def test_yuan_formatting(self):
        self.assertEqual(yuan(73_377), "733.77")
        self.assertEqual(yuan(-500), "-5.00")


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_rejection_reasons_collected_per_program(self):
        # 非本地户籍、67 岁、一级照护：购置路径因户籍+年龄+照护不合格，仅租赁合格
        app = rental_app(self.svc, "app-nl", evidence=NONLOCAL,
                         programs=("purchase", "rental", "institution"))
        by = {r["program"]: r for r in app["decision"]["programs"]}
        self.assertTrue(by["rental"]["eligible"])
        self.assertFalse(by["purchase"]["eligible"])
        purchase_reasons = set(by["purchase"]["rejection_reasons"])
        self.assertIn(REASON_HUKOU_MISMATCH, purchase_reasons)
        self.assertIn(REASON_AGE_BELOW_MINIMUM, purchase_reasons)
        self.assertIn(REASON_CARE_LEVEL_MISMATCH, purchase_reasons)
        # 机构路径要求三级以上
        self.assertFalse(by["institution"]["eligible"])
        self.assertIn(REASON_CARE_LEVEL_MISMATCH, by["institution"]["rejection_reasons"])

    def test_age_based_on_application_date(self):
        # 1955-12-31 出生，2025-11-20 申请时 69 岁，购置要求 70
        ev = {**LOCAL, "birth_date": "1955-12-31"}
        app = purchase_app(self.svc, "app-age", evidence=ev, care_level=3)
        self.assertTrue(app["decision"]["rejected"])
        self.assertIn(
            REASON_AGE_BELOW_MINIMUM, app["decision"]["rejection_reasons"]
        )

    def test_policy_version_effective_window(self):
        # 2024 年申请无生效政策
        app = rental_app(self.svc, "app-old", apply_date="2024-12-31",
                         start="2024-12-31", end="2025-02-01")
        reasons = app["decision"]["rejection_reasons"]
        self.assertIn(REASON_POLICY_NOT_IN_FORCE, reasons)
        self.assertEqual(app["state"], "REJECTED")

    def test_low_income_only_rule(self):
        policy = {
            "region_code": "330200",
            "version": "NB-X",
            "name": "宁波低收入专项",
            "valid_from": "2025-01-01",
            "valid_to": None,
            "rules": [
                {
                    "program": "rental",
                    "rate_permille": 900,
                    "per_item_cap_cents": 1_000_000,
                    "annual_cap_cents": 2_000_000,
                    "low_income_only": True,
                    "hukou_types": ["local", "nonlocal"],
                }
            ],
        }
        catalog = {
            "region_code": "330200",
            "version": "NB-C1",
            "items": [
                {"sku": "X", "name": "x", "purchase_price_cents": 1,
                 "monthly_rent_cents": 10_000, "monthly_service_cents": 0}
            ],
        }
        self.svc.register_policy(policy)
        self.svc.register_catalog(catalog)
        ev = {**NONLOCAL, "region_code": "330200", "low_income": False}
        app = rental_app(self.svc, "app-li-1", evidence=ev, sku="X",
                         start="2025-06-01", end="2025-08-01",
                         apply_date="2025-05-01", region_code="330200")
        self.assertIn(REASON_LOW_INCOME_REQUIRED,
                      app["decision"]["programs"][0]["rejection_reasons"])
        ev2 = {**ev, "low_income": True}
        app2 = rental_app(self.svc, "app-li-2", evidence=ev2, sku="X",
                          start="2025-06-01", end="2025-08-01",
                          apply_date="2025-05-01", region_code="330200")
        self.assertTrue(app2["decision"]["selected"]["eligible"])

    def test_duplicate_device_benefit_blocked_while_active(self):
        rental_app(self.svc, "app-dup-1", household_id="hh-dup")
        again = rental_app(self.svc, "app-dup-2", household_id="hh-dup")
        self.assertIn(REASON_DUPLICATE_BENEFIT,
                      again["decision"]["programs"][0]["rejection_reasons"])
        self.assertEqual(again["state"], "REJECTED")

    def test_versioned_policy_and_catalog_are_immutable(self):
        with self.assertRaises(VersionConflict):
            self.svc.register_policy(fixtures.POLICY_HZ_2025)
        with self.assertRaises(VersionConflict):
            self.svc.register_catalog(fixtures.CATALOG_HZ_V1)


class QuoteAndCapTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_purchase_rate_and_per_item_cap(self):
        # EXO-A1 25000 元 × 70% = 17500 元，未触单件上限 20000
        app = purchase_app(self.svc, "app-buy")
        sel = app["decision"]["selected"]
        self.assertEqual(sel["program"], "purchase")
        self.assertEqual(sel["subsidy_cents"], 1_750_000)
        self.assertEqual(sel["family_cents"], 750_000)
        seg = sel["yearly"][0]
        self.assertFalse(seg["cap_clipped"])

    def test_per_item_cap_clips_subsidy(self):
        # 自定义高价设备：30000 × 70% = 21000，被单件上限 20000 截断
        self.svc.register_catalog(
            {
                "region_code": "330100",
                "version": "CAT-V2",
                "items": [
                    {
                        "sku": "EXO-PRO",
                        "name": "顶配外骨骼",
                        "purchase_price_cents": 3_000_000,
                        "monthly_rent_cents": 200_000,
                        "monthly_service_cents": 200_000,
                    }
                ],
            }
        )
        app = purchase_app(self.svc, "app-cap", sku="EXO-PRO")
        sel = app["decision"]["selected"]
        self.assertEqual(sel["subsidy_cents"], 2_000_000)
        self.assertTrue(sel["yearly"][0]["cap_clipped"])
        self.assertEqual(sel["family_cents"], 1_000_000)

    def test_highest_rate_wins_and_only_one_program_paid(self):
        # 同一租赁事实下显式试三条路径：合格路径中金额最高者中选，仅一条占用
        app = rental_app(
            self.svc, "app-best",
            programs=("purchase", "rental", "institution"),
            start="2025-12-01", end="2026-02-01",
        )
        sel = app["decision"]["selected"]
        eligible = [p for p in app["decision"]["programs"] if p["eligible"]]
        self.assertEqual(max(p["subsidy_cents"] for p in eligible), sel["subsidy_cents"])
        # 台账中只存在中选路径一笔占用
        entries = self.svc.ledger.entries(application_id="app-best")
        self.assertTrue(all(e["kind"] == "OCCUPY" for e in entries))
        self.assertEqual(len(entries), len(sel["yearly"]))

    def test_cross_year_rental_split_by_actual_days(self):
        app = rental_app(self.svc, "app-xyear")
        sel = app["decision"]["selected"]
        by_year = {s["year"]: s for s in sel["yearly"]}
        self.assertEqual(by_year[2025]["days"], 31)
        self.assertEqual(by_year[2026]["days"], 90)
        # 2025 段：3945×31=122295，60%=73377
        self.assertEqual(by_year[2025]["subsidy_cents"], 73_377)
        # 2026 段：3945×90=355050，60%=213030
        self.assertEqual(by_year[2026]["subsidy_cents"], 213_030)
        proof25 = self.svc.quota_proof("hh-zhang", 2025)
        proof26 = self.svc.quota_proof("hh-zhang", 2026)
        self.assertEqual(proof25["committed_cents"], 73_377)
        self.assertEqual(proof26["committed_cents"], 213_030)

    def test_frozen_snapshot_embedded_in_decision(self):
        app = rental_app(self.svc, "app-freeze")
        frozen = app["decision"]["frozen"]
        self.assertEqual(frozen["policy"]["version"], "HZ-2025")
        self.assertEqual(frozen["device"]["sku"], "EXO-A1")
        self.assertEqual(frozen["evidence"]["region_code"], "330100")
        self.assertEqual(frozen["evidence"]["age_at_application"], 77)


class QuotaConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_concurrent_submissions_never_over_occupy(self):
        # 同家庭两笔不同型号购置：17500 + 12600 = 30100 > 年度上限 30000。
        # 每轮两个申请在同一屏障后并发提交，重复 20 轮：
        # 每轮恰有一笔成功、一笔 QuotaExceeded，额度永不越界。
        def one_round(idx):
            ev1 = {**LOCAL, "household_id": f"hh-race-{idx}",
                   "person_id": f"p-{idx}-1"}
            ev2 = {**LOCAL, "household_id": f"hh-race-{idx}",
                   "person_id": f"p-{idx}-2", "care_level": 4}
            gate = threading.Barrier(2)
            outcomes = []

            def submit(sku, ev, aid):
                gate.wait()
                try:
                    purchase_app(self.svc, aid, sku=sku, evidence=ev)
                    outcomes.append("ok")
                except QuotaExceeded:
                    outcomes.append("rejected")

            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [
                    pool.submit(submit, "EXO-A1", ev1, f"race-{idx}-a"),
                    pool.submit(submit, "BOT-C2", ev2, f"race-{idx}-b"),
                ]
                for f in futs:
                    f.result()
            return outcomes

        with ThreadPoolExecutor(max_workers=4) as pool:
            rounds = list(pool.map(one_round, range(20)))
        for idx, outcomes in enumerate(rounds):
            self.assertEqual(sorted(outcomes), ["ok", "rejected"])
            proof = self.svc.quota_proof(f"hh-race-{idx}", 2025)
            # 中选金额不超过购置年度上限 30000 元
            self.assertLessEqual(proof["committed_cents"], 3_000_000)
            self.assertIn(proof["committed_cents"], (1_750_000, 1_260_000))

    def test_rejected_submit_leaves_no_ledger_entry(self):
        purchase_app(self.svc, "base", evidence={**LOCAL, "household_id": "hh-x"})
        before = len(self.svc.ledger.entries(household_id="hh-x"))
        with self.assertRaises(QuotaExceeded):
            purchase_app(
                self.svc, "over", sku="BOT-C2",
                evidence={**LOCAL, "household_id": "hh-x", "person_id": "p2"},
            )
        after = len(self.svc.ledger.entries(household_id="hh-x"))
        self.assertEqual(before, after)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_withdraw_releases_full_reservation_and_keeps_decision(self):
        app = rental_app(self.svc, "app-w", household_id="hh-w")
        occupied = app["decision"]["selected"]["subsidy_cents"]
        view = self.svc.withdraw("app-w")
        self.assertEqual(view["state"], "WITHDRAWN")
        proof = self.svc.quota_proof("hh-w", 2025)
        self.assertEqual(proof["committed_cents"], 0)
        # 原决定保留且内容未被改动
        self.assertEqual(view["decision"]["selected"]["subsidy_cents"], occupied)
        # 撤回后不可再撤回
        with self.assertRaises(InvalidTransition):
            self.svc.withdraw("app-w")

    def test_purchase_settles_on_delivery(self):
        purchase_app(self.svc, "app-p", evidence={**LOCAL, "household_id": "hh-p"})
        view = self.svc.deliver("app-p", {"at": "2025-11-25", "activated_by": "v1"})
        self.assertEqual(view["state"], "DELIVERED")
        proof = self.svc.quota_proof("hh-p", 2025)
        self.assertEqual(proof["reserved_cents"], 0)
        self.assertEqual(proof["settled_cents"], 1_750_000)

    def test_early_return_settles_by_actual_days_and_releases_rest(self):
        app = rental_app(self.svc, "app-r", household_id="hh-r")
        total_reserved = app["decision"]["selected"]["subsidy_cents"]
        self.svc.deliver("app-r", {"at": "2025-12-01"})
        # 提前于 2026-02-01 归还：实际租期 2025-12（31天）+ 2026-01（31天）
        view = self.svc.return_device("app-r", "2026-02-01", reason="提前归还")
        self.assertEqual(view["state"], "SETTLED")
        detail = view["corrections"][-1]["detail"]["segments"]
        by_year = {s["year"]: s for s in detail}
        self.assertEqual(by_year[2025]["subsidy_cents"], 73_377)
        self.assertEqual(by_year[2026]["subsidy_cents"], 73_377)
        # 2026 年预留 213030 中只结算 73377，余额释放
        proof26 = self.svc.quota_proof("hh-r", 2026)
        self.assertEqual(proof26["reserved_cents"], 0)
        self.assertEqual(proof26["settled_cents"], 73_377)
        # 总占用严格小于原预留
        self.assertLess(73_377 * 2, total_reserved)

    def test_maturity_settles_full_reserved_amount(self):
        rental_app(self.svc, "app-m", household_id="hh-m")
        self.svc.deliver("app-m", {"at": "2025-12-01"})
        view = self.svc.settle_maturity("app-m")
        self.assertEqual(view["state"], "SETTLED")
        self.assertEqual(self.svc.quota_proof("hh-m", 2025)["reserved_cents"], 0)
        self.assertEqual(self.svc.quota_proof("hh-m", 2026)["reserved_cents"], 0)
        self.assertEqual(
            self.svc.quota_proof("hh-m", 2026)["settled_cents"], 213_030
        )

    def test_review_failure_prospective_then_retroactive_reversal(self):
        # 场景一：租赁交付后，2026-01-15 复核失败，结算至失败日前，之后释放
        rental_app(self.svc, "app-rv", household_id="hh-rv")
        self.svc.deliver("app-rv", {"at": "2025-12-01"})
        view = self.svc.review_failed("app-rv", "2026-01-15")
        self.assertEqual(view["state"], "REJECTED")
        detail = view["corrections"][-1]["detail"]
        by_year = {s["year"]: s for s in detail["segments"]}
        self.assertEqual(by_year[2025]["normal_days"], 31)
        self.assertEqual(by_year[2026]["normal_days"], 14)
        self.assertEqual(self.svc.quota_proof("hh-rv", 2026)["reserved_cents"], 0)

        # 场景二：购置已结算后复核自始无效 → REVERSE 全额追回，原 SETTLE 保留
        purchase_app(self.svc, "app-pv", evidence={**LOCAL, "household_id": "hh-pv"})
        self.svc.deliver("app-pv", {"at": "2025-11-25"})
        view2 = self.svc.review_failed(
            "app-pv", "2025-12-10", retroactive=True, reason="发现照护等级造假"
        )
        kinds = [e["kind"] for e in self.svc.ledger.entries(application_id="app-pv")]
        self.assertIn("SETTLE", kinds)
        self.assertIn("REVERSE", kinds)
        self.assertEqual(self.svc.quota_proof("hh-pv", 2025)["settled_cents"], 0)
        # 原决定仍可读取且未被改写
        self.assertEqual(
            view2["decision"]["selected"]["subsidy_cents"], 1_750_000
        )
        with self.assertRaises(InvalidTransition):
            self.svc.review_failed("app-pv", "2025-12-11")


class DisputeTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        rental_app(self.svc, "app-d", household_id="hh-d")
        self.svc.deliver("app-d", {"at": "2025-12-01"})

    def test_settlement_frozen_but_replacement_continues(self):
        view = self.svc.open_dispute(
            "app-d", "2026-01-10", "驱动异响",
            replacement_payload={
                "at": "2026-01-11", "replacement_sku": "EXO-A1",
                "reason": "提供替代设备",
            },
        )
        self.assertTrue(view["settlement_frozen"])
        # 替代设备事件已登记，履约不中断
        self.assertEqual(view["events"][-1]["type"], "replacement")
        # 争议期间结算类操作被拒
        with self.assertRaises(SettlementFrozen):
            self.svc.settle_maturity("app-d")
        with self.assertRaises(SettlementFrozen):
            self.svc.return_device("app-d", "2026-02-01")
        # 但维护、故障、归还登记等履约事件不受影响
        evt = self.svc.record_event(
            "app-d", "maintenance", "vendor",
            {"at": "2026-01-20", "technician_id": "t1", "summary": "上门检测"},
        )
        self.assertEqual(evt["type"], "maintenance")
        # 争议中归还：挂起，不解冻
        view = self.svc.return_device_under_dispute("app-d", "2026-02-01")
        self.assertTrue(view["settlement_frozen"])
        self.assertEqual(view["state"], "DELIVERED")

    def test_resolve_dispute_settles_by_responsible_party(self):
        self.svc.open_dispute("app-d", "2026-01-10", "驱动异响")
        self.svc.return_device_under_dispute("app-d", "2026-02-01")
        view = self.svc.resolve_dispute(
            "app-d", "vendor", "2026-02-10",
            downtime_start="2026-01-10", downtime_end="2026-02-01",
        )
        self.assertEqual(view["state"], "SETTLED")
        self.assertFalse(view["settlement_frozen"])
        segs = view["corrections"][-1]["detail"]["settlement"]["segments"]
        seg26 = next(s for s in segs if s["year"] == 2026)
        # 1 月 31 天中 10-31 日共 22 天为厂商责任停用，不计补贴
        self.assertEqual(seg26["vendor_responsible_downtime_days"], 22)
        self.assertEqual(seg26["normal_days"], 9)
        self.assertEqual(seg26["subsidy_cents"], ratio_amount(3945 * 9, 600))

    def test_dispute_resolved_then_maturity_keeps_downtime_exclusion(self):
        # 争议在租期内认定厂商责任并解除，设备继续使用；
        # 届满分段结算时已认定的停用日仍不计补贴
        self.svc.open_dispute("app-d", "2026-01-10", "驱动异响")
        view = self.svc.resolve_dispute(
            "app-d", "vendor", "2026-01-20",
            downtime_start="2026-01-10", downtime_end="2026-01-20",
        )
        self.assertEqual(view["state"], "DELIVERED")
        self.assertFalse(view["settlement_frozen"])
        view = self.svc.settle_maturity("app-d")
        seg26 = next(
            s for s in view["corrections"][-1]["detail"]["segments"]
            if s["year"] == 2026
        )
        # 2026 段共 90 天，其中 10 天厂商责任停用
        self.assertEqual(seg26["vendor_responsible_downtime_days"], 10)
        self.assertEqual(seg26["normal_days"], 80)


class DataBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        rental_app(self.svc, "app-b", household_id="hh-b")
        self.svc.deliver("app-b", {"at": "2025-12-01"})

    def test_health_fields_rejected_as_whole_payload(self):
        bad_payloads = [
            ("maintenance", "vendor",
             {"at": "2025-12-05", "heart_rate": 82}),
            ("fault", "family",
             {"at": "2025-12-06", "blood_pressure": "130/85", "fault_code": "F1"}),
            ("maintenance", "vendor",
             {"at": "2025-12-07", "健康备注": "血压偏高"}),
        ]
        for event_type, source, payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(PayloadRejected) as ctx:
                    self.svc.record_event("app-b", event_type, source, payload)
                self.assertTrue(ctx.exception.details["rejected_fields"])
        # 被拒事件不得留痕
        view = self.svc.get_application("app-b")
        self.assertEqual(
            [e["type"] for e in view["events"]].count("maintenance"), 0
        )

    def test_unknown_event_and_fields_rejected(self):
        with self.assertRaises(PayloadRejected):
            self.svc.record_event(
                "app-b", "health_monitor", "vendor", {"at": "2025-12-05"}
            )
        with self.assertRaises(PayloadRejected):
            self.svc.record_event(
                "app-b", "maintenance", "hospital", {"at": "2025-12-05"}
            )
        with self.assertRaises(PayloadRejected):
            self.svc.record_event(
                "app-b", "maintenance", "vendor", {"at": "2025-12-05", "x": 1}
            )

    def test_allowed_events_and_reviewer_view(self):
        self.svc.record_event(
            "app-b", MAINT_EVT := "maintenance", "vendor",
            {"at": "2025-12-05", "technician_id": "t9",
             "summary": "例行保养", "result": "正常"},
        )
        self.svc.record_event(
            "app-b", "fault", "community",
            {"at": "2026-01-03", "fault_code": "E7",
             "description": "电池衰减", "reported_by": "community"},
        )
        view = self.svc.reviewer_view("app-b")
        types_ = [e["type"] for e in view["events"]]
        self.assertEqual(types_, ["activation", "maintenance", "fault"])
        # 审核视图里只有白名单字段
        for event in view["events"]:
            self.assertEqual(
                set(event["payload"]) - {"at", "activated_by", "technician_id",
                                         "summary", "result", "fault_code",
                                         "description", "reported_by",
                                         "replacement_sku", "reason",
                                         "condition", "received_by"},
                set(),
            )
        # 视图中不存在任何健康类键
        import json
        text = json.dumps(view, ensure_ascii=False)
        for hint in ("heart", "blood", "血压", "心率", "health"):
            self.assertNotIn(hint, text)


class RecalculationTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.app = rental_app(
            self.svc, "app-recalc", household_id="hh-rc",
            programs=("purchase", "rental", "institution"),
        )

    def test_recalc_keeps_original_and_compares_line_by_line(self):
        report = self.svc.recalculate(
            "app-recalc", target_policy_version="HZ-2026"
        )
        self.assertTrue(report["original_decision_unchanged"])
        # 原申请冻结的仍是 2025 版
        self.assertEqual(
            self.svc.get_application("app-recalc")["policy_version"], "HZ-2025"
        )
        rental_row = next(
            r for r in report["line_comparison"] if r["program"] == "rental"
        )
        # 2026 版租赁比例 60%→70%：财政责任上升、家庭自付下降
        self.assertEqual(rental_row["before"]["fiscal_cents"], 73_377 + 213_030)
        self.assertGreater(rental_row["after"]["fiscal_cents"],
                           rental_row["before"]["fiscal_cents"])
        self.assertLess(rental_row["after"]["family_cents"],
                        rental_row["before"]["family_cents"])
        self.assertGreater(rental_row["fiscal_delta_cents"], 0)
        self.assertIn("rejection_reasons", rental_row["after"])

    def test_recalc_with_family_sample_can_newly_reject(self):
        # "before" 始终是原申请冻结的本地家庭（购置合格）；
        # 用非本地、一级照护样例复算后购置路径新被拒绝，租赁仍合格
        report = self.svc.recalculate(
            "app-recalc", target_policy_version="HZ-2026",
            sample_id="family-li",
        )
        purchase_row = next(
            r for r in report["line_comparison"] if r["program"] == "purchase"
        )
        self.assertTrue(purchase_row["before"]["eligible"])
        self.assertFalse(purchase_row["after"]["eligible"])
        self.assertTrue(purchase_row["newly_rejected"])
        self.assertIn(REASON_HUKOU_MISMATCH,
                      purchase_row["after"]["rejection_reasons"])
        rental_row = next(
            r for r in report["line_comparison"] if r["program"] == "rental"
        )
        self.assertTrue(rental_row["after"]["eligible"])

        # 再用一个"2025 合格、2026 被收紧"的样例验证 newly_rejected：
        # 构造 2026 版目录将 EXO-A1 移出购置路径
        self.svc.register_catalog({
            "region_code": "330100",
            "version": "CAT-V2",
            "items": [{
                "sku": "EXO-A1", "name": "助行外骨骼A1", "category": "外骨骼",
                "purchase_price_cents": 2_500_000,
                "monthly_rent_cents": 120_000,
                "monthly_service_cents": 150_000,
                "programs": ["rental", "institution"],
            }],
        })
        report2 = self.svc.recalculate(
            "app-recalc", target_policy_version="HZ-2026",
            target_catalog_version="CAT-V2", sample_id="family-zhang",
        )
        purchase_row2 = next(
            r for r in report2["line_comparison"] if r["program"] == "purchase"
        )
        self.assertTrue(purchase_row2["newly_rejected"])

    def test_recalc_concurrent_applications_quota_proof(self):
        # 同时提交另一家庭大额购置，报告中逐家庭逐年证明不超占
        concurrent = [
            {
                "application_id": "concurrent-1",
                "region_code": "330100",
                "application_date": "2025-11-20",
                "sku": "EXO-A1",
                "programs": ["purchase"],
                "evidence": {**LOCAL, "household_id": "hh-other",
                             "person_id": "p-other"},
            }
        ]
        report = self.svc.recalculate(
            "app-recalc", target_policy_version="HZ-2026",
            concurrent_applications=concurrent,
        )
        self.assertTrue(report["quota_proofs"])
        self.assertTrue(all(not p["over_occupy"] for p in report["quota_proofs"]))
        other = next(p for p in report["quota_proofs"]
                     if p["household_id"] == "hh-other")
        self.assertEqual(other["simulated_pending_cents"], 1_750_000)

    def test_over_occupy_quota_proof_visible_in_simulation(self):
        # 先给家庭落一笔购置，再把一笔超额购置作为并发申请模拟，证明应显示超占
        purchase_app(
            self.svc, "base-rc", sku="EXO-A1",
            evidence={**LOCAL, "household_id": "hh-rc2", "person_id": "p1"},
        )
        target = rental_app(
            self.svc, "target-rc", household_id="hh-rc2",
            programs=("rental",), start="2025-12-01", end="2026-01-15",
        )
        concurrent = [
            {
                "application_id": "would-over",
                "region_code": "330100",
                "application_date": "2025-11-21",
                "sku": "BOT-C2",
                "programs": ["purchase"],
                "evidence": {**LOCAL, "household_id": "hh-rc2",
                             "person_id": "p2", "care_level": 4},
            }
        ]
        report = self.svc.recalculate(
            "target-rc", target_policy_version="HZ-2026",
            concurrent_applications=concurrent,
        )
        proof2025 = next(p for p in report["quota_proofs"]
                         if p["household_id"] == "hh-rc2" and p["year"] == 2025)
        # 基线含购置 17500 元（本模拟剔除 target 自身），再加机器人 12600 元则超占
        self.assertEqual(proof2025["baseline_committed_cents"], 1_750_000)
        self.assertTrue(proof2025["over_occupy"])


class LedgerImmutabilityTest(unittest.TestCase):
    def test_entries_append_only_and_balance_replays(self):
        svc = new_service()
        app = rental_app(svc, "app-l", household_id="hh-l")
        first_seq = svc.ledger.entries(application_id="app-l")[0]["seq"]
        svc.withdraw("app-l")
        entries = svc.ledger.entries(application_id="app-l")
        # OCCUPY 原分录仍在，RELEASE 追加在后，序号严格递增
        self.assertEqual(entries[0]["seq"], first_seq)
        self.assertEqual(entries[0]["kind"], "OCCUPY")
        self.assertTrue(all(
            entries[i]["seq"] < entries[i + 1]["seq"]
            for i in range(len(entries) - 1)
        ))
        self.assertEqual(entries[-1]["kind"], "RELEASE")
        # 重放余额为 0
        self.assertEqual(svc.quota_proof("hh-l", 2025)["committed_cents"], 0)
        self.assertEqual(svc.quota_proof("hh-l", 2026)["committed_cents"], 0)


if __name__ == "__main__":
    unittest.main()
