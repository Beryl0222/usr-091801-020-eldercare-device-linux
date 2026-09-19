"""适老设备权益后端运行入口。

提供：
- GET  /health                         服务身份（保持基线契约）
- POST /admin/policies                 注册不可变政策版本
- POST /admin/catalogs                 注册不可变设备目录
- POST /admin/samples                  注册家庭样例
- POST /applications                   提交申请（冻结证据/政策 + 原子占用额度）
- GET  /applications/{id}              申请详情（决定、冲正、事件、争议）
- GET  /applications/{id}/reviewer     审核员视图（无健康字段）
- POST /applications/{id}/withdraw     撤回（释放预留）
- POST /applications/{id}/deliver      交付启用（购置即结算）
- POST /applications/{id}/events       维护/故障等履约事件（白名单）
- POST /applications/{id}/return       归还（提前/届满，分段结算）
- POST /applications/{id}/maturity     租期届满分段结算
- POST /applications/{id}/review-failed 资格复核失败（冲正/分段）
- POST /applications/{id}/disputes     发起故障责任争议（可同时登记替代设备）
- POST /applications/{id}/dispute/resolve 认定责任并解冻结算
- POST /applications/{id}/recalculate  用新政策/目录/样例复算并做额度证明
- GET  /households/{id}/quota?year=    年度额度证明
- GET  /ledger?household_id=           台账分录查询

运行 ``python3 service.py --check`` 执行端到端自检；
``python3 service.py --port 8000`` 启动 HTTP 服务。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import fixtures
from domain.application import BenefitService
from domain.errors import DomainError

SERVICE_ID = "eldercare-device"
SERVICE_NAME = "养老机器人补贴租赁"


def health_payload():
    """返回服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """JSON over HTTP 的薄适配层；全部业务规则在 domain 内。"""

    service = BenefitService()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    # ------------------------------------------------------------------ #
    def _route(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        query = self._query()
        try:
            if method == "GET" and path == "/health":
                return self._json(200, health_payload())
            if method == "POST" and path == "/admin/policies":
                return self._json(201, self.service.register_policy(self._body()))
            if method == "POST" and path == "/admin/catalogs":
                return self._json(201, self.service.register_catalog(self._body()))
            if method == "POST" and path == "/admin/samples":
                body = self._body()
                return self._json(
                    201,
                    self.service.register_sample(str(body["sample_id"]), body["evidence"]),
                )
            if method == "POST" and path == "/applications":
                return self._json(201, self.service.submit_application(self._body()))
            if method == "GET" and path.startswith("/applications/"):
                app_id, sub = self._sub_resource(path)
                if sub is None:
                    return self._json(200, self.service.get_application(app_id))
                if sub == "reviewer":
                    return self._json(200, self.service.reviewer_view(app_id))
            if method == "POST" and path.startswith("/applications/"):
                app_id, sub = self._sub_resource(path)
                return self._json(200, self._post_action(app_id, sub, self._body()))
            if method == "GET" and path.startswith("/households/"):
                rest = path[len("/households/"):]
                household_id, _, sub = rest.partition("/")
                if sub == "quota":
                    year = int(query.get("year", ["0"])[0])
                    return self._json(200, self.service.quota_proof(household_id, year))
            if method == "GET" and path == "/ledger":
                return self._json(
                    200,
                    {
                        "entries": self.service.ledger.entries(
                            household_id=query.get("household_id", [None])[0]
                        )
                    },
                )
            self._json(
                404, {"code": "not_found", "message": f"未知路径: {path}"}
            )
        except DomainError as exc:
            self._json(exc.http_status, exc.to_dict())
        except (KeyError, ValueError, TypeError) as exc:
            self._json(400, {"code": "bad_request", "message": str(exc)})

    def _post_action(self, app_id, sub, body):
        body = body or {}
        svc = self.service
        if sub == "withdraw":
            return svc.withdraw(app_id, body.get("reason", "申请人撤回"))
        if sub == "deliver":
            return svc.deliver(app_id, body.get("activation", {}))
        if sub == "events":
            return svc.record_event(
                app_id, str(body["event_type"]), str(body["source"]),
                body.get("payload", {}),
            )
        if sub == "return":
            return svc.return_device_under_dispute(
                app_id, str(body["return_date"]),
                reason=body.get("reason", "设备归还"),
                condition=body.get("condition", "正常"),
            )
        if sub == "maturity":
            return svc.settle_maturity(app_id)
        if sub == "review-failed":
            return svc.review_failed(
                app_id, str(body["failure_date"]),
                retroactive=bool(body.get("retroactive", False)),
                reason=body.get("reason", "资格复核未通过"),
            )
        if sub == "disputes":
            return svc.open_dispute(
                app_id, str(body["fault_date"]), str(body.get("description", "")),
                replacement_payload=body.get("replacement"),
            )
        if sub == "dispute/resolve":
            return svc.resolve_dispute(
                app_id, str(body["responsible_party"]),
                str(body["resolved_date"]),
                downtime_start=body.get("downtime_start"),
                downtime_end=body.get("downtime_end"),
                reason=body.get("reason", "争议处理完成"),
            )
        if sub == "recalculate":
            return svc.recalculate(
                app_id,
                target_policy_version=body.get("target_policy_version"),
                target_catalog_version=body.get("target_catalog_version"),
                sample_id=body.get("sample_id"),
                concurrent_applications=body.get("concurrent_applications", ()),
            )
        raise DomainError(f"不支持的操作: {sub}", {"sub_resource": sub})

    @staticmethod
    def _sub_resource(path):
        rest = path[len("/applications/"):]
        app_id, _, sub = rest.partition("/")
        return app_id, sub or None

    def _query(self):
        from urllib.parse import parse_qs

        if "?" in self.path:
            return parse_qs(self.path.split("?", 1)[1])
        return {}

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


# --------------------------------------------------------------------------- #
# 端到端自检
# --------------------------------------------------------------------------- #
def self_check():
    """在内存中走完关键业务闭环并断言结果；供 --check 与测试复用。"""
    svc = BenefitService()
    seed_info = fixtures.seed(svc)

    # 1) 提交跨年社区租赁申请（本地家庭，2025-12 至 2026-03）
    app = svc.submit_application(
        {
            "application_id": "check-rental-01",
            "region_code": "330100",
            "application_date": "2025-11-20",
            "sku": "EXO-A1",
            "programs": ["rental"],
            "evidence": {**fixtures.SAMPLE_LOCAL_FAMILY["evidence"],
                         "document_ids": ["DOC-SELF-CHECK"]},
            "term_start": "2025-12-01",
            "term_end": "2026-04-01",
        }
    )
    assert app["state"] == "SUBMITTED", app
    selected = app["decision"]["selected"]
    assert selected["program"] == "rental", selected["program"]
    years = {seg["year"]: seg["subsidy_cents"] for seg in selected["yearly"]}
    assert set(years) == {2025, 2026}, years

    # 2) 冻结的是申请日生效的 2025 版政策，2026 版注册不影响原决定
    assert app["policy_version"] == "HZ-2025"

    # 3) 额度被逐年占用且未超年度上限
    proof_2025 = svc.quota_proof("hh-zhang", 2025)
    assert proof_2025["committed_cents"] == years[2025]
    assert all(not p["over_occupy"] for p in proof_2025["per_program_cap"])

    # 4) 健康监测字段在事件入口被整份拒绝
    rejected = False
    try:
        svc.record_event(
            "check-rental-01", "maintenance", "vendor",
            {"at": "2025-12-05", "heart_rate": 82, "blood_pressure": "130/85"},
        )
    except Exception as exc:
        rejected = getattr(exc, "code", "") == "payload_rejected"
    assert rejected, "健康字段必须被拒绝"

    # 5) 交付 -> 争议冻结 -> 替代设备不中断 -> 提前归还挂起 -> 认定厂商责任后分段结算
    svc.deliver("check-rental-01", {"at": "2025-12-01", "activated_by": "vendor-9"})
    svc.open_dispute(
        "check-rental-01", "2026-01-10", "关节驱动异响，责任待认定",
        replacement_payload={
            "at": "2026-01-11", "replacement_sku": "EXO-A1",
            "reason": "争议期间提供替代设备，服务不中断",
        },
    )
    assert svc.get_application("check-rental-01")["settlement_frozen"] is True
    svc.return_device_under_dispute(
        "check-rental-01", "2026-02-01", reason="争议期间提前归还"
    )
    resolved = svc.resolve_dispute(
        "check-rental-01", "vendor", "2026-02-10",
        downtime_start="2026-01-10", downtime_end="2026-02-01",
    )
    assert resolved["state"] == "SETTLED", resolved["state"]

    # 6) 厂商责任停用日不产生补贴；结算后年度额度占用下降且台账可重放
    final_proof = svc.quota_proof("hh-zhang", 2026)
    assert final_proof["reserved_cents"] == 0, final_proof
    seg_2026 = next(
        seg
        for seg in next(
            c["detail"]["settlement"]["segments"]
            for c in resolved["corrections"]
            if c["kind"] == "DISPUTE_RESOLVE"
        )
        if seg["year"] == 2026
    )
    assert seg_2026["vendor_responsible_downtime_days"] == 22

    # 7) 撤回路径：另一家庭提交后撤回，预留全额释放
    app2 = svc.submit_application(
        {
            "application_id": "check-rental-02",
            "region_code": "330100",
            "application_date": "2025-11-21",
            "sku": "BOT-C2",
            "evidence": fixtures.SAMPLE_NONLOCAL_FAMILY["evidence"],
            "term_start": "2025-12-01",
            "term_end": "2026-06-01",
        }
    )
    assert app2["decision"]["selected"]["program"] == "rental"
    withdrawn = svc.withdraw("check-rental-02")
    assert withdrawn["state"] == "WITHDRAWN"
    p = svc.quota_proof("hh-li", 2025)
    assert p["committed_cents"] == 0, p

    # 8) 复算：用 2026 版政策重算旧申请，原决定不变，报告逐项对比
    report = svc.recalculate("check-rental-01", target_policy_version="HZ-2026")
    assert report["original_decision_unchanged"] is True
    assert report["target_policy_version"] == "HZ-2026"
    rental_row = next(
        row for row in report["line_comparison"] if row["program"] == "rental"
    )
    assert "family_cents" in rental_row["after"]

    # 9) 累计/并发占用超过年度上限时整笔拒绝且不留任何分录。
    #    外骨骼购置补贴 25000×70%=17500 元；看护机器人 18000×70%=12600 元；
    #    合计 30100 元 > 购置年度上限 30000 元。
    svc.submit_application(
        {
            "application_id": "check-cap-base",
            "region_code": "330100",
            "application_date": "2025-11-22",
            "sku": "EXO-A1",
            "evidence": {**fixtures.SAMPLE_LOCAL_FAMILY["evidence"],
                         "household_id": "hh-cap", "person_id": "person-cap-1"},
        }
    )
    entries_before = len(svc.ledger.entries(household_id="hh-cap"))
    over = False
    try:
        svc.submit_application(
            {
                "application_id": "check-cap-over",
                "region_code": "330100",
                "application_date": "2025-11-24",
                "sku": "BOT-C2",
                "evidence": {**fixtures.SAMPLE_LOCAL_FAMILY["evidence"],
                             "household_id": "hh-cap", "person_id": "person-cap-2",
                             "care_level": 4},
            }
        )
    except DomainError as exc:
        over = exc.code == "quota_exceeded"
        assert exc.details["over_occupy"] is True
        assert exc.details["would_remain_cents"] == -10_000, exc.details
    assert over, "占用超过年度额度时必须拒绝"
    # 被拒申请不得留下任何台账分录
    assert len(svc.ledger.entries(household_id="hh-cap")) == entries_before

    # 10) 台账只追加：最早分录仍然存在且原决定可读取
    entries = svc.ledger.entries(application_id="check-rental-01")
    assert entries and entries[0]["kind"] == "OCCUPY"
    assert svc.get_application("check-rental-01")["decision"]["selected"][
        "program_label"
    ] == "社区租赁补贴"

    return {"seed": seed_info, "checked_applications": 4}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        result = self_check()
        print(f"端到端检查通过：{result}")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
