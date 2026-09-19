"""不可变政策版本、设备目录与资格/补贴试算（纯函数，无副作用）。

申请提交时由服务层选定政策版本，并把政策版本内容、目录条目与资格证据
**原样冻结**进决定；后续政策调整不改变已存决定，只影响复算结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

from .errors import PayloadRejected
from .money import (
    INSTITUTION,
    PROGRAM_LABELS,
    PROGRAM_ORDER,
    PURCHASE,
    RENTAL,
    age_on,
    daily_from_monthly,
    parse_date,
    ratio_amount,
    split_by_year,
)

# 资格拒绝原因码（逐项输出，供审核页与复算报告展示）
REASON_POLICY_NOT_IN_FORCE = "policy_not_in_force"
REASON_REGION_MISMATCH = "region_mismatch"
REASON_HUKOU_MISMATCH = "hukou_mismatch"
REASON_AGE_BELOW_MINIMUM = "age_below_minimum"
REASON_CARE_LEVEL_MISMATCH = "care_level_mismatch"
REASON_LOW_INCOME_REQUIRED = "low_income_required"
REASON_DEVICE_NOT_IN_CATALOG = "device_not_in_catalog"
REASON_DEVICE_NOT_COVERED = "device_not_covered_by_program"
REASON_DUPLICATE_BENEFIT = "duplicate_device_benefit"
REASON_TERM_INVALID = "rental_term_invalid"

CARE_LEVELS = ("未评估", "一级", "二级", "三级", "四级", "五级")
HUKOU_LOCAL = "local"
HUKOU_NONLOCAL = "nonlocal"
HUKOU_LABELS = {HUKOU_LOCAL: "本地户籍", HUKOU_NONLOCAL: "非本地户籍"}


@dataclass(frozen=True)
class EligibilityEvidence:
    """申请日冻结的资格证据。只存结论性字段与来源文书编号，不存原始材料。"""

    person_id: str
    household_id: str
    region_code: str
    hukou: str
    birth_date: date
    care_level: int  # 0 表示未评估，1-5 对应一级至五级
    low_income: bool
    assessed_at: date
    document_ids: tuple = ()

    @staticmethod
    def from_dict(data: dict) -> "EligibilityEvidence":
        try:
            return EligibilityEvidence(
                person_id=str(data["person_id"]),
                household_id=str(data["household_id"]),
                region_code=str(data["region_code"]),
                hukou=str(data["hukou"]),
                birth_date=parse_date(data["birth_date"]),
                care_level=int(data["care_level"]),
                low_income=bool(data["low_income"]),
                assessed_at=parse_date(data["assessed_at"]),
                document_ids=tuple(data.get("document_ids", ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadRejected(f"资格证据字段不完整: {exc}") from exc

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "household_id": self.household_id,
            "region_code": self.region_code,
            "hukou": self.hukou,
            "hukou_label": HUKOU_LABELS.get(self.hukou, self.hukou),
            "birth_date": self.birth_date.isoformat(),
            "age_at_application": None,  # 由试算填充
            "care_level": self.care_level,
            "care_level_label": CARE_LEVELS[self.care_level]
            if 0 <= self.care_level < len(CARE_LEVELS)
            else str(self.care_level),
            "low_income": self.low_income,
            "assessed_at": self.assessed_at.isoformat(),
            "document_ids": list(self.document_ids),
        }


@dataclass(frozen=True)
class DeviceItem:
    """设备目录条目；价格以分为单位。"""

    sku: str
    name: str
    category: str
    purchase_price_cents: int
    monthly_rent_cents: int
    monthly_service_cents: int
    programs: tuple = PROGRAM_ORDER  # 该设备可走的补贴路径

    @staticmethod
    def from_dict(data: dict) -> "DeviceItem":
        price = lambda k: int(data.get(k, 0))
        programs = tuple(data.get("programs", PROGRAM_ORDER))
        unknown = set(programs) - set(PROGRAM_ORDER)
        if unknown:
            raise PayloadRejected(f"设备目录存在未知补贴路径: {sorted(unknown)}")
        return DeviceItem(
            sku=str(data["sku"]),
            name=str(data["name"]),
            category=str(data.get("category", "未分类")),
            purchase_price_cents=price("purchase_price_cents"),
            monthly_rent_cents=price("monthly_rent_cents"),
            monthly_service_cents=price("monthly_service_cents"),
            programs=programs,
        )

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "category": self.category,
            "purchase_price_cents": self.purchase_price_cents,
            "monthly_rent_cents": self.monthly_rent_cents,
            "monthly_service_cents": self.monthly_service_cents,
            "programs": list(self.programs),
        }


@dataclass(frozen=True)
class DeviceCatalog:
    region_code: str
    version: str
    items: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict) -> "DeviceCatalog":
        items = {}
        for raw in data.get("items", []):
            item = DeviceItem.from_dict(raw)
            items[item.sku] = item
        return DeviceCatalog(
            region_code=str(data["region_code"]),
            version=str(data["version"]),
            items=items,
        )

    def get(self, sku: str) -> Optional[DeviceItem]:
        return self.items.get(sku)

    def to_dict(self) -> dict:
        return {
            "region_code": self.region_code,
            "version": self.version,
            "items": [item.to_dict() for item in self.items.values()],
        }


@dataclass(frozen=True)
class ProgramRule:
    """单条补贴路径规则。"""

    program: str
    rate_permille: int
    per_item_cap_cents: int
    annual_cap_cents: int
    min_age: int = 0
    care_levels: tuple = (1, 2, 3, 4, 5)
    hukou_types: tuple = (HUKOU_LOCAL, HUKOU_NONLOCAL)
    low_income_only: bool = False

    @staticmethod
    def from_dict(data: dict) -> "ProgramRule":
        program = str(data["program"])
        if program not in PROGRAM_LABELS:
            raise PayloadRejected(f"未知补贴路径: {program}")
        rate = int(data["rate_permille"])
        if not 0 <= rate <= 1000:
            raise PayloadRejected("补贴比例必须在 0 至 1000 千分比之间")
        return ProgramRule(
            program=program,
            rate_permille=rate,
            per_item_cap_cents=int(data["per_item_cap_cents"]),
            annual_cap_cents=int(data["annual_cap_cents"]),
            min_age=int(data.get("min_age", 0)),
            care_levels=tuple(data.get("care_levels", (1, 2, 3, 4, 5))),
            hukou_types=tuple(data.get("hukou_types", (HUKOU_LOCAL, HUKOU_NONLOCAL))),
            low_income_only=bool(data.get("low_income_only", False)),
        )

    def to_dict(self) -> dict:
        return {
            "program": self.program,
            "program_label": PROGRAM_LABELS[self.program],
            "rate_permille": self.rate_permille,
            "per_item_cap_cents": self.per_item_cap_cents,
            "annual_cap_cents": self.annual_cap_cents,
            "min_age": self.min_age,
            "care_levels": list(self.care_levels),
            "hukou_types": list(self.hukou_types),
            "low_income_only": self.low_income_only,
        }


@dataclass(frozen=True)
class PolicyVersion:
    """某地区某一版政策；一旦发布内容不可变（同名同版本重复注册即冲突）。"""

    region_code: str
    version: str
    name: str
    valid_from: date
    valid_to: Optional[date]
    rules: dict

    @staticmethod
    def from_dict(data: dict) -> "PolicyVersion":
        rules = {}
        for raw in data.get("rules", []):
            rule = ProgramRule.from_dict(raw)
            rules[rule.program] = rule
        if not rules:
            raise PayloadRejected("政策版本至少包含一条补贴路径规则")
        valid_to = data.get("valid_to")
        return PolicyVersion(
            region_code=str(data["region_code"]),
            version=str(data["version"]),
            name=str(data.get("name", f"政策 {data['version']}")),
            valid_from=parse_date(data["valid_from"]),
            valid_to=parse_date(valid_to) if valid_to else None,
            rules=rules,
        )

    def in_force_on(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to

    def to_dict(self) -> dict:
        return {
            "region_code": self.region_code,
            "version": self.version,
            "name": self.name,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "rules": [rule.to_dict() for rule in self.rules.values()],
        }


# --------------------------------------------------------------------------- #
# 资格评估与补贴试算
# --------------------------------------------------------------------------- #


def _evaluate_eligibility(rule, evidence, policy, device, application_date,
                          ignore_effective_date=False):
    reasons = []
    if not ignore_effective_date and not policy.in_force_on(application_date):
        reasons.append(REASON_POLICY_NOT_IN_FORCE)
    if evidence.region_code != policy.region_code:
        reasons.append(REASON_REGION_MISMATCH)
    if evidence.hukou not in rule.hukou_types:
        reasons.append(REASON_HUKOU_MISMATCH)
    age = age_on(evidence.birth_date, application_date)
    if age < rule.min_age:
        reasons.append(REASON_AGE_BELOW_MINIMUM)
    if evidence.care_level not in rule.care_levels:
        reasons.append(REASON_CARE_LEVEL_MISMATCH)
    if rule.low_income_only and not evidence.low_income:
        reasons.append(REASON_LOW_INCOME_REQUIRED)
    if device is None:
        reasons.append(REASON_DEVICE_NOT_IN_CATALOG)
    elif rule.program not in device.programs:
        reasons.append(REASON_DEVICE_NOT_COVERED)
    return reasons


def _yearly_segments(program, device, term_start, term_end):
    """按自然年返回 (year, days, base_cents)；购置为一次性、不切年。"""
    if program == PURCHASE:
        return [(term_start.year, None, device.purchase_price_cents)]
    monthly = (
        device.monthly_rent_cents
        if program == RENTAL
        else device.monthly_service_cents
    )
    daily = daily_from_monthly(monthly)
    return [
        (year, days, daily * days)
        for year, _start, _end, days in split_by_year(term_start, term_end)
    ]


def quote(application, evidence, policy, catalog, duplicate_skus=(),
          ignore_effective_date=False):
    """对一笔申请逐条路径试算。纯函数：复算旧申请时同样调用它。

    ``application`` 字段：
        application_date、sku、programs（候选路径，缺省三条全试）、
        term_start/term_end（租赁或机构服务租期）。
    ``duplicate_skus`` 该家庭已享受补贴且未冲正的设备 sku，
    命中即追加"不得重复享受"原因。
    ``ignore_effective_date`` 仅供政策复算：工作人员显式选定某版本做假设重算时，
    跳过政策生效期校验（其余资格条件仍逐项判定）。
    返回可直接序列化的 dict（同时含冻结快照与逐项明细）。
    """
    application_date = parse_date(application["application_date"])
    sku = str(application["sku"])
    device = catalog.get(sku) if catalog is not None else None
    candidates = tuple(application.get("programs") or PROGRAM_ORDER)
    term_start = (
        parse_date(application["term_start"]) if application.get("term_start") else None
    )
    term_end = (
        parse_date(application["term_end"]) if application.get("term_end") else None
    )

    evidence_view = evidence.to_dict()
    evidence_view["age_at_application"] = age_on(
        evidence.birth_date, application_date
    )

    program_results = []
    for program in PROGRAM_ORDER:
        if program not in candidates or program not in policy.rules:
            continue
        rule = policy.rules[program]
        reasons = list(_evaluate_eligibility(
            rule, evidence, policy, device, application_date,
            ignore_effective_date=ignore_effective_date,
        ))
        if sku in duplicate_skus:
            reasons.append(REASON_DUPLICATE_BENEFIT)

        yearly = []
        base_total = subsidy_total = family_total = 0
        if not reasons and device is not None:
            if program in (RENTAL, INSTITUTION):
                if not term_start or not term_end or term_end <= term_start:
                    reasons.append(REASON_TERM_INVALID)
                else:
                    # 租赁/机构补贴自申请日之后的交付租期起算；申请年按租期自然分段
                    segments = _yearly_segments(program, device, term_start, term_end)
                    for year, days, base in segments:
                        raw = ratio_amount(base, rule.rate_permille)
                        subsidy = min(raw, rule.per_item_cap_cents)
                        yearly.append(
                            {
                                "year": year,
                                "days": days,
                                "base_cents": base,
                                "rate_permille": rule.rate_permille,
                                "subsidy_before_cap_cents": raw,
                                "per_item_cap_cents": rule.per_item_cap_cents,
                                "cap_clipped": raw > rule.per_item_cap_cents,
                                "subsidy_cents": subsidy,
                                "family_cents": base - subsidy,
                                "annual_cap_cents": rule.annual_cap_cents,
                            }
                        )
                        base_total += base
                        subsidy_total += subsidy
                        family_total += base - subsidy
            else:
                base = device.purchase_price_cents
                raw = ratio_amount(base, rule.rate_permille)
                subsidy = min(raw, rule.per_item_cap_cents)
                yearly.append(
                    {
                        "year": application_date.year,
                        "days": None,
                        "base_cents": base,
                        "rate_permille": rule.rate_permille,
                        "subsidy_before_cap_cents": raw,
                        "per_item_cap_cents": rule.per_item_cap_cents,
                        "cap_clipped": raw > rule.per_item_cap_cents,
                        "subsidy_cents": subsidy,
                        "family_cents": base - subsidy,
                        "annual_cap_cents": rule.annual_cap_cents,
                    }
                )
                base_total = base
                subsidy_total = subsidy
                family_total = base - subsidy

        program_results.append(
            {
                "program": program,
                "program_label": PROGRAM_LABELS[program],
                "eligible": not reasons,
                "rejection_reasons": reasons,
                "rate_permille": rule.rate_permille,
                "base_cents": base_total,
                "subsidy_cents": subsidy_total,
                "family_cents": family_total,
                "fiscal_cents": subsidy_total,  # 财政责任 = 补贴合计
                "yearly": yearly,
            }
        )

    # 比例就高：合格路径中补贴额最大者；并列时按固定路径顺序确定，保证可复现
    eligible_results = [r for r in program_results if r["eligible"]]
    selected = None
    if eligible_results:
        selected = max(
            eligible_results,
            key=lambda r: (r["subsidy_cents"], -PROGRAM_ORDER.index(r["program"])),
        )

    return {
        "application_date": application_date.isoformat(),
        "sku": sku,
        "term_start": term_start.isoformat() if term_start else None,
        "term_end": term_end.isoformat() if term_end else None,
        "frozen": {
            "policy": policy.to_dict(),
            "device": device.to_dict() if device else {"sku": sku, "missing": True},
            "evidence": evidence_view,
        },
        "programs": program_results,
        "selected": selected,
        "rejected": selected is None,
        "rejection_reasons": sorted(
            {reason for r in program_results for reason in r["rejection_reasons"]}
        ),
    }


def recalc_settlement_window(program, device, window_start, window_end, rule):
    """复算/冲正公用：对 [start, end) 租期按年分段计算各年补贴。"""
    if program == PURCHASE:
        base = device.purchase_price_cents
        raw = ratio_amount(base, rule.rate_permille)
        return [
            {
                "year": window_start.year,
                "days": None,
                "base_cents": base,
                "subsidy_cents": min(raw, rule.per_item_cap_cents),
                "family_cents": base - min(raw, rule.per_item_cap_cents),
            }
        ]
    monthly = (
        device.monthly_rent_cents
        if program == RENTAL
        else device.monthly_service_cents
    )
    daily = daily_from_monthly(monthly)
    out = []
    for year, _s, _e, days in split_by_year(window_start, window_end):
        base = daily * days
        raw = ratio_amount(base, rule.rate_permille)
        subsidy = min(raw, rule.per_item_cap_cents)
        out.append(
            {
                "year": year,
                "days": days,
                "base_cents": base,
                "subsidy_cents": subsidy,
                "family_cents": base - subsidy,
            }
        )
    return out
