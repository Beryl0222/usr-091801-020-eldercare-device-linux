"""HTTP 接口端到端测试：服务以真实线程启动，走真实 HTTP 请求。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import service


def _request(method: str, url: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试模块拿到一个干净的领域单例，避免与种子演示数据互相影响。
        cls._original_service = service.SERVICE
        service.SERVICE = service.build_service()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.SERVICE = cls._original_service

    def test_01_health_contract_intact(self):
        status, body = _request("GET", f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, service.health_payload())

    def test_02_submit_freezes_and_explains_money(self):
        status, body = _request("POST", f"{self.base}/applications", {
            "applicant_id": "A001", "device_id": "EXO-01",
            "apply_on": "2025-11-20", "route": "RENT",
            "months": 6, "lease_start": "2025-12-01",
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(body["status"], "APPROVED")
        self.assertEqual(body["snapshot"]["policy"]["version"], "HZ-2025")
        decision = body["decision"]
        self.assertEqual(decision["subsidy_cents"], 240_000)
        self.assertEqual(decision["household_paid_cents"], 660_000)
        # 跨年额度分别占用，且有逐项额度证明
        self.assertEqual({p["year"] for p in decision["annual_split"]}, {2025, 2026})
        self.assertTrue(decision["quota_proof"])
        type(self).rent_app_id = body["application_id"]

    def test_03_denied_application_lists_reject_reasons(self):
        status, body = _request("POST", f"{self.base}/applications", {
            "applicant_id": "A004", "device_id": "BOT-01", "apply_on": "2026-03-01",
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "DENIED")
        self.assertIn("HUKOU", body["evaluation"]["routes"]["PURCHASE"]["reject_codes"])
        self.assertTrue(body["decision"] is None)

    def test_04_quota_reflects_reservation(self):
        status, body = _request(
            "GET", f"{self.base}/quota?{urlencode({'region': 'HZ', 'year': 2025})}")
        self.assertEqual(status, 200)
        self.assertEqual(body["region"]["used_cents"], 40_000)
        self.assertEqual(body["region"]["available_cents"], 19_960_000)

    def test_05_events_activation_fault_return_and_settlement(self):
        app_id = type(self).rent_app_id
        for event in (
            {"type": "ACTIVATION", "occurred_on": "2025-12-01", "device_id": "EXO-01"},
            {"type": "FAULT", "occurred_on": "2026-01-10", "device_id": "EXO-01",
             "fault_code": "BATTERY"},
            {"type": "RETURN", "occurred_on": "2026-02-14", "device_id": "EXO-01",
             "condition": "正常"},
        ):
            status, body = _request("POST", f"{self.base}/applications/{app_id}/events", event)
            self.assertEqual(status, 201, body)
        status, body = _request("POST", f"{self.base}/applications/{app_id}/settle",
                                {"as_of": "2026-02-14"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "SETTLED")
        self.assertLess(sum(s["amount_cents"] for s in body["new_segments"]), 240_000)

    def test_06_health_data_is_rejected(self):
        status, body = _request("POST", f"{self.base}/applications", {
            "applicant_id": "A003", "device_id": "BOT-01", "apply_on": "2026-03-01",
            "route": "RENT", "months": 3, "lease_start": "2026-03-05",
        })
        app_id = body["application_id"]
        _request("POST", f"{self.base}/applications/{app_id}/events",
                 {"type": "ACTIVATION", "occurred_on": "2026-03-05", "device_id": "BOT-01"})
        status, body = _request("POST", f"{self.base}/applications/{app_id}/events",
                                {"type": "MAINTENANCE", "note": "读取心率80"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "health_data_forbidden")

    def test_07_dispute_freezes_settlement_replacement_still_works(self):
        status, body = _request("POST", f"{self.base}/applications", {
            "applicant_id": "A002", "device_id": "BOT-01", "apply_on": "2026-03-01",
            "route": "RENT", "months": 6, "lease_start": "2026-03-05",
        })
        app_id = body["application_id"]
        _request("POST", f"{self.base}/applications/{app_id}/events",
                 {"type": "ACTIVATION", "occurred_on": "2026-03-05", "device_id": "BOT-01"})
        status, fault = _request("POST", f"{self.base}/applications/{app_id}/events",
                                 {"type": "FAULT", "occurred_on": "2026-04-10",
                                  "device_id": "BOT-01", "fault_code": "MOTOR"})
        status, _ = _request("POST", f"{self.base}/applications/{app_id}/disputes",
                             {"parties": ["VENDOR", "FAMILY"], "description": "责任分歧"})
        self.assertEqual(status, 201)
        status, body = _request("POST", f"{self.base}/applications/{app_id}/replacements",
                                {"replacement_device_id": "BOT-01-R",
                                 "reason_fault_event_id": fault["event_id"]})
        self.assertEqual(status, 201)
        self.assertEqual(body["replacement_device_id"], "BOT-01-R")
        status, body = _request("POST", f"{self.base}/applications/{app_id}/settle",
                                {"as_of": "2026-04-30"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "dispute_frozen")
        status, body = _request("POST", f"{self.base}/applications/{app_id}/disputes/close",
                                {"responsible_party": "VENDOR", "as_of": "2026-04-30"})
        self.assertEqual(status, 200)
        self.assertTrue(body["post_close_settlement"])

    def test_08_replay_is_read_only(self):
        status, body = _request("POST", f"{self.base}/applications", {
            "applicant_id": "A001", "device_id": "BOT-01",
            "apply_on": "2025-06-01", "route": "PURCHASE",
        })
        app_id = body["application_id"]
        status, before = _request("GET", f"{self.base}/ledger/verify")
        status, body = _request("POST", f"{self.base}/applications/{app_id}/replay", {
            "policy_version": "HZ-2026", "catalog_version": "HZ-CAT-2026",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["no_write"])
        self.assertEqual(body["policy_version"], "HZ-2026")
        self.assertNotEqual(
            body["recalculated_decision"]["subsidy_cents"],
            body["diff_vs_original"]["original_subsidy_cents"])
        status, sample = _request("POST", f"{self.base}/replays", {
            "applicant_id": "A003", "device_id": "BOT-01",
            "apply_on": "2026-03-01", "route": "RENT",
            "months": 6, "lease_start": "2026-03-10",
        })
        self.assertTrue(sample["no_write"])
        self.assertEqual(sample["winning_route"], "RENT")

    def test_09_concurrent_submissions_cannot_overrun_budget_over_http(self):
        # HZ 2026 预算 20000 元；同一老人多笔并发也不得击穿个人年度上限。
        def one(_):
            return _request("POST", f"{self.base}/applications", {
                "applicant_id": "A002", "device_id": "EXO-02",
                "apply_on": "2026-05-01", "route": "RENT",
                "months": 3, "lease_start": "2026-05-01",
            })
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(one, range(20)))
        statuses = [code for code, _ in responses]
        self.assertIn(409, statuses)
        _, quota = _request(
            "GET", f"{self.base}/quota?{urlencode({'applicant_id': 'A002', 'year': 2026})}")
        self.assertLessEqual(quota["personal"]["used_cents"],
                             quota["personal"]["cap_cents"])

    def test_10_ledger_chain_verifies(self):
        status, body = _request("GET", f"{self.base}/ledger/verify")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)

    def test_11_invalid_route_and_body(self):
        status, _ = _request("GET", f"{self.base}/applications/NO-SUCH")
        self.assertEqual(status, 404)
        req = Request(f"{self.base}/applications", data=b"{not-json",
                      headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


from concurrent.futures import ThreadPoolExecutor


if __name__ == "__main__":
    unittest.main()
