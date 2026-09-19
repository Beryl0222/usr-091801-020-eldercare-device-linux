"""适老设备权益领域服务。

所有改变状态的动作都在台账锁内完成并追加分录，因此：

* 并发申请下，个人年度上限与地区年度预算的“检查—占用”是原子的，
  绝不会被超占；
* 撤回、提前归还、复核失败只追加冲正分录（RELEASE/CLAWBACK），
  原决定分录原样保留，哈希链可验；
* 跨年租期按自然日把补贴分到各年度分别占用额度；
* 结算只认启用后的履约事实；争议期冻结结算但不拦替代设备；
* 复算（replay）是纯只读试算，不写台账。
"""

import uuid
from copy import deepcopy
from datetime import date

from .eligibility import add_months, evaluate, money_breakdown
from .errors import (
    REJECT,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .ledger import Ledger
from .models import (
    ACTIVATION,
    DEVICE_EVENT_TYPES,
    DISPUTE_PARTIES,
    EVENT_ALLOWED_FIELDS,
    FAULT,
    HEALTH_FIELD_HINTS,
    INSTITUTION,
    PURCHASE,
    REPLACEMENT,
    RETURN,
    ApplicationSnapshot,
    ContractTerm,
    EvidenceSnapshot,
)
from .money import (
    cents_to_yuan,
    inclusive_days,
    largest_remainder_split,
    parse_day,
    yuan_to_cents,
)

# 申请状态
DENIED = "DENIED"
APPROVED = "APPROVED"          # 额度已预留
ACTIVE = "ACTIVE"              # 设备已启用
SETTLING = "SETTLING"          # 部分结算
SETTLED = "SETTLED"            # 全部结清（含提前归还后结清）
WITHDRAWN = "WITHDRAWN"
REVOKED = "REVOKED"            # 复核失败追回


class ApplicationRecord:
    def __init__(self, application_id: str, snapshot: ApplicationSnapshot,
                 evaluation, decision: dict | None, status: str):
        self.id = application_id
        self.snapshot = snapshot
        self.evaluation = evaluation
        self.decision = decision                 # 入选路径的决定（不可变）
        self.status = status
        self.events: list[dict] = []
        self.settled_segments: list[dict] = []   # 已结算分段
        self.reserves: list[dict] = []           # 年度额度预留
        self.dispute: dict | None = None
        self.pending_return: dict | None = None
        self.replacement_devices: list[dict] = []
        self.created_seq: int | None = None
        self.notes: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "application_id": self.id,
            "status": self.status,
            "snapshot": self.snapshot.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "decision": self.decision,
            "events": list(self.events),
            "settled_segments": list(self.settled_segments),
            "reserves": list(self.reserves),
            "dispute": self.dispute,
            "pending_return": self.pending_return,
            "replacement_devices": list(self.replacement_devices),
        }


class BenefitService:
    def __init__(self, registry):
        self.registry = registry
        self.ledger = Ledger()
        self._applications: dict[str, ApplicationRecord] = {}
        # 台账序列号 -> app_id 的反向索引，冲正引用用
        self._seq_index: dict[int, str] = {}

    # ================= 申请与决定 =================

    def submit(self, request: dict) -> dict:
        """提交单笔申请；返回决定（含逐项金额说明与额度占用凭据）。"""
        results = self.submit_batch([request])
        return results[0]

    def submit_batch(self, requests: list[dict]) -> list[dict]:
        """原子批量提交：任一申请额度不足则整批拒绝，全部不占用。

        批量与单笔共用同一把台账锁，因此“同时提交”的多笔申请与其他
        在途线程一起排队，年度额度不可能被超占。
        """
        if not requests:
            raise ValidationError("申请列表不能为空")
        prepared: list[tuple[dict, ApplicationSnapshot, object, dict | None, str]] = []
        for request in requests:
            prepared.append(self._prepare(request))

        with self.ledger.lock:
            # 第一阶段：锁内复核全部申请的额度（条件检查与占用在同一临界区）。
            # 本批内各笔的预估占用要彼此累加，否则两笔各自“看起来够”、
            # 合起来就会击穿年度上限。
            tentative_personal: dict[tuple[str, int], int] = {}
            tentative_region: dict[tuple[str, int], int] = {}
            plans = []
            for request, snapshot, evaluation, decision, status in prepared:
                if decision is not None:
                    needed: dict[int, int] = {}
                    for part in decision["annual_split"]:
                        needed[part["year"]] = needed.get(part["year"], 0) + part["amount_cents"]
                    applicant_id = snapshot.evidence.applicant.applicant_id
                    region = snapshot.policy.region
                    proof_lines = []
                    for year, amount in sorted(needed.items()):
                        personal_used = self._personal_used(applicant_id, year)
                        budget_used = self._region_used(region, year)
                        cap = snapshot.policy.personal_annual_cap_cents
                        budget = self.registry.get_budget(region, year)
                        personal_free = cap - personal_used - tentative_personal.get(
                            (applicant_id, year), 0)
                        if amount > personal_free:
                            raise ConflictError(
                                REJECT["PERSONAL_CAP_EXHAUSTED"],
                                code="personal_cap_exhausted",
                                details={
                                    "applicant_id": applicant_id, "year": year,
                                    "requested_cents": amount,
                                    "available_cents": max(0, personal_free),
                                    "cap_cents": cap, "used_cents": personal_used,
                                },
                            )
                        if budget is not None:
                            budget_free = (
                                budget - budget_used
                                - tentative_region.get((region, year), 0))
                            if amount > budget_free:
                                raise ConflictError(
                                    REJECT["REGION_BUDGET_EXHAUSTED"],
                                    code="region_budget_exhausted",
                                    details={
                                        "region": region, "year": year,
                                        "requested_cents": amount,
                                        "available_cents": max(0, budget_free),
                                        "budget_cents": budget, "used_cents": budget_used,
                                    },
                                )
                        tentative_personal[(applicant_id, year)] = (
                            tentative_personal.get((applicant_id, year), 0) + amount)
                        if budget is not None:
                            tentative_region[(region, year)] = (
                                tentative_region.get((region, year), 0) + amount)
                        proof_lines.append({
                            "scope": "personal", "year": year,
                            "cap_cents": cap, "used_before_cents": personal_used,
                            "reserve_cents": amount,
                            "free_after_cents": personal_free - amount,
                        })
                        if budget is not None:
                            proof_lines.append({
                                "scope": "region", "year": year,
                                "cap_cents": budget, "used_before_cents": budget_used,
                                "reserve_cents": amount,
                                "free_after_cents": (
                                    budget - budget_used
                                    - tentative_region[(region, year)]),
                            })
                    plans.append((needed, proof_lines))
                else:
                    plans.append((None, []))

            # 第二阶段：全部通过，落决定并逐年度预留额度。
            responses = []
            for (request, snapshot, evaluation, decision, status), plan in zip(prepared, plans):
                needed, proof_lines = plan
                application_id = self._new_id(snapshot.term.apply_on, "APP")
                record = ApplicationRecord(application_id, snapshot, evaluation, decision, status)

                submit_entry = self._append("APPLICATION_SUBMITTED", {
                    "application_id": application_id,
                    "request": self._sanitize_request(request),
                })
                self._seq_index[submit_entry.seq] = application_id
                record.created_seq = submit_entry.seq

                if decision is None:
                    self._append("DECISION", {
                        "application_id": application_id,
                        "result": "DENIED",
                        "evaluation": evaluation.to_dict(),
                        "evidence_captured_on": snapshot.evidence.captured_on.isoformat(),
                        "policy_version": snapshot.policy.version,
                        "catalog_version": snapshot.catalog.version,
                    })
                else:
                    self._append("DECISION", {
                        "application_id": application_id,
                        "result": "APPROVED",
                        "route": decision["route"],
                        "tier_name": decision["tier_name"],
                        "ratio_bp": decision["ratio_bp"],
                        "subsidy_cents": decision["subsidy_cents"],
                        "household_paid_cents": decision["household_paid_cents"],
                        "annual_split": decision["annual_split"],
                        "schedule": decision["schedule"],
                        "policy_version": snapshot.policy.version,
                        "catalog_version": snapshot.catalog.version,
                        "evidence_captured_on": snapshot.evidence.captured_on.isoformat(),
                        "immutable": True,
                    })
                    for year, amount in sorted(needed.items()):
                        reserve_entry = self._append("RESERVE", {
                            "application_id": application_id,
                            "applicant_id": snapshot.evidence.applicant.applicant_id,
                            "region": snapshot.policy.region,
                            "year": year, "amount_cents": amount,
                            "state": "RESERVED",
                        })
                        record.reserves.append({
                            "year": year, "amount_cents": amount,
                            "entry_seq": reserve_entry.seq, "state": "RESERVED",
                        })
                    decision["quota_proof"] = proof_lines
                self._applications[application_id] = record
                responses.append(self._decision_view(record))
            return responses

    def _prepare(self, request: dict):
        """构建冻结快照并评估，不产生任何副作用。"""
        applicant = self.registry.get_applicant(request["applicant_id"])
        apply_on = parse_day(request.get("apply_on") or date.today().isoformat())
        policy_version = request.get("policy_version")
        catalog_version = request.get("catalog_version")
        policy = (
            self._policy_by_version(applicant.region, policy_version)
            if policy_version else self.registry.resolve_policy(applicant.region, apply_on)
        )
        catalog = (
            self._catalog_by_version(applicant.region, catalog_version)
            if catalog_version else self.registry.resolve_catalog(applicant.region, apply_on)
        )
        device = catalog.entry(request["device_id"])
        if device is None:
            raise NotFoundError(
                f"设备不在 {catalog.version} 目录: {request['device_id']}",
                code="device_not_cataloged",
            )

        route = request.get("route")
        if route is not None and route not in (PURCHASE, "RENT", INSTITUTION):
            raise ValidationError(f"未知路径: {route}")

        terms = self._build_terms(route, apply_on, device, request)
        evidence = EvidenceSnapshot(
            applicant=applicant,
            age=applicant.age_on(apply_on),
            captured_on=apply_on,
            assessment_ref=applicant.assessment_ref,
            income_ref=applicant.income_ref,
        )
        snapshot = ApplicationSnapshot(
            evidence=evidence, policy=policy, catalog=catalog,
            device=device, terms=tuple(terms), requested_route=route,
        )
        evaluation = evaluate(snapshot)

        chosen = route or evaluation.winning_route
        decision = None
        status = DENIED
        if chosen is not None:
            chosen_eval = evaluation.evaluations.get(chosen)
            if chosen_eval is None or not chosen_eval.eligible:
                # 显式指定的路径不合格时保持拒绝；原因已在评估里。
                chosen = None
            else:
                breakdown = money_breakdown(snapshot, chosen_eval)
                decision = {
                    "route": chosen,
                    "tier_name": chosen_eval.tier_name,
                    "ratio_bp": chosen_eval.ratio_bp,
                    "reference_cents": breakdown["reference_cents"],
                    "contract_cents": breakdown["contract_cents"],
                    "discount_cents": breakdown["discount_cents"],
                    "net_cents": breakdown["net_cents"],
                    "subsidy_cents": breakdown["subsidy_cents"],
                    "household_paid_cents": breakdown["household_paid_cents"],
                    "annual_split": breakdown["annual_split"],
                    "schedule": chosen_eval.schedule,
                    "months": chosen_eval.months,
                    "rejected_alternatives": [
                        {"route": r, "codes": ev.reject_codes, "reasons": ev.reject_reasons}
                        for r, ev in evaluation.evaluations.items()
                        if r != chosen
                    ],
                }
                status = APPROVED
        return request, snapshot, evaluation, decision, status

    @staticmethod
    def _discount_total(request) -> int:
        discount_total = 0
        for item in request.get("discounts", []):
            amount = item.get("amount_cents")
            if amount is None:
                amount = yuan_to_cents(item.get("amount", 0))
            if int(amount) < 0:
                raise ValidationError("优惠金额不能为负")
            discount_total += int(amount)
        return discount_total

    def _build_terms(self, requested_route, apply_on, device, request) -> list:
        """为三条路径分别冻结计价要素。

        购置始终可计价（设备不支持该路径时由资格侧拒绝）；租赁与机构
        服务只有在请求给出租期时才可计价，显式指定月度路径则必须给租期。
        """
        from datetime import timedelta
        discount_total = self._discount_total(request)
        months = int(request.get("months", 0) or 0)
        lease_start = parse_day(request["lease_start"]) if request.get("lease_start") else apply_on

        terms: list[ContractTerm] = []

        def monthly_term(route, monthly_cents, override_key):
            if months <= 0:
                return None
            reference = monthly_cents * months
            raw = request.get(override_key)
            contract = int(raw) if raw is not None else reference
            if contract < 0:
                raise ValidationError("合同金额不能为负")
            if discount_total > contract:
                raise ValidationError(f"{route} 优惠总额不能超过合同金额")
            lease_end = add_months(lease_start, months) - timedelta(days=1)
            return ContractTerm(
                route=route, apply_on=apply_on, reference_cents=reference,
                contract_cents=contract, discount_cents=discount_total,
                lease_start=lease_start, lease_end=lease_end, months=months,
            )

        purchase_ref = device.price_cents
        purchase_raw = request.get("contract_cents", request.get("contract"))
        purchase_contract = int(purchase_raw) if purchase_raw is not None else purchase_ref
        if purchase_contract < 0:
            raise ValidationError("合同金额不能为负")
        if discount_total > purchase_contract:
            raise ValidationError("优惠总额不能超过合同金额")
        terms.append(ContractTerm(
            route=PURCHASE, apply_on=apply_on, reference_cents=purchase_ref,
            contract_cents=purchase_contract, discount_cents=discount_total,
        ))

        if requested_route in ("RENT", INSTITUTION) and months <= 0:
            raise ValidationError("租赁/机构服务必须给出正整数租期（月）")

        rent_term = monthly_term("RENT", device.monthly_rent_cents,
                                 "rent_contract_cents")
        inst_term = monthly_term(INSTITUTION, device.monthly_service_cents,
                                 "service_contract_cents")
        terms.extend(t for t in (rent_term, inst_term) if t is not None)
        return terms

    # ================= 撤回 =================

    def withdraw(self, application_id: str, reason: str = "") -> dict:
        record = self._get(application_id)
        with self.ledger.lock:
            if record.status not in (APPROVED, ACTIVE, SETTLING):
                raise ConflictError(
                    REJECT["APPLICATION_NOT_ACTIVE"],
                    code="application_not_active",
                    details={"application_id": application_id, "status": record.status},
                )
            if record.settled_segments:
                raise ConflictError(
                    "已有结算分段，撤回请通过提前归还或资格复核流程冲正",
                    code="settlement_exists",
                )
            self._append("WITHDRAWAL", {
                "application_id": application_id, "reason": reason,
                "reverses_decision_seq": record.created_seq,
            })
            self._release_all(record, "WITHDRAWAL")
            record.status = WITHDRAWN
            return self._decision_view(record)

    # ================= 履约事件 =================

    def record_event(self, application_id: str, event: dict) -> dict:
        record = self._get(application_id)
        event_type = event.get("type")
        if event_type not in DEVICE_EVENT_TYPES:
            raise ValidationError(
                f"仅允许 {', '.join(DEVICE_EVENT_TYPES)} 类设备事件",
                details={"received": event_type},
            )
        self._reject_health_fields(event)
        payload = {k: v for k, v in event.items() if k != "type"}
        allowed = EVENT_ALLOWED_FIELDS[event_type]
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError(
                f"{event_type} 事件存在非必要字段: {sorted(unknown)}",
                details={"unknown_fields": sorted(unknown)},
            )
        occurred_on = parse_day(payload.get("occurred_on") or date.today().isoformat())

        with self.ledger.lock:
            if record.status in (DENIED, WITHDRAWN, REVOKED):
                raise ConflictError(
                    REJECT["APPLICATION_NOT_ACTIVE"],
                    code="application_not_active",
                    details={"status": record.status},
                )
            if event_type == ACTIVATION:
                if any(e["type"] == ACTIVATION and e.get("device_id") == payload.get("device_id")
                       for e in record.events):
                    raise ConflictError("该设备已启用，不能重复启用", code="already_activated")
                record.status = ACTIVE
            elif event_type == RETURN:
                if not any(e["type"] == ACTIVATION for e in record.events):
                    raise ConflictError(REJECT["DEVICE_NOT_DELIVERED"], code="device_not_delivered")
            elif event_type in ("MAINTENANCE", FAULT, REPLACEMENT):
                if not any(e["type"] == ACTIVATION for e in record.events):
                    raise ConflictError(REJECT["DEVICE_NOT_DELIVERED"], code="device_not_delivered")

            stored = {
                "event_id": uuid.uuid4().hex,
                "type": event_type,
                "occurred_on": occurred_on.isoformat(),
                **{k: v for k, v in payload.items() if k != "occurred_on"},
            }
            # 争议期间归还先挂账：记录事实，但结算维持冻结。
            if event_type == RETURN and record.dispute:
                record.pending_return = stored
            record.events.append(stored)
            self._append("DEVICE_EVENT", {
                "application_id": application_id, "event": stored,
                "settlement_frozen": bool(record.dispute),
            })
            return stored

    @staticmethod
    def _reject_health_fields(event: dict):
        """健康监测由医疗服务方持有：键名或文本值命中体征特征即拒。"""
        def walk(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if any(hint in str(key).lower() for hint in HEALTH_FIELD_HINTS):
                        raise ValidationError(
                            REJECT["HEALTH_DATA_FORBIDDEN"],
                            code="health_data_forbidden",
                            details={"field": str(key)},
                        )
                    if isinstance(value, str):
                        haystack = value.lower()
                        if any((hint in value) if not hint.isascii() else (hint in haystack)
                               for hint in HEALTH_FIELD_HINTS):
                            raise ValidationError(
                                REJECT["HEALTH_DATA_FORBIDDEN"],
                                code="health_data_forbidden",
                                details={"value": value[:30]},
                            )
                    walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)
        walk(event)

    # ================= 结算与提前归还 =================

    def settle(self, application_id: str, as_of: str | None = None) -> dict:
        record = self._get(application_id)
        with self.ledger.lock:
            if record.dispute:
                raise ConflictError(
                    REJECT["DISPUTE_FROZEN"],
                    code="dispute_frozen",
                    details={"dispute": record.dispute},
                )
            if record.status not in (ACTIVE, SETTLING):
                raise ConflictError(
                    REJECT["APPLICATION_NOT_ACTIVE"],
                    code="application_not_active",
                    details={"status": record.status},
                )
            as_of_day = parse_day(as_of or date.today().isoformat())
            newly_settled, early_closed = self._settle_due(record, as_of_day)
            if not newly_settled and not early_closed:
                return {"application_id": application_id, "new_segments": [],
                        "status": record.status, "message": "暂无到期可结算分段"}
            if record.decision["route"] == PURCHASE or early_closed or self._all_resolved(record):
                record.status = SETTLED
            else:
                record.status = SETTLING
            return {"application_id": application_id,
                    "new_segments": newly_settled, "status": record.status,
                    "early_return_closed": early_closed}

    def _settle_due(self, record: ApplicationRecord, as_of_day: date):
        decision = record.decision
        settled_ids = {s["segment_id"] for s in record.settled_segments}
        newly: list[dict] = []
        early_closed = False

        if decision["route"] == PURCHASE:
            segment_id = "PURCHASE"
            if segment_id not in settled_ids:
                slice_ = decision["schedule"][0]
                newly.append(self._settle_slice(record, slice_, slice_["subsidy_cents"],
                                                slice_["annual_split"], as_of_day,
                                                segment_id, label="购置一次性结算"))
            return newly, False

        return_event = next((e for e in record.events if e["type"] == RETURN), None)
        if return_event is not None:
            cap_day = min(as_of_day, parse_day(return_event["occurred_on"]))
        else:
            cap_day = as_of_day

        for slice_ in decision["schedule"]:
            seg_id = f"MONTH-{slice_['index']:02d}"
            if seg_id in settled_ids:
                continue
            start = parse_day(slice_["start"])
            end = parse_day(slice_["end"])
            if end <= cap_day:
                newly.append(self._settle_slice(
                    record, slice_, slice_["subsidy_cents"],
                    slice_["annual_split"], cap_day, seg_id,
                    label=f"租期分段 {slice_['start']}~{slice_['end']}"))
            elif start <= cap_day < end:
                # 提前归还落在月中：按实际占用自然日分段结算，余数留在释放侧。
                total_days = inclusive_days(start, end)
                used_days = inclusive_days(start, cap_day)
                partial = slice_["subsidy_cents"] * used_days // total_days
                years = self._prorate_slice_years(start, cap_day, slice_, partial)
                newly.append(self._settle_slice(
                    record, slice_, partial, years, cap_day, seg_id,
                    label=f"提前归还分段 {start.isoformat()}~{cap_day.isoformat()}",
                    used_days=used_days, total_days=total_days))
                self._release_unused(record, "EARLY_RETURN")
                early_closed = True
                break
            else:
                break

        if return_event is not None and self._all_resolved(record):
            self._release_unused(record, "EARLY_RETURN")
            early_closed = True
        return newly, early_closed

    def _settle_slice(self, record, slice_, amount_cents, year_split, as_of_day,
                      segment_id, label, used_days=None, total_days=None) -> dict:
        if amount_cents < 0:
            raise ValidationError("结算金额不能为负")
        segment = {
            "segment_id": segment_id,
            "label": label,
            "period_start": slice_["start"],
            "period_end": min(as_of_day.isoformat(), slice_["end"]),
            "amount_cents": amount_cents,
            "annual_split": year_split,
            "used_days": used_days,
            "total_days": total_days,
        }
        entry = self._append("SETTLE", {
            "application_id": record.id, "segment": segment,
        })
        segment["entry_seq"] = entry.seq
        # 把对应年度预留从 RESERVED 转为 SETTLED（额度仍被占用，不重复计数）。
        for part in year_split:
            self._mark_reserve_state(record, part["year"], part["amount_cents"], "SETTLED")
        record.settled_segments.append(segment)
        return segment

    @staticmethod
    def _prorate_slice_years(start, end, slice_, amount_cents):
        """月中截断后，把该分段金额按各自然年实际天数再分摊。"""
        weights = []
        years = []
        cursor = start
        from datetime import timedelta
        while cursor <= end:
            year_end = min(end, date(cursor.year, 12, 31))
            years.append(cursor.year)
            weights.append(inclusive_days(cursor, year_end))
            cursor = year_end + timedelta(days=1)
        shares = largest_remainder_split(amount_cents, weights)
        return [{"year": y, "days": d, "amount_cents": a}
                for y, d, a in zip(years, weights, shares) if a or d]

    def _release_unused(self, record, reason):
        """按已结算净额释放各年度剩余预留（提前归还/复核失败冲正）。"""
        released_before: dict[int, int] = {}
        for entry in self.ledger.all():
            if entry.type == "RELEASE" and entry.payload.get("application_id") == record.id:
                year = entry.payload["year"]
                released_before[year] = released_before.get(year, 0) + entry.payload["amount_cents"]
        for reserve in record.reserves:
            if reserve["state"] == "RELEASED":
                continue
            settled = self._reserve_settled(record, reserve["year"])
            unused = reserve["amount_cents"] - settled - released_before.get(reserve["year"], 0)
            if unused > 0:
                entry = self._append("RELEASE", {
                    "application_id": record.id, "year": reserve["year"],
                    "amount_cents": unused, "reason": reason,
                    "reverses_reserve_seq": reserve["entry_seq"],
                })
                reserve["state"] = "RELEASED"
                reserve["release_seq"] = entry.seq
            elif unused == 0:
                reserve["state"] = "SETTLED"

    def _reserve_settled(self, record, year):
        return sum(
            part["amount_cents"]
            for segment in record.settled_segments for part in segment["annual_split"]
            if part["year"] == year
        )

    def _release_all(self, record, reason):
        for reserve in record.reserves:
            if reserve["state"] == "RELEASED":
                continue
            entry = self._append("RELEASE", {
                "application_id": record.id, "year": reserve["year"],
                "amount_cents": reserve["amount_cents"], "reason": reason,
                "reverses_reserve_seq": reserve["entry_seq"],
            })
            reserve["state"] = "RELEASED"
            reserve["release_seq"] = entry.seq

    @staticmethod
    def _all_resolved(record) -> bool:
        scheduled = sum(s["subsidy_cents"] for s in record.decision["schedule"])
        settled = sum(s["amount_cents"] for s in record.settled_segments)
        return settled >= scheduled

    def _mark_reserve_state(self, record, year, amount_cents, state):
        # 年度预留是聚合的，结算不改变占用总量，仅在预留上累加已结算数。
        for reserve in record.reserves:
            if reserve["year"] == year and reserve["state"] != "RELEASED":
                reserve["settled_cents"] = reserve.get("settled_cents", 0) + amount_cents
                reserve["state"] = state if reserve.get("settled_cents", 0) >= reserve["amount_cents"] else "PARTIAL_SETTLED"
                return

    # ================= 资格复核 =================

    def recheck(self, application_id: str, passed: bool, reason: str = "",
                on: str | None = None) -> dict:
        """用档案当前证据复核；失败则冲正未用预留并追回已结算补贴。

        原 DECISION 与快照不动，失败结论与冲正分录另条追加。
        """
        record = self._get(application_id)
        with self.ledger.lock:
            if record.status in (DENIED, WITHDRAWN, REVOKED):
                raise ConflictError(
                    REJECT["APPLICATION_NOT_ACTIVE"],
                    code="application_not_active",
                    details={"status": record.status},
                )
            on_day = parse_day(on or date.today().isoformat())
            if passed:
                entry = self._append("REVIEW_PASSED", {
                    "application_id": application_id, "on": on_day.isoformat(),
                    "reason": reason,
                })
                return {"application_id": application_id, "result": "PASSED",
                        "entry_seq": entry.seq}

            current = self.registry.get_applicant(record.snapshot.evidence.applicant.applicant_id)
            age = current.age_on(on_day)
            failure_codes = []
            policy = record.snapshot.policy
            rule = policy.rule(record.decision["route"])
            if current.region != policy.region:
                failure_codes.append("REGION")
            if age < rule.min_age:
                failure_codes.append("AGE")
            if current.care_level < rule.min_care_level:
                failure_codes.append("CARE_LEVEL")
            if rule.low_income_only and not current.low_income:
                failure_codes.append("LOW_INCOME")
            hukou_ok = current.hukou_region == policy.region
            residency_ok = current.has_local_residency
            if rule.require_local_hukou and rule.require_residency:
                if not (hukou_ok or residency_ok):
                    failure_codes.append("HUKOU")
            elif rule.require_local_hukou and not hukou_ok:
                failure_codes.append("HUKOU")
            elif rule.require_residency and not residency_ok:
                failure_codes.append("RESIDENCY")

            self._append("REVIEW_FAILED", {
                "application_id": application_id, "on": on_day.isoformat(),
                "reason": reason,
                "failure_codes": failure_codes,
                "failure_reasons": [REJECT.get(c, c) for c in failure_codes],
                "current_evidence": {
                    "age": age, "region": current.region,
                    "hukou_region": current.hukou_region,
                    "care_level": current.care_level, "low_income": current.low_income,
                },
                "reverses_decision_seq": record.created_seq,
                "immutable_note": "原决定保留不变，本分录为冲正依据",
            })
            # 未使用的预留释放；已结算部分走追回（CLAWBACK）。
            self._release_unused(record, "REVIEW_FAILED")
            for segment in list(record.settled_segments):
                if segment.get("clawback_seq"):
                    continue
                for part in segment["annual_split"]:
                    if part["amount_cents"] <= 0:
                        continue
                    entry = self._append("CLAWBACK", {
                        "application_id": application_id,
                        "year": part["year"],
                        "amount_cents": part["amount_cents"],
                        "reason": "REVIEW_FAILED",
                        "reverses_settle_seq": segment["entry_seq"],
                    })
                    self._mark_clawback(record, part["year"], part["amount_cents"])
                segment["clawback_seq"] = True
            record.status = REVOKED
            return {"application_id": application_id, "result": "FAILED",
                    "failure_codes": failure_codes, "status": record.status}

    def _mark_clawback(self, record, year, amount_cents):
        for reserve in record.reserves:
            if reserve["year"] == year and reserve["state"] != "RELEASED":
                reserve["clawed_back_cents"] = reserve.get("clawed_back_cents", 0) + amount_cents
                reserve["state"] = "CLAWED_BACK"

    # ================= 争议与替代设备 =================

    def open_dispute(self, application_id: str, parties: list[str], description: str) -> dict:
        record = self._get(application_id)
        for party in parties:
            if party not in DISPUTE_PARTIES:
                raise ValidationError(f"争议方须为 {DISPUTE_PARTIES} 之一", details={"party": party})
        with self.ledger.lock:
            if record.dispute:
                raise ConflictError("该申请已在争议处理中", code="dispute_open")
            record.dispute = {
                "parties": parties, "description": description,
                "opened_on": date.today().isoformat(), "resolved": False,
            }
            entry = self._append("DISPUTE_OPENED", {
                "application_id": application_id,
                "parties": parties, "description": description,
                "effect": "SETTLEMENT_FROZEN_REPLACEMENT_ALLOWED",
            })
            record.dispute["entry_seq"] = entry.seq
            return record.dispute

    def request_replacement(self, application_id: str, replacement_device_id: str,
                            reason_fault_event_id: str | None = None) -> dict:
        """争议期间也可调用：冻结结算，不中断必要的替代设备。"""
        record = self._get(application_id)
        with self.ledger.lock:
            if record.status not in (ACTIVE, SETTLING):
                raise ConflictError(
                    REJECT["APPLICATION_NOT_ACTIVE"],
                    code="application_not_active",
                    details={"status": record.status},
                )
            event = {
                "event_id": uuid.uuid4().hex,
                "type": REPLACEMENT,
                "occurred_on": date.today().isoformat(),
                "device_id": record.snapshot.device.device_id,
                "replacement_device_id": replacement_device_id,
                "reason_fault_event_id": reason_fault_event_id,
                "note": "争议期替代设备，不改变补贴结算",
            }
            record.replacement_devices.append(event)
            record.events.append(event)
            self._append("REPLACEMENT_APPROVED", {
                "application_id": application_id, "event": event,
                "settlement_frozen": bool(record.dispute),
            })
            return event

    def close_dispute(self, application_id: str, responsible_party: str,
                      resolution_note: str = "", as_of: str | None = None) -> dict:
        record = self._get(application_id)
        if responsible_party not in DISPUTE_PARTIES:
            raise ValidationError(f"责任方须为 {DISPUTE_PARTIES} 之一")
        with self.ledger.lock:
            if not record.dispute:
                raise ConflictError("该申请没有进行中的争议", code="no_dispute")
            record.dispute.update({
                "resolved": True, "responsible_party": responsible_party,
                "resolution_note": resolution_note,
                "closed_on": date.today().isoformat(),
            })
            self._append("DISPUTE_CLOSED", {
                "application_id": application_id,
                "responsible_party": responsible_party,
                "resolution_note": resolution_note,
                "effect": "SETTLEMENT_UNFROZEN",
            })
            settled_hint = None
            # 解冻后恢复结算：有挂账归还按归还日截断，否则按给定日期结清到期分段。
            if record.status in (ACTIVE, SETTLING):
                settle_day = (record.pending_return["occurred_on"]
                              if record.pending_return else (as_of or date.today().isoformat()))
                newly, early_closed = self._settle_due(record, parse_day(settle_day))
                if early_closed or self._all_resolved(record):
                    record.status = SETTLED
                elif newly:
                    record.status = SETTLING
                if record.pending_return:
                    record.pending_return = None
                settled_hint = newly
            return {"dispute": record.dispute, "post_close_settlement": settled_hint,
                    "status": record.status}

    # ================= 复算（只读） =================

    def replay(self, application_id: str = None, sample: dict | None = None,
               policy_version: str | None = None, catalog_version: str | None = None) -> dict:
        """用指定政策/目录版本对旧申请或家庭样例重新试算。

        纯只读：不追加台账分录、不占用额度。逐项给出家庭自付、财政
        责任、拒绝原因，并与原决定做差异对比。
        """
        if sample is not None:
            request = deepcopy(sample)
            if policy_version:
                request["policy_version"] = policy_version
            if catalog_version:
                request["catalog_version"] = catalog_version
            _, snap, evaluation, decision, status = self._prepare(request)
            original = None
            target_id = None
        else:
            record = self._get(application_id)
            target_id = application_id
            original = record.decision
            frozen = record.snapshot.to_dict()
            if original is None:
                # 原申请即被拒绝：只重跑资格并解释原因，不存在差异对比。
                chosen_route = frozen.get("requested_route")
                chosen_term = None
            else:
                chosen_route = original["route"]
                chosen_term = next(t for t in frozen["terms"] if t["route"] == chosen_route)
            request = {
                "applicant_id": frozen["evidence"]["applicant"]["applicant_id"],
                "device_id": frozen["device"]["device_id"],
                "apply_on": frozen["evidence"]["captured_on"],
                "route": chosen_route,
            }
            if chosen_term is not None:
                catalog_changed = bool(
                    catalog_version and catalog_version != frozen["catalog"]["version"])
                request.update({"discounts": [{"name": "frozen",
                                                "amount_cents": chosen_term["discount_cents"]}],
                                "months": chosen_term["months"] or None,
                                "lease_start": chosen_term["lease_start"]})
                # 仅在显式换用新目录版本时改按新目录参考价计价；
                # 同版本复算保留原合同金额，保证结果与原决定一致。
                if not catalog_changed:
                    key = {PURCHASE: "contract_cents",
                           "RENT": "rent_contract_cents",
                           INSTITUTION: "service_contract_cents"}[chosen_route]
                    request[key] = chosen_term["contract_cents"]
            request = {k: v for k, v in request.items() if v is not None}
            if policy_version:
                request["policy_version"] = policy_version
            if catalog_version:
                request["catalog_version"] = catalog_version
            _, snap, evaluation, decision, status = self._prepare(request)

        items = []
        for route, ev in evaluation.evaluations.items():
            if ev.eligible:
                bd = money_breakdown(snap, ev)
                items.append({
                    "route": route, "eligible": True,
                    "tier_name": ev.tier_name, "ratio_bp": ev.ratio_bp,
                    "fiscal_subsidy_cents": bd["subsidy_cents"],
                    "fiscal_subsidy_yuan": cents_to_yuan(bd["subsidy_cents"]),
                    "household_paid_cents": bd["household_paid_cents"],
                    "household_paid_yuan": cents_to_yuan(bd["household_paid_cents"]),
                    "annual_split": bd["annual_split"],
                    "reject_codes": [c for c in ev.reject_codes if c != "DUPLICATE_BENEFIT"],
                })
            else:
                items.append({
                    "route": route, "eligible": False,
                    "reject_codes": ev.reject_codes,
                    "reject_reasons": ev.reject_reasons,
                })
        result = {
            "application_id": target_id,
            "mode": "sample" if sample is not None else "historical",
            "policy_version": snap.policy.version,
            "catalog_version": snap.catalog.version,
            "apply_on": snap.term.apply_on.isoformat(),
            "items": items,
            "winning_route": evaluation.winning_route,
            "read_only": True,
            "ledger_entries_before": len(self.ledger),
        }
        if decision:
            result["recalculated_decision"] = {
                "route": decision["route"],
                "subsidy_cents": decision["subsidy_cents"],
                "subsidy_yuan": cents_to_yuan(decision["subsidy_cents"]),
                "household_paid_cents": decision["household_paid_cents"],
                "household_paid_yuan": cents_to_yuan(decision["household_paid_cents"]),
                "annual_split": decision["annual_split"],
            }
        if original is not None:
            result["diff_vs_original"] = self._diff_decisions(original, decision)
        result["ledger_entries_after"] = len(self.ledger)
        result["no_write"] = result["ledger_entries_before"] == result["ledger_entries_after"]
        return result

    @staticmethod
    def _diff_decisions(original: dict | None, recalculated: dict | None) -> dict:
        if original is None and recalculated is None:
            return {"changed": False}
        if original is None or recalculated is None:
            return {"changed": True,
                    "original_route": original and original["route"],
                    "recalculated_route": recalculated and recalculated["route"]}
        delta_subsidy = recalculated["subsidy_cents"] - original["subsidy_cents"]
        return {
            "changed": delta_subsidy != 0 or original["route"] != recalculated["route"],
            "original_route": original["route"],
            "recalculated_route": recalculated["route"],
            "original_subsidy_cents": original["subsidy_cents"],
            "recalculated_subsidy_cents": recalculated["subsidy_cents"],
            "delta_subsidy_cents": delta_subsidy,
            "original_household_paid_cents": original["household_paid_cents"],
            "recalculated_household_paid_cents": recalculated["household_paid_cents"],
        }

    # ================= 额度查询 =================

    def quota_status(self, applicant_id: str | None = None, region: str | None = None,
                     year: int | None = None) -> dict:
        year = year or date.today().year
        out = {"year": year}
        if applicant_id is not None:
            applicant = self.registry.get_applicant(applicant_id)
            policy = self.registry.resolve_policy(applicant.region, date.today())
            used = self._personal_used(applicant_id, year)
            out["personal"] = {
                "applicant_id": applicant_id, "region": applicant.region,
                "cap_cents": policy.personal_annual_cap_cents,
                "used_cents": used,
                "reserved_cents": self._personal_used_by_type(applicant_id, year, "RESERVE"),
                "available_cents": policy.personal_annual_cap_cents - used,
            }
        if region is not None:
            budget = self.registry.get_budget(region, year)
            used = self._region_used(region, year)
            out["region"] = {
                "region": region,
                "budget_cents": budget,
                "used_cents": used,
                "available_cents": None if budget is None else budget - used,
            }
        return out

    # ================= 查询与校验 =================

    def get_application(self, application_id: str) -> dict:
        return self._decision_view(self._get(application_id))

    def list_applications(self) -> list[dict]:
        return [self._decision_view(r) for r in self._applications.values()]

    def verify_ledger(self) -> dict:
        return self.ledger.verify_chain()

    def _get(self, application_id: str) -> ApplicationRecord:
        record = self._applications.get(application_id)
        if record is None:
            raise NotFoundError(f"申请不存在: {application_id}")
        return record

    # ---- 台账聚合：额度占用恒等式 RESERVE - RELEASE - CLAWBACK ----

    def _flow_entries(self):
        return self.ledger.all()

    def _personal_used(self, applicant_id: str, year: int) -> int:
        total = 0
        for entry in self._flow_entries():
            p = entry.payload
            if entry.type == "RESERVE" and p.get("applicant_id") == applicant_id and p.get("year") == year:
                total += p["amount_cents"]
            elif entry.type in ("RELEASE", "CLAWBACK") and p.get("application_id"):
                record = self._applications.get(p["application_id"])
                if record and record.snapshot.evidence.applicant.applicant_id == applicant_id and p.get("year") == year:
                    total -= p["amount_cents"]
        return total

    def _personal_used_by_type(self, applicant_id: str, year: int, _type: str) -> int:
        total = 0
        for entry in self._flow_entries():
            if entry.type != _type:
                continue
            p = entry.payload
            if p.get("applicant_id") == applicant_id and p.get("year") == year:
                total += p["amount_cents"]
        return total

    def _region_used(self, region: str, year: int) -> int:
        total = 0
        for entry in self._flow_entries():
            p = entry.payload
            if entry.type == "RESERVE" and p.get("region") == region and p.get("year") == year:
                total += p["amount_cents"]
            elif entry.type in ("RELEASE", "CLAWBACK") and p.get("year") == year:
                record = self._applications.get(p.get("application_id"))
                if record and record.snapshot.policy.region == region:
                    total -= p["amount_cents"]
        return total

    def _policy_by_version(self, region, version):
        for policy in self.registry._policies.get(region, ()):
            if policy.version == version:
                return policy
        raise NotFoundError(f"政策版本不存在: {region}/{version}")

    def _catalog_by_version(self, region, version):
        for catalog in self.registry._catalogs.get(region, ()):
            if catalog.version == version:
                return catalog
        raise NotFoundError(f"目录版本不存在: {region}/{version}")

    def _append(self, entry_type, payload):
        return self.ledger.append(entry_type, payload)

    @staticmethod
    def _new_id(day: date, prefix: str) -> str:
        return f"{prefix}-{day.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _sanitize_request(request: dict) -> dict:
        allowed = {
            "applicant_id", "device_id", "route", "apply_on",
            "policy_version", "catalog_version", "months", "lease_start",
            "contract_cents", "discounts",
        }
        return {k: v for k, v in request.items() if k in allowed}

    def _decision_view(self, record: ApplicationRecord) -> dict:
        view = record.to_dict()
        view["settlement_totals"] = {
            "settled_cents": sum(s["amount_cents"] for s in record.settled_segments),
            "reserved_open_cents": sum(
                r["amount_cents"] - r.get("settled_cents", 0) - r.get("clawed_back_cents", 0)
                for r in record.reserves if r["state"] in ("RESERVED", "PARTIAL_SETTLED", "SETTLED")
            ),
        }
        return view
