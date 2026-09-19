"""养老机器人补贴租赁的运行入口与 HTTP 接口。

路由（均返回 JSON）：
  GET  /health                       健康检查（稳定契约）
  POST /applications                 提交单笔申请（冻结证据/版本，占用额度）
  POST /applications/batch           原子批量提交（整批通过或整批拒绝）
  GET  /applications                 申请列表
  GET  /applications/{id}            申请详情（含决定、事件、分段、预留）
  POST /applications/{id}/withdraw   撤回（冲正预留）
  POST /applications/{id}/events     履约事件（启用/维护/故障/归还）
  POST /applications/{id}/settle     到期分段结算（可提前归还截断）
  POST /applications/{id}/recheck    资格复核（失败追回）
  POST /applications/{id}/disputes   开启故障责任争议（冻结结算）
  POST /applications/{id}/disputes/close  定责并解冻
  POST /applications/{id}/replacements    争议期替代设备
  POST /applications/{id}/replay     用指定版本复算旧申请（只读）
  POST /replays                      用家庭样例试算（只读）
  GET  /quota                        个人/地区年度额度占用
  GET  /ledger/verify                哈希链校验
  GET  /policies|catalogs|applicants 版本与档案查询
  POST /admin/policies|catalogs|applicants|budgets  登记新版本/档案/预算
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benefits.errors import DomainError
from benefits.registry import Registry
from benefits.service import BenefitService
from data.seed import load_seed

SERVICE_ID = "eldercare-device"
SERVICE_NAME = "养老机器人补贴租赁"


def build_service() -> BenefitService:
    registry = Registry()
    load_seed(registry)
    return BenefitService(registry)


SERVICE = build_service()

_STATUS_BY_CODE = {
    "not_found": 404,
    "conflict": 409,
    "invalid_request": 400,
    "immutable_violation": 409,
    "policy_not_in_force": 422,
    "catalog_not_in_force": 422,
    "device_not_cataloged": 422,
    "personal_cap_exhausted": 409,
    "region_budget_exhausted": 409,
    "application_not_active": 409,
    "device_not_delivered": 409,
    "dispute_frozen": 409,
    "health_data_forbidden": 422,
}


def health_payload():
    """返回服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    server_version = "EldercareBenefits/1.0"

    # ---- 基础收发 ----

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}", code="invalid_request")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象", code="invalid_request")
        return data

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_domain_error(self, error: DomainError):
        status = _STATUS_BY_CODE.get(error.code, 400)
        self._send_json({
            "error": error.code,
            "message": str(error),
            "details": error.details,
        }, status=status)

    # ---- 路由 ----

    def do_GET(self):
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        try:
            if path == "/health":
                self._send_json(health_payload())
                return
            if path == "/applications":
                self._send_json({"applications": SERVICE.list_applications()})
                return
            match = re.fullmatch(r"/applications/([^/]+)", path)
            if match:
                self._send_json(SERVICE.get_application(match.group(1)))
                return
            if path == "/quota":
                self._send_json(self._quota(query))
                return
            if path == "/ledger/verify":
                self._send_json(SERVICE.verify_ledger())
                return
            if path == "/policies":
                self._send_json({"policies": SERVICE.registry.export_seed()["policies"]})
                return
            if path == "/catalogs":
                self._send_json({"catalogs": SERVICE.registry.export_seed()["catalogs"]})
                return
            if path == "/applicants":
                self._send_json({"applicants": SERVICE.registry.export_seed()["applicants"]})
                return
            self.send_error(404)
        except DomainError as error:
            self._handle_domain_error(error)

    def do_POST(self):
        from urllib.parse import urlsplit

        try:
            data = self._read_json()
            path = urlsplit(self.path).path
            if path == "/applications":
                self._send_json(SERVICE.submit(data), status=201)
                return
            if self.path == "/applications/batch":
                self._send_json({"applications": SERVICE.submit_batch(data.get("applications", []))},
                                status=201)
                return
            if self.path == "/replays":
                self._send_json(SERVICE.replay(sample=data))
                return

            match = re.fullmatch(r"/applications/([^/]+)/(withdraw|events|settle|recheck|"
                                 r"replay|disputes|disputes/close|replacements)", path)
            if match:
                application_id, action = match.groups()
                self._application_action(application_id, action, data)
                return

            if path == "/admin/policies":
                policy = SERVICE.registry.add_policy(data)
                self._send_json({"policy": policy.to_dict()}, status=201)
                return
            if path == "/admin/catalogs":
                catalog = SERVICE.registry.add_catalog(data)
                self._send_json({"catalog": catalog.to_dict()}, status=201)
                return
            if path == "/admin/applicants":
                applicant = SERVICE.registry.upsert_applicant(data)
                self._send_json({"applicant": applicant.to_dict()}, status=201)
                return
            if path == "/admin/budgets":
                amount = data.get("amount_cents", data.get("amount"))
                cents = SERVICE.registry.set_budget(data["region"], int(data["year"]), amount)
                self._send_json({"region": data["region"], "year": int(data["year"]),
                                 "budget_cents": cents}, status=201)
                return
            self.send_error(404)
        except DomainError as error:
            self._handle_domain_error(error)
    def _application_action(self, application_id: str, action: str, data: dict):
        if action == "withdraw":
            self._send_json(SERVICE.withdraw(application_id, data.get("reason", "")))
        elif action == "events":
            self._send_json(SERVICE.record_event(application_id, data), status=201)
        elif action == "settle":
            self._send_json(SERVICE.settle(application_id, data.get("as_of")))
        elif action == "recheck":
            self._send_json(SERVICE.recheck(
                application_id, bool(data.get("passed", False)),
                data.get("reason", ""), data.get("on")))
        elif action == "replay":
            self._send_json(SERVICE.replay(
                application_id, policy_version=data.get("policy_version"),
                catalog_version=data.get("catalog_version")))
        elif action == "disputes":
            self._send_json(SERVICE.open_dispute(
                application_id, data.get("parties", []), data.get("description", "")),
                status=201)
        elif action == "disputes/close":
            self._send_json(SERVICE.close_dispute(
                application_id, data["responsible_party"],
                data.get("resolution_note", ""), data.get("as_of")))
        elif action == "replacements":
            self._send_json(SERVICE.request_replacement(
                application_id, data["replacement_device_id"],
                data.get("reason_fault_event_id")), status=201)

    @staticmethod
    def _quota(query: dict) -> dict:
        year_value = query.get("year", [None])[0]
        year = int(year_value) if year_value else None
        return SERVICE.quota_status(
            applicant_id=query.get("applicant_id", [None])[0],
            region=query.get("region", [None])[0],
            year=year,
        )

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        assert SERVICE.verify_ledger()["ok"]
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
