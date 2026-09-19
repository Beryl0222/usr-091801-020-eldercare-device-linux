"""应用服务：政策注册、申请生命周期、履约事件、争议冻结与政策复算。

决定不可变原则：
    提交时的 ``decision``（含政策/目录/证据冻结快照与逐项试算）一经生成不再修改；
    撤回、提前归还、复核失败、争议责任分摊全部以追加 ``corrections`` 与台账冲正
    分录表达。

数据最小化原则：
    履约事件按类型限定字段白名单，任何健康/生命体征字段在入口即整份拒绝、不落库；
    补贴审核视图只包含资格结论与履约事件，医疗服务方数据不在本服务内。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date

from .errors import (
    InvalidTransition,
    NotFound,
    PayloadRejected,
    SettlementFrozen,
    VersionConflict,
)
from .ledger import QuotaLedger
from .money import (
    INSTITUTION,
    PROGRAM_LABELS,
    PURCHASE,
    RENTAL,
    age_on,
    daily_from_monthly,
    parse_date,
    ratio_amount,
    split_by_year,
    utc_now,
)
from .policy import (
    DeviceCatalog,
    EligibilityEvidence,
    PolicyVersion,
    REASON_POLICY_NOT_IN_FORCE,
    quote,
)

# 申请状态
REJECTED = "REJECTED"
SUBMITTED = "SUBMITTED"
DELIVERED = "DELIVERED"
WITHDRAWN = "WITHDRAWN"
SETTLED = "SETTLED"
ACTIVE_STATES = (SUBMITTED, DELIVERED)

# 履约事件白名单：类型 -> 允许字段
ACTIVATION = "activation"
MAINTENANCE = "maintenance"
FAULT = "fault"
REPLACEMENT = "replacement"
RETURN = "return"
EVENT_ALLOWED_FIELDS = {
    ACTIVATION: {"at", "activated_by"},
    MAINTENANCE: {"at", "technician_id", "summary", "result"},
    FAULT: {"at", "fault_code", "description", "reported_by"},
    REPLACEMENT: {"at", "replacement_sku", "reason"},
    RETURN: {"at", "condition", "received_by"},
}
EVENT_SOURCES = {"vendor", "community", "institution", "family"}
# 健康监测相关字段一律拒绝（双重保险：未知字段本身也会被拒）
FORBIDDEN_FIELD_HINTS = (
    "health", "heart", "blood", "vital", "spo2", "sleep", "pulse",
    "temperature", "medical", "diagnos", "血压", "心率", "脉搏", "体温",
    "健康", "血氧", "睡眠", "病历", "诊断",
)

# 争议责任方
VENDOR = "vendor"
COMMUNITY = "community"
FAMILY = "family"
PARTY_LABELS = {VENDOR: "厂商", COMMUNITY: "社区", FAMILY: "家庭"}


@dataclass
class ApplicationRecord:
    application_id: str
    household_id: str
    region_code: str
    sku: str
    created_at: str
    state: str = SUBMITTED
    decision: dict = None
    policy_version: str = None
    catalog_version: str = None
    term_start: date = None
    term_end: date = None
    # 各年度仍挂账的预留（分次释放时扣减）；已结算金额逐年记录
    reservations: dict = field(default_factory=dict)
    settled: dict = field(default_factory=dict)
    corrections: list = field(default_factory=list)
    events: list = field(default_factory=list)
    dispute: dict = None
    pending_return: date = None  # 争议期间已归还但结算冻结，等待责任认定
    downtimes: list = field(default_factory=list)  # 已认定停用区间 [(起,止,责任方)]

    def to_dict(self) -> dict:
        return {
            "application_id": self.application_id,
            "household_id": self.household_id,
            "region_code": self.region_code,
            "sku": self.sku,
            "state": self.state,
            "created_at": self.created_at,
            "policy_version": self.policy_version,
            "catalog_version": self.catalog_version,
            "term_start": self.term_start.isoformat() if self.term_start else None,
            "term_end": self.term_end.isoformat() if self.term_end else None,
            "reservations": dict(self.reservations),
            "settled": dict(self.settled),
            "decision": self.decision,
            "corrections": list(self.corrections),
            "events": list(self.events),
            "dispute": self.dispute,
            "pending_return": self.pending_return.isoformat()
            if self.pending_return
            else None,
            "downtimes": [
                {"start": s.isoformat(), "end": e.isoformat(),
                 "responsible_party": p}
                for s, e, p in self.downtimes
            ],
        }


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _reject_health_fields(event_type, payload):
    allowed = EVENT_ALLOWED_FIELDS[event_type]
    extra = set(payload) - allowed
    bad = [
        key
        for key in payload
        if any(hint in key.lower() for hint in FORBIDDEN_FIELD_HINTS)
    ]
    if bad:
        raise PayloadRejected(
            "健康监测数据由医疗服务方持有，补贴业务不得采集",
            {"rejected_fields": sorted(bad)},
        )
    if extra:
        raise PayloadRejected(
            f"履约事件 {event_type} 含白名单外字段，整份拒绝",
            {"allowed_fields": sorted(allowed), "unknown_fields": sorted(extra)},
        )


class BenefitService:
    """领域服务门面。内存态存储 + 一把服务锁；台账内部另有锁。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.policies = {}   # (region, version) -> PolicyVersion
        self.catalogs = {}   # (region, version) -> DeviceCatalog
        self.applications = {}
        self.household_apps = {}  # household_id -> [application_id]
        self.samples = {}    # family sample 样例：sample_id -> evidence dict
        self.ledger = QuotaLedger()

    # ------------------------------------------------------------------ #
    # 政策、目录与家庭样例
    # ------------------------------------------------------------------ #
    def register_policy(self, data: dict) -> dict:
        policy = PolicyVersion.from_dict(data)
        with self._lock:
            key = (policy.region_code, policy.version)
            if key in self.policies:
                raise VersionConflict(
                    f"政策版本 {policy.region_code}/{policy.version} 已存在且不可覆盖",
                    {"region_code": policy.region_code, "version": policy.version},
                )
            self.policies[key] = policy
            return policy.to_dict()

    def register_catalog(self, data: dict) -> dict:
        catalog = DeviceCatalog.from_dict(data)
        with self._lock:
            key = (catalog.region_code, catalog.version)
            if key in self.catalogs:
                raise VersionConflict(
                    f"设备目录 {catalog.region_code}/{catalog.version} 已存在且不可覆盖",
                    {"region_code": catalog.region_code, "version": catalog.version},
                )
            self.catalogs[key] = catalog
            return catalog.to_dict()

    def register_sample(self, sample_id, evidence_data: dict) -> dict:
        evidence = EligibilityEvidence.from_dict(evidence_data)
        with self._lock:
            if sample_id in self.samples:
                raise VersionConflict(f"家庭样例 {sample_id} 已存在", {"sample_id": sample_id})
            self.samples[sample_id] = evidence
            return {"sample_id": sample_id, "evidence": evidence.to_dict()}

    def _policy_for(self, region_code, day: date) -> PolicyVersion:
        candidates = [
            p
            for (region, _v), p in self.policies.items()
            if region == region_code and p.in_force_on(day)
        ]
        if not candidates:
            raise NotFound(
                f"地区 {region_code} 在 {day.isoformat()} 无生效政策版本",
                {"region_code": region_code, "date": day.isoformat()},
            )
        # 生效日最晚、其次版本号最大；版本号相等时先注册者优先（确定性）
        return sorted(
            candidates,
            key=lambda p: (p.valid_from, p.version),
            reverse=True,
        )[0]

    def _catalog_for(self, region_code, version=None) -> DeviceCatalog:
        if version:
            key = (region_code, version)
            if key not in self.catalogs:
                raise NotFound(f"设备目录 {region_code}/{version} 不存在")
            return self.catalogs[key]
        versions = [
            c for (region, _v), c in self.catalogs.items() if region == region_code
        ]
        if not versions:
            raise NotFound(f"地区 {region_code} 无设备目录")
        return sorted(versions, key=lambda c: c.version, reverse=True)[0]

    # ------------------------------------------------------------------ #
    # 申请提交：先冻结，再试算，再原子占用年度额度
    # ------------------------------------------------------------------ #
    def submit_application(self, data: dict) -> dict:
        application_date = parse_date(data["application_date"])
        region_code = str(data["region_code"])
        with self._lock:
            evidence = EligibilityEvidence.from_dict(data["evidence"])
            evidence_view = evidence.to_dict()
            evidence_view["age_at_application"] = age_on(
                evidence.birth_date, application_date
            )
            try:
                policy = self._policy_for(region_code, application_date)
            except NotFound:
                policy = None
            try:
                catalog = self._catalog_for(region_code, data.get("catalog_version"))
            except NotFound:
                catalog = None

            application = {
                "application_date": data["application_date"],
                "sku": str(data["sku"]),
                "programs": data.get("programs"),
                "term_start": data.get("term_start"),
                "term_end": data.get("term_end"),
            }

            if policy is None:
                # 申请日无生效政策：同样落一笔拒绝决定并逐项说明，避免事后才告知
                decision = {
                    "application_date": data["application_date"],
                    "sku": application["sku"],
                    "term_start": application["term_start"],
                    "term_end": application["term_end"],
                    "frozen": {
                        "policy": None,
                        "device": catalog.get(application["sku"]).to_dict()
                        if catalog and catalog.get(application["sku"])
                        else {"sku": application["sku"], "missing": True},
                        "evidence": evidence_view,
                    },
                    "programs": [],
                    "selected": None,
                    "rejected": True,
                    "rejection_reasons": [REASON_POLICY_NOT_IN_FORCE],
                }
            else:
                duplicate_skus = self._active_duplicate_skus(
                    evidence.household_id, exclude_app=None
                )
                decision = quote(application, evidence, policy, catalog,
                                 duplicate_skus)

            record = ApplicationRecord(
                application_id=str(data.get("application_id") or _new_id("app")),
                household_id=evidence.household_id,
                region_code=region_code,
                sku=application["sku"],
                created_at=utc_now().isoformat(),
                decision=decision,
                policy_version=policy.version if policy else None,
                catalog_version=catalog.version if catalog else None,
                term_start=parse_date(data["term_start"]) if data.get("term_start") else None,
                term_end=parse_date(data["term_end"]) if data.get("term_end") else None,
            )

            if record.application_id in self.applications:
                raise VersionConflict(
                    "申请号重复", {"application_id": record.application_id}
                )

            if decision["rejected"]:
                record.state = REJECTED
            else:
                selected = decision["selected"]
                rule = policy.rules[selected["program"]]
                reservations = [
                    (
                        evidence.household_id,
                        seg["year"],
                        seg["subsidy_cents"],
                        record.application_id,
                        f"提交申请占用：{PROGRAM_LABELS[selected['program']]}",
                    )
                    for seg in selected["yearly"]
                    if seg["subsidy_cents"] > 0
                ]
                caps = {
                    (evidence.household_id, seg["year"]): rule.annual_cap_cents
                    for seg in selected["yearly"]
                }
                ledger_entries = self.ledger.occupy_batch(reservations, caps)
                for seg in selected["yearly"]:
                    record.reservations[seg["year"]] = seg["subsidy_cents"]
                record.corrections.append(
                    self._correction(
                        "SUBMIT", "申请提交，按年度冻结额度", application_date,
                        {"selected_program": selected["program"],
                         "ledger_entries": ledger_entries},
                    )
                )

            self.applications[record.application_id] = record
            self.household_apps.setdefault(record.household_id, []).append(
                record.application_id
            )
            return self._view(record)

    def _active_duplicate_skus(self, household_id, exclude_app):
        """在途申请（已提交/已交付）占用的设备型号，不得就同型号重复享受。

        已归还或已结算完毕的设备不在此列：退回的设备可再次投放，
        家庭日后购置新型号亦不受限（防同机重复骗取以实物序列号核验为准）。
        """
        skus = set()
        for app_id in self.household_apps.get(household_id, []):
            if app_id == exclude_app:
                continue
            rec = self.applications[app_id]
            if rec.state in (SUBMITTED, DELIVERED):
                skus.add(rec.sku)
        return skus

    @staticmethod
    def _correction(kind, reason, effective_date, detail):
        return {
            "correction_id": _new_id("corr"),
            "kind": kind,
            "reason": reason,
            "effective_date": effective_date.isoformat()
            if isinstance(effective_date, date)
            else str(effective_date),
            "detail": detail,
            "created_at": utc_now().isoformat(),
        }

    def _get(self, application_id) -> ApplicationRecord:
        record = self.applications.get(application_id)
        if record is None:
            raise NotFound(f"申请 {application_id} 不存在",
                           {"application_id": application_id})
        return record

    def _frozen_rule(self, record: ApplicationRecord, program: str):
        policy = self.policies[(record.region_code, record.policy_version)]
        return policy.rules[program], policy

    def _frozen_device(self, record: ApplicationRecord):
        catalog = self.catalogs[(record.region_code, record.catalog_version)]
        return catalog.get(record.sku)

    # ------------------------------------------------------------------ #
    # 撤回（付款/交付前）：全额释放预留，原决定保留
    # ------------------------------------------------------------------ #
    def withdraw(self, application_id, reason="申请人撤回"):
        with self._lock:
            record = self._get(application_id)
            if record.state != SUBMITTED:
                raise InvalidTransition(
                    "只有已提交未交付的申请可以撤回",
                    {"application_id": application_id, "state": record.state},
                )
            entries = []
            for year, amount in sorted(record.reservations.items()):
                if amount > 0:
                    entries.append(
                        self.ledger.release_reservation(
                            application_id, record.household_id, year, amount, reason
                        )
                    )
            released_years = [y for y, amount in record.reservations.items() if amount > 0]
            record.reservations = {y: 0 for y in record.reservations}
            record.state = WITHDRAWN
            record.corrections.append(
                self._correction(
                    "WITHDRAW", reason, parse_date(record.decision["application_date"]),
                    {"released_years": released_years,
                     "ledger_entries": entries},
                )
            )
            return self._view(record)

    # ------------------------------------------------------------------ #
    # 履约：交付启用、维护、故障、替代、归还（白名单 + 健康边界）
    # ------------------------------------------------------------------ #
    def deliver(self, application_id, activation_payload: dict):
        with self._lock:
            record = self._get(application_id)
            if record.state != SUBMITTED:
                raise InvalidTransition(
                    "只有已提交申请可以登记交付", {"state": record.state}
                )
            payload = dict(activation_payload or {})
            payload.setdefault("at", utc_now().date().isoformat())
            self._append_event(record, ACTIVATION, "vendor", payload)
            record.state = DELIVERED

            program = record.decision["selected"]["program"]
            entries = []
            if program == PURCHASE:
                # 购置：交付即结算，预留转结算
                rule, _ = self._frozen_rule(record, program)
                for year, amount in sorted(record.reservations.items()):
                    entries.extend(
                        self.ledger.settle_segment(
                            application_id, record.household_id, year,
                            amount, amount, rule.annual_cap_cents, "设备交付，购置补贴结算",
                        )
                    )
                    record.settled[year] = amount
                    record.reservations[year] = 0
            record.corrections.append(
                self._correction(
                    "DELIVER", "设备交付启用", parse_date(payload["at"]),
                    {"ledger_entries": entries},
                )
            )
            return self._view(record)

    def record_event(self, application_id, event_type, source, payload):
        with self._lock:
            record = self._get(application_id)
            return self._append_event(record, event_type, source, payload)

    def _append_event(self, record, event_type, source, payload):
        if record.state not in (SUBMITTED, DELIVERED):
            raise InvalidTransition(
                "当前状态不接受履约事件",
                {"application_id": record.application_id, "state": record.state},
            )
        if event_type not in EVENT_ALLOWED_FIELDS:
            raise PayloadRejected(
                "不支持的履约事件类型；补贴业务仅采集启用/维护/故障/替代/归还",
                {"event_type": event_type},
            )
        if source not in EVENT_SOURCES:
            raise PayloadRejected(
                "事件来源必须是厂商/社区/机构/家庭；医疗服务方数据不进入本系统",
                {"source": source},
            )
        if not isinstance(payload, dict):
            raise PayloadRejected("事件载荷必须是对象")
        _reject_health_fields(event_type, payload)
        if "at" not in payload:
            raise PayloadRejected("履约事件必须包含事件日期 at")
        parse_date(payload["at"])  # 校验
        event = {
            "event_id": _new_id("evt"),
            "type": event_type,
            "source": source,
            "payload": dict(payload),
            "recorded_at": utc_now().isoformat(),
        }
        record.events.append(event)
        return event

    # ------------------------------------------------------------------ #
    # 归还：正常届满或提前归还，按实际租期逐年分段结算，余额释放
    # ------------------------------------------------------------------ #
    def return_device(self, application_id, return_date, reason="设备归还",
                      condition="正常"):
        with self._lock:
            record = self._get(application_id)
            if record.state != DELIVERED:
                raise InvalidTransition("只有已交付申请可以登记归还",
                                        {"state": record.state})
            self._ensure_not_frozen(record)
            return_date = parse_date(return_date)
            program = record.decision["selected"]["program"]
            payload = {"at": return_date.isoformat(), "condition": condition,
                       "received_by": "community"}
            if program != PURCHASE:
                if not record.term_start <= return_date <= record.term_end:
                    raise InvalidTransition(
                        "归还日期必须在租期内",
                        {"term_start": record.term_start.isoformat(),
                         "term_end": record.term_end.isoformat(),
                         "return_date": return_date.isoformat()},
                    )
                kind = "SETTLE_RETURN"
                if return_date < record.term_end:
                    kind = "EARLY_RETURN"
                detail = self._settle_rental(record, record.term_start, return_date,
                                             downtime=list(record.downtimes),
                                             reason=reason)
            else:
                kind = "PURCHASE_RETURN"
                detail = {"note": "购置设备归还不改变已完成结算；争议另案处理"}
            self._append_event(record, RETURN, "community", payload)
            record.state = SETTLED
            record.corrections.append(
                self._correction(kind, reason, return_date, detail)
            )
            return self._view(record)

    def settle_maturity(self, application_id):
        """租期正常届满：按原决定金额把预留全额转结算。"""
        with self._lock:
            record = self._get(application_id)
            if record.state != DELIVERED:
                raise InvalidTransition("只有已交付申请可以届满结算",
                                        {"state": record.state})
            self._ensure_not_frozen(record)
            program = record.decision["selected"]["program"]
            if program == PURCHASE:
                raise InvalidTransition("购置申请交付时已结算")
            detail = self._settle_rental(record, record.term_start, record.term_end,
                                         downtime=list(record.downtimes),
                                         reason="租期届满分段结算")
            record.state = SETTLED
            record.corrections.append(
                self._correction("SETTLE_TERM", "租期正常届满", record.term_end, detail)
            )
            return self._view(record)

    def _settle_rental(self, record, window_start, window_end, downtime, reason):
        """对实际窗口 [start, end) 逐年结算；downtime=[(起,止,责任方)]。

        厂商/社区责任的停用日不计费（家庭与财政均不承担）；
        家庭责任的停用日按全额自付（不享受补贴）。
        剩余年度预留全部释放，台账中每年 RELEASE+SETTLE 原子成对。
        """
        program = record.decision["selected"]["program"]
        rule, _policy = self._frozen_rule(record, program)
        device = self._frozen_device(record)
        monthly = (
            device.monthly_rent_cents
            if program == RENTAL
            else device.monthly_service_cents
        )
        daily = daily_from_monthly(monthly)

        day_classes = self._classify_days(window_start, window_end, downtime)
        yearly_detail = []
        ledger_entries = []
        for year, normal, family_fault, vendor_fault in day_classes:
            normal_base = daily * normal
            family_fault_base = daily * family_fault
            raw = ratio_amount(normal_base, rule.rate_permille)
            subsidy = min(raw, rule.per_item_cap_cents)
            base = normal_base + family_fault_base  # 厂商责任停用日不进基数
            family_pay = base - subsidy
            reserved_now = record.reservations.get(year, 0)
            ledger_entries.extend(
                self.ledger.settle_segment(
                    record.application_id, record.household_id, year,
                    reserved_now, subsidy, rule.annual_cap_cents,
                    f"{reason}（{year}年段）",
                )
            )
            record.settled[year] = record.settled.get(year, 0) + subsidy
            record.reservations[year] = 0
            yearly_detail.append({
                "year": year,
                "normal_days": normal,
                "family_responsible_downtime_days": family_fault,
                "vendor_responsible_downtime_days": vendor_fault,
                "daily_rate_cents": daily,
                "base_cents": base,
                "subsidy_cents": subsidy,
                "family_cents": family_pay,
                "released_reservation_cents": reserved_now,
            })
        return {"segments": yearly_detail, "ledger_entries": ledger_entries}

    @staticmethod
    def _classify_days(window_start, window_end, downtime):
        """逐年统计正常日/家庭责任停用日/厂商社区责任停用日。"""
        down = []
        for s, e, party in downtime:
            s, e = parse_date(s), parse_date(e)
            down.append((max(s, window_start), min(e, window_end), party))
        result = []
        for year, seg_start, seg_end, _days in split_by_year(window_start, window_end):
            normal = family_f = vendor_f = 0
            day = seg_start
            while day < seg_end:
                party = None
                for s, e, p in down:
                    if s <= day < e:
                        party = p
                        break
                if party == FAMILY:
                    family_f += 1
                elif party in (VENDOR, COMMUNITY):
                    vendor_f += 1
                else:
                    normal += 1
                day = date.fromordinal(day.toordinal() + 1)
            result.append((year, normal, family_f, vendor_f))
        return result

    # ------------------------------------------------------------------ #
    # 资格复核失败：按失败日分段结算，后续预留释放；可声明原资格自始无效
    # ------------------------------------------------------------------ #
    def review_failed(self, application_id, failure_date, retroactive=False,
                      reason="资格复核未通过"):
        with self._lock:
            record = self._get(application_id)
            if record.state not in (SUBMITTED, DELIVERED, SETTLED):
                raise InvalidTransition("当前状态不能进行资格复核处理",
                                        {"state": record.state})
            if record.state == SETTLED and not retroactive:
                raise InvalidTransition(
                    "已结算申请复核失败必须声明追溯冲正（retroactive=true）",
                    {"state": record.state},
                )
            self._ensure_not_frozen(record)
            failure_date = parse_date(failure_date)
            program = record.decision["selected"]["program"]
            ledger_entries = []
            detail = {}

            if retroactive:
                # 自始无效：全部已结算补贴冲正追回，全部预留释放
                rule, _ = self._frozen_rule(record, program)
                for year, amount in sorted(record.settled.items()):
                    if amount > 0:
                        ledger_entries.append(
                            self.ledger.reverse(
                                application_id, record.household_id, year,
                                amount, f"复核自始无效，追回补贴：{reason}",
                            )
                        )
                for year, amount in sorted(record.reservations.items()):
                    if amount > 0:
                        ledger_entries.append(
                            self.ledger.release_reservation(
                                application_id, record.household_id, year, amount,
                                "复核失败，释放未用预留",
                            )
                        )
                record.settled = {y: 0 for y in record.settled}
                record.reservations = {y: 0 for y in record.reservations}
                detail["mode"] = "retroactive_reversal"
            else:
                # 自失败日起终止：已交付期间按分段结算，未来预留释放
                if record.state == DELIVERED and program != PURCHASE:
                    end = min(failure_date, record.term_end)
                    if end > record.term_start:
                        detail = self._settle_rental(
                            record, record.term_start, end,
                            downtime=list(record.downtimes),
                            reason=f"复核失败，结算至失败日前",
                        )
                    else:
                        detail = {"segments": []}
                elif record.state == DELIVERED and program == PURCHASE:
                    # 购置已结算，是否追回由 retroactive 决定；非追溯则保留
                    detail = {"mode": "purchase_settlement_retained"}
                for year, amount in sorted(record.reservations.items()):
                    if amount > 0:
                        ledger_entries.append(
                            self.ledger.release_reservation(
                                application_id, record.household_id, year, amount,
                                "复核失败，释放失败日之后预留",
                            )
                        )
                    record.reservations[year] = 0
                detail.setdefault("mode", "prospective_stop")
            detail["ledger_entries"] = detail.get("ledger_entries", []) + ledger_entries
            record.state = REJECTED  # 复核失败后资格失效；原决定仍完整保留
            record.corrections.append(
                self._correction("REVIEW_FAIL", reason, failure_date, detail)
            )
            return self._view(record)

    # ------------------------------------------------------------------ #
    # 故障责任争议：冻结结算、不中断替代设备
    # ------------------------------------------------------------------ #
    def open_dispute(self, application_id, fault_date, description,
                     replacement_payload=None):
        with self._lock:
            record = self._get(application_id)
            if record.state != DELIVERED:
                raise InvalidTransition(
                    "故障责任争议只能在设备交付后发起", {"state": record.state}
                )
            if record.dispute and record.dispute["status"] == "open":
                raise InvalidTransition("该申请已有未决争议")
            self._append_event(
                record, FAULT, "family",
                {"at": parse_date(fault_date).isoformat(),
                 "fault_code": "DISPUTE", "description": description,
                 "reported_by": "family"},
            )
            replacement_event = None
            if replacement_payload:
                replacement_event = self._append_event(
                    record, REPLACEMENT, "community", replacement_payload
                )
            record.dispute = {
                "status": "open",
                "fault_date": parse_date(fault_date).isoformat(),
                "description": description,
                "opened_at": utc_now().isoformat(),
                "resolution": None,
            }
            record.corrections.append(
                self._correction(
                    "DISPUTE_OPEN", "故障责任争议，结算冻结", parse_date(fault_date),
                    {"replacement_event": replacement_event},
                )
            )
            return self._view(record)

    def resolve_dispute(self, application_id, responsible_party, resolved_date,
                        downtime_start=None, downtime_end=None, reason="争议处理完成"):
        with self._lock:
            record = self._get(application_id)
            if not record.dispute or record.dispute["status"] != "open":
                raise InvalidTransition("该申请没有待决争议")
            if responsible_party not in PARTY_LABELS:
                raise PayloadRejected("责任方必须是 vendor/community/family")
            resolved_date = parse_date(resolved_date)
            downtime = []
            if downtime_start and downtime_end:
                downtime = [
                    (parse_date(downtime_start), parse_date(downtime_end),
                     responsible_party)
                ]
                record.downtimes.extend(downtime)
            record.dispute["status"] = "resolved"
            record.dispute["resolution"] = {
                "responsible_party": responsible_party,
                "responsible_party_label": PARTY_LABELS[responsible_party],
                "resolved_date": resolved_date.isoformat(),
                "downtime": [
                    {"start": str(downtime_start), "end": str(downtime_end)}
                ] if downtime else [],
                "reason": reason,
            }
            settlement = None
            # 争议期间仅冻结，不结算；解除后若设备已归还则按责任分段结算
            if record.state == DELIVERED and record.pending_return:
                settlement = self._settle_rental(
                    record, record.term_start, record.pending_return,
                    downtime=downtime, reason="争议解除，按责任分段结算",
                )
                record.state = SETTLED
                record.pending_return = None
            record.corrections.append(
                self._correction(
                    "DISPUTE_RESOLVE", reason, resolved_date,
                    {"responsible_party": responsible_party,
                     "settlement": settlement},
                )
            )
            return self._view(record)

    def return_device_under_dispute(self, application_id, return_date, **kwargs):
        """争议未决时归还：登记归还事件，结算保持冻结，等待责任认定。"""
        with self._lock:
            record = self._get(application_id)
            if not record.dispute or record.dispute["status"] != "open":
                return self.return_device(application_id, return_date, **kwargs)
            return_date = parse_date(return_date)
            self._append_event(
                record, RETURN, "community",
                {"at": return_date.isoformat(),
                 "condition": kwargs.get("condition", "争议中归还"),
                 "received_by": "community"},
            )
            record.pending_return = return_date
            record.corrections.append(
                self._correction(
                    "RETURN_PENDING_DISPUTE",
                    "争议期间归还设备，结算冻结至责任认定", return_date, {},
                )
            )
            return self._view(record)

    @staticmethod
    def _ensure_not_frozen(record):
        if record.dispute and record.dispute["status"] == "open":
            raise SettlementFrozen(
                "故障责任争议未决，结算冻结；替代设备服务不受影响",
                {"application_id": record.application_id,
                 "fault_date": record.dispute["fault_date"]},
            )

    # ------------------------------------------------------------------ #
    # 政策复算：用新政策 + 目录 + 家庭样例重算旧申请，逐项对比
    # ------------------------------------------------------------------ #
    def recalculate(self, application_id, target_policy_version=None,
                    target_catalog_version=None, sample_id=None,
                    concurrent_applications=()):
        """对旧申请按新政策/新目录/家庭样例复算。

        - 原决定不修改，只输出对比报告；
        - 逐项给出家庭自付、财政责任、合格性与拒绝原因；
        - ``concurrent_applications`` 为同时提交的申请载荷列表，
          报告会模拟全部占用，证明各家庭年度额度不会被超占。
        """
        with self._lock:
            record = self._get(application_id)
            old = record.decision
            app_date = parse_date(old["application_date"])
            if target_policy_version:
                policy = self.policies[(record.region_code, target_policy_version)]
            else:
                policy = self._policy_for(record.region_code, app_date)
            catalog = self._catalog_for(record.region_code, target_catalog_version)

            if sample_id:
                evidence = self.samples[sample_id]
            else:
                ev = old["frozen"]["evidence"]
                evidence = EligibilityEvidence.from_dict(
                    {**ev, "document_ids": ev.get("document_ids", [])}
                )

            application = {
                "application_date": old["application_date"],
                "sku": record.sku,
                "term_start": old["term_start"],
                "term_end": old["term_end"],
            }
            new = quote(application, evidence, policy, catalog,
                        self._active_duplicate_skus(record.household_id,
                                                    exclude_app=record.application_id),
                        ignore_effective_date=True)

            comparison = self._compare_decisions(old, new)

            # 额度模拟：以当前台账（剔除本申请自身挂账）为基线，
            # 加入复算后选择金额与同批并发申请，逐年给出额度证明
            proofs = self._simulate_quota(record, new, policy,
                                          list(concurrent_applications))

            return {
                "application_id": application_id,
                "recalculated_at": utc_now().isoformat(),
                "target_policy_version": policy.version,
                "target_catalog_version": catalog.version,
                "sample_id": sample_id,
                "original_selected_program": (old.get("selected") or {}).get("program"),
                "recalculated": new,
                "line_comparison": comparison,
                "quota_proofs": proofs,
                "original_decision_unchanged": record.decision is old,
            }

    @staticmethod
    def _compare_decisions(old, new):
        old_by = {r["program"]: r for r in old["programs"]}
        rows = []
        for nr in new["programs"]:
            before = old_by.get(nr["program"])
            rows.append({
                "program": nr["program"],
                "program_label": nr["program_label"],
                "before": None if before is None else {
                    "eligible": before["eligible"],
                    "family_cents": before["family_cents"],
                    "fiscal_cents": before["fiscal_cents"],
                    "rejection_reasons": before["rejection_reasons"],
                },
                "after": {
                    "eligible": nr["eligible"],
                    "family_cents": nr["family_cents"],
                    "fiscal_cents": nr["fiscal_cents"],
                    "rejection_reasons": nr["rejection_reasons"],
                    "yearly": nr["yearly"],
                },
                "family_delta_cents": None if before is None else
                    nr["family_cents"] - before["family_cents"],
                "fiscal_delta_cents": None if before is None else
                    nr["fiscal_cents"] - before["fiscal_cents"],
                "newly_rejected": bool(
                    before and before["eligible"] and not nr["eligible"]
                ),
                "newly_approved": bool(
                    before and not before["eligible"] and nr["eligible"]
                ),
            })
        return rows

    def _simulate_quota(self, record, new_decision, new_policy, concurrent):
        """构造模拟占用：当前真实挂账剔除本申请 + 复算选择 + 并发申请。"""
        household_id = record.household_id
        base_usage = {}
        for entry in self.ledger.entries():
            sign = {"OCCUPY": 1, "RELEASE": -1, "SETTLE": 1, "REVERSE": -1}[
                entry["kind"]
            ]
            if entry["application_id"] == record.application_id:
                continue
            key = (entry["household_id"], entry["year"])
            base_usage[key] = base_usage.get(key, 0) + sign * entry["amount_cents"]

        simulated = []
        selected = new_decision["selected"]
        if selected:
            rule = new_policy.rules[selected["program"]]
            for seg in selected["yearly"]:
                simulated.append((household_id, seg["year"], seg["subsidy_cents"],
                                  rule.annual_cap_cents, record.application_id))

        # 并发申请：使用各自载荷里的家庭/证据与新政策环境独立试算
        for idx, payload in enumerate(concurrent):
            decision, policy, ev = self._quote_raw(payload)
            if decision["selected"]:
                rule = policy.rules[decision["selected"]["program"]]
                hh = ev.household_id
                for seg in decision["selected"]["yearly"]:
                    simulated.append((hh, seg["year"], seg["subsidy_cents"],
                                      rule.annual_cap_cents,
                                      payload.get("application_id", f"concurrent_{idx}")))

        totals = {}
        caps = {}
        labels = {}
        for hh, year, amount, cap, app_id in simulated:
            totals[(hh, year)] = totals.get((hh, year), 0) + amount
            caps[(hh, year)] = cap
            labels.setdefault((hh, year), []).append(app_id)

        proofs = []
        households = {(hh, year) for hh, year, *_ in simulated}
        for hh, year in sorted(households):
            base = base_usage.get((hh, year), 0)
            pending = totals[(hh, year)]
            cap = caps[(hh, year)]
            proofs.append({
                "household_id": hh,
                "year": year,
                "annual_cap_cents": cap,
                "baseline_committed_cents": base,
                "simulated_pending_cents": pending,
                "projected_committed_cents": base + pending,
                "available_after_cents": cap - base - pending,
                "over_occupy": base + pending > cap,
                "from_applications": labels[(hh, year)],
            })
        return proofs

    def _quote_raw(self, payload):
        """供并发模拟：按载荷自身地区/日期选择政策目录并试算。"""
        app_date = parse_date(payload["application_date"])
        region = str(payload["region_code"])
        policy = self._policy_for(region, app_date)
        catalog = self._catalog_for(region, payload.get("catalog_version"))
        evidence = EligibilityEvidence.from_dict(payload["evidence"])
        decision = quote(
            {
                "application_date": payload["application_date"],
                "sku": str(payload["sku"]),
                "programs": payload.get("programs"),
                "term_start": payload.get("term_start"),
                "term_end": payload.get("term_end"),
            },
            evidence, policy, catalog,
            self._active_duplicate_skus(evidence.household_id, None),
        )
        return decision, policy, evidence

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #
    def get_application(self, application_id) -> dict:
        with self._lock:
            return self._view(self._get(application_id))

    def reviewer_view(self, application_id) -> dict:
        """补贴审核员视图：资格结论 + 金额 + 履约事件；不含任何健康字段。"""
        with self._lock:
            view = self._view(self._get(application_id))
            for event in view["events"]:
                allowed = EVENT_ALLOWED_FIELDS[event["type"]]
                event["payload"] = {
                    k: v for k, v in event["payload"].items() if k in allowed
                }
            return view

    def quota_proof(self, household_id, year) -> dict:
        """逐年额度证明：按该家庭地区当前生效政策的每条路径上限分别给出。

        占用口径与提交时一致：实际占用按选中路径的年度上限约束，
        因此证明中同时返回最严格（最小）上限的结论，便于审核一眼判断。
        """
        with self._lock:
            region = self._region_of_household(household_id)
            caps = {}
            if region:
                day = date(year, 1, 1)
                try:
                    policy = self._policy_for(region, day)
                    caps = {
                        program: rule.annual_cap_cents
                        for program, rule in policy.rules.items()
                    }
                except NotFound:
                    caps = {}
            reserved, settled = self.ledger.balances(household_id, year)
            committed = reserved + settled
            per_program = []
            for program, cap in caps.items():
                per_program.append({
                    "program": program,
                    "program_label": PROGRAM_LABELS[program],
                    "annual_cap_cents": cap,
                    "available_cents": cap - committed,
                    "over_occupy": committed > cap,
                })
            return {
                "household_id": household_id,
                "year": year,
                "reserved_cents": reserved,
                "settled_cents": settled,
                "committed_cents": committed,
                "per_program_cap": per_program,
                "as_of": utc_now().isoformat(),
            }

    def _region_of_household(self, household_id):
        for app_id in self.household_apps.get(household_id, []):
            return self.applications[app_id].region_code
        return None

    def _view(self, record: ApplicationRecord) -> dict:
        data = record.to_dict()
        data.pop("dispute", None)
        data["settlement_frozen"] = bool(
            record.dispute and record.dispute["status"] == "open"
        )
        data["dispute"] = record.dispute
        if record.decision and record.decision.get("selected"):
            sel = record.decision["selected"]
            data["money_summary"] = {
                "program": sel["program"],
                "program_label": sel["program_label"],
                "family_cents": sel["family_cents"],
                "fiscal_cents": sel["fiscal_cents"],
                "base_cents": sel["base_cents"],
            }
        return data
