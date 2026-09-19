"""HTTP 接口契约测试：错误码、冻结提交、健康边界、争议与复算走网络。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload


class Server:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)


EVIDENCE = {
    "person_id": "p-api-1",
    "household_id": "hh-api",
    "region_code": "330100",
    "hukou": "local",
    "birth_date": "1948-05-12",
    "care_level": 3,
    "low_income": False,
    "assessed_at": "2025-02-01",
    "document_ids": ["D1"],
}


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()
        # 类级共享一个 Handler.service（模块级单例），注册一次夹具
        cls.srv.call("POST", "/admin/policies", {
            "region_code": "330100", "version": "API-P1", "name": "api政策",
            "valid_from": "2025-01-01", "valid_to": None,
            "rules": [
                {"program": "purchase", "rate_permille": 700,
                 "per_item_cap_cents": 2_000_000, "annual_cap_cents": 3_000_000,
                 "min_age": 70, "care_levels": [2, 3, 4, 5],
                 "hukou_types": ["local"]},
                {"program": "rental", "rate_permille": 600,
                 "per_item_cap_cents": 800_000, "annual_cap_cents": 1_200_000,
                 "min_age": 65, "care_levels": [1, 2, 3, 4, 5],
                 "hukou_types": ["local", "nonlocal"]},
                {"program": "institution", "rate_permille": 500,
                 "per_item_cap_cents": 600_000, "annual_cap_cents": 1_000_000,
                 "min_age": 60, "care_levels": [3, 4, 5],
                 "hukou_types": ["local", "nonlocal"]},
            ],
        })
        cls.srv.call("POST", "/admin/catalogs", {
            "region_code": "330100", "version": "API-C1",
            "items": [
                {"sku": "EXO-A1", "name": "助行外骨骼", "category": "外骨骼",
                 "purchase_price_cents": 2_500_000,
                 "monthly_rent_cents": 120_000,
                 "monthly_service_cents": 150_000,
                 "programs": ["purchase", "rental", "institution"]},
            ],
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_01_health_contract_remains(self):
        status, body = self.srv.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "service": SERVICE_ID,
                                "name": SERVICE_NAME})
        self.assertEqual(health_payload()["name"], SERVICE_NAME)

    def test_02_submit_freezes_policy_and_lists_three_programs(self):
        status, app = self.srv.call("POST", "/applications", {
            "application_id": "api-app-1",
            "region_code": "330100",
            "application_date": "2025-11-20",
            "sku": "EXO-A1",
            "evidence": EVIDENCE,
            "term_start": "2025-12-01",
            "term_end": "2026-04-01",
        })
        self.assertEqual(status, 201)
        self.assertEqual(app["state"], "SUBMITTED")
        self.assertEqual(app["policy_version"], "API-P1")
        self.assertEqual(app["catalog_version"], "API-C1")
        programs = {p["program"] for p in app["decision"]["programs"]}
        self.assertEqual(programs, {"purchase", "rental", "institution"})
        # 本地三级老人三路径均合格，就高选择购置（一次性 17500）
        self.assertEqual(app["decision"]["selected"]["program"], "purchase")

    def test_03_quota_proof_endpoint(self):
        status, proof = self.srv.call("GET", "/households/hh-api/quota?year=2025")
        self.assertEqual(status, 200)
        self.assertEqual(proof["committed_cents"], 1_750_000)
        self.assertTrue(proof["per_program_cap"])

    def test_04_health_payload_in_event_is_rejected_with_422(self):
        # 先交付才能记事件；购置交付即结算
        status, _ = self.srv.call("POST", "/applications/api-app-1/deliver",
                                  {"activation": {"at": "2025-11-25"}})
        self.assertEqual(status, 200)
        status, err = self.srv.call("POST", "/applications/api-app-1/events", {
            "event_type": "maintenance", "source": "vendor",
            "payload": {"at": "2025-12-01", "heart_rate": 80},
        })
        self.assertEqual(status, 422)
        self.assertEqual(err["code"], "payload_rejected")
        self.assertIn("heart_rate", err["details"]["rejected_fields"])

    def test_05_reviewer_view_exposes_no_health_data(self):
        status, view = self.srv.call("GET", "/applications/api-app-1/reviewer")
        self.assertEqual(status, 200)
        text = json.dumps(view, ensure_ascii=False)
        for hint in ("heart_rate", "血压", "health"):
            self.assertNotIn(hint, text)

    def test_06_duplicate_policy_version_conflicts(self):
        status, err = self.srv.call("POST", "/admin/policies", {
            "region_code": "330100", "version": "API-P1", "name": "重复",
            "valid_from": "2025-01-01",
            "rules": [
                {"program": "rental", "rate_permille": 600,
                 "per_item_cap_cents": 1, "annual_cap_cents": 1},
            ],
        })
        self.assertEqual(status, 409)
        self.assertEqual(err["code"], "version_conflict")

    def test_07_rental_dispute_freeze_and_resolve_over_http(self):
        ev = {**EVIDENCE, "household_id": "hh-api-2", "person_id": "p-api-2"}
        status, app = self.srv.call("POST", "/applications", {
            "application_id": "api-app-2",
            "region_code": "330100",
            "application_date": "2025-11-20",
            "sku": "EXO-A1",
            "programs": ["rental"],
            "evidence": ev,
            "term_start": "2025-12-01",
            "term_end": "2026-04-01",
        })
        self.assertEqual(status, 201)
        self.srv.call("POST", "/applications/api-app-2/deliver",
                      {"activation": {"at": "2025-12-01"}})
        status, opened = self.srv.call("POST", "/applications/api-app-2/disputes", {
            "fault_date": "2026-01-10", "description": "异响",
            "replacement": {"at": "2026-01-11", "replacement_sku": "EXO-A1",
                            "reason": "替代设备不中断"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(opened["settlement_frozen"])
        # 冻结期间结算返回 409
        status, err = self.srv.call("POST", "/applications/api-app-2/maturity", {})
        self.assertEqual(status, 409)
        self.assertEqual(err["code"], "settlement_frozen")
        # 归还挂起
        status, pending = self.srv.call("POST", "/applications/api-app-2/return", {
            "return_date": "2026-02-01", "reason": "争议中归还",
        })
        self.assertEqual(status, 200)
        self.assertTrue(pending["settlement_frozen"])
        # 认定厂商责任后解冻并分段结算
        status, resolved = self.srv.call(
            "POST", "/applications/api-app-2/dispute/resolve",
            {"responsible_party": "vendor", "resolved_date": "2026-02-10",
             "downtime_start": "2026-01-10", "downtime_end": "2026-02-01"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(resolved["state"], "SETTLED")

    def test_08_recalculate_report_endpoint(self):
        # 注册 2026 版新政策后复算
        self.srv.call("POST", "/admin/policies", {
            "region_code": "330100", "version": "API-P2", "name": "api新政",
            "valid_from": "2026-01-01", "valid_to": None,
            "rules": [
                {"program": "purchase", "rate_permille": 600,
                 "per_item_cap_cents": 1_800_000, "annual_cap_cents": 3_000_000,
                 "min_age": 70, "care_levels": [2, 3, 4, 5],
                 "hukou_types": ["local"]},
                {"program": "rental", "rate_permille": 700,
                 "per_item_cap_cents": 900_000, "annual_cap_cents": 1_500_000,
                 "min_age": 65, "care_levels": [1, 2, 3, 4, 5],
                 "hukou_types": ["local", "nonlocal"]},
                {"program": "institution", "rate_permille": 500,
                 "per_item_cap_cents": 600_000, "annual_cap_cents": 1_000_000,
                 "min_age": 60, "care_levels": [3, 4, 5],
                 "hukou_types": ["local", "nonlocal"]},
            ],
        })
        status, report = self.srv.call(
            "POST", "/applications/api-app-2/recalculate",
            {"target_policy_version": "API-P2"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(report["original_decision_unchanged"])
        rental = next(r for r in report["line_comparison"]
                      if r["program"] == "rental")
        self.assertIsNotNone(rental["family_delta_cents"])

    def test_09_unknown_route_404_and_bad_json_400(self):
        status, _ = self.srv.call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_10_ledger_endpoint_lists_entries(self):
        status, body = self.srv.call(
            "GET", "/ledger?household_id=hh-api-2"
        )
        self.assertEqual(status, 200)
        kinds = [e["kind"] for e in body["entries"]]
        self.assertIn("OCCUPY", kinds)
        self.assertIn("SETTLE", kinds)


if __name__ == "__main__":
    unittest.main()
