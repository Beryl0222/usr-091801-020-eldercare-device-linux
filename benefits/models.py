"""领域模型：路径、政策版本、设备目录、申请人证据与申请快照。

模型均为不可变值对象。申请一旦做出决定，其资格证据、政策版本与
目录版本整体冻结进 ApplicationSnapshot，之后政策调整或老人情况
变化都不影响原决定；复算只能显式指定新版本另行试算。
"""

from dataclasses import dataclass, field
from datetime import date

from .money import parse_day, yuan_to_cents

# ---- 枚举（以稳定字符串落库） ------------------------------------------

PURCHASE = "PURCHASE"          # 家庭购置补贴
RENT = "RENT"                  # 社区租赁补贴
INSTITUTION = "INSTITUTION"    # 机构服务
ROUTES = (PURCHASE, RENT, INSTITUTION)

MONTHLY_ROUTES = (RENT, INSTITUTION)

# 履约只允许这四类设备事件，健康监测不在其列。
ACTIVATION = "ACTIVATION"      # 设备启用（交付）
MAINTENANCE = "MAINTENANCE"    # 维护
FAULT = "FAULT"                # 故障
RETURN = "RETURN"              # 归还
REPLACEMENT = "REPLACEMENT"    # 替代设备投入
DEVICE_EVENT_TYPES = (ACTIVATION, MAINTENANCE, FAULT, RETURN, REPLACEMENT)

# 各事件允许携带的字段白名单；任何白名单外字段一律拒绝入库。
EVENT_ALLOWED_FIELDS = {
    ACTIVATION: {"device_id", "occurred_on", "note"},
    MAINTENANCE: {"device_id", "occurred_on", "note", "maintenance_type"},
    FAULT: {"device_id", "occurred_on", "note", "fault_code", "reported_by"},
    RETURN: {"device_id", "occurred_on", "note", "condition"},
    REPLACEMENT: {
        "device_id", "occurred_on", "note",
        "replacement_device_id", "reason_fault_event_id",
    },
}

# 明显属于健康监测的字段名片段，命中即拒绝，从入口阻断医疗数据进入。
HEALTH_FIELD_HINTS = (
    "health", "vital", "heart", "blood", "pulse", "spo2", "sleep",
    "fall_detect", "location_track", "体温", "血压", "心率", "血氧",
    "脉搏", "睡眠", "健康", "体征", "定位",
)

VENDOR = "VENDOR"          # 厂商责任
COMMUNITY = "COMMUNITY"    # 社区责任
FAMILY = "FAMILY"          # 家庭责任
DISPUTE_PARTIES = (VENDOR, COMMUNITY, FAMILY)


# ---- 政策 ---------------------------------------------------------------

@dataclass(frozen=True)
class RuleTier:
    """同一通道内的待遇档。基础档无附加条件；申请人可命中多档时就高。"""

    name: str
    ratio_bp: int                     # 补贴比例，基点（万分率），6000 = 60%
    one_time_cap_cents: int | None = None   # 购置类单件上限
    monthly_cap_cents: int | None = None    # 租赁/机构服务每月上限
    low_income_only: bool = False
    min_care_level: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "RuleTier":
        return cls(
            name=data["name"],
            ratio_bp=int(data["ratio_bp"]),
            one_time_cap_cents=_opt_cents(data.get("one_time_cap_cents"), data.get("one_time_cap")),
            monthly_cap_cents=_opt_cents(data.get("monthly_cap_cents"), data.get("monthly_cap")),
            low_income_only=bool(data.get("low_income_only", False)),
            min_care_level=int(data.get("min_care_level", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ratio_bp": self.ratio_bp,
            "one_time_cap_cents": self.one_time_cap_cents,
            "monthly_cap_cents": self.monthly_cap_cents,
            "low_income_only": self.low_income_only,
            "min_care_level": self.min_care_level,
        }


@dataclass(frozen=True)
class RouteRule:
    route: str
    min_age: int
    min_care_level: int = 0
    require_local_hukou: bool = False       # 户籍须在政策所在地区
    require_residency: bool = False         # 或持有本地居住证
    low_income_only: bool = False
    eligible_categories: frozenset[str] = frozenset()
    rent_min_months: int = 0
    rent_max_months: int = 0
    tiers: tuple[RuleTier, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: dict) -> "RouteRule":
        tiers = tuple(RuleTier.from_dict(t) for t in data.get("tiers", ()))
        return cls(
            route=data["route"],
            min_age=int(data["min_age"]),
            min_care_level=int(data.get("min_care_level", 0)),
            require_local_hukou=bool(data.get("require_local_hukou", False)),
            require_residency=bool(data.get("require_residency", False)),
            low_income_only=bool(data.get("low_income_only", False)),
            eligible_categories=frozenset(data.get("eligible_categories", ())),
            rent_min_months=int(data.get("rent_min_months", 0)),
            rent_max_months=int(data.get("rent_max_months", 0)),
            tiers=tiers,
        )

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "min_age": self.min_age,
            "min_care_level": self.min_care_level,
            "require_local_hukou": self.require_local_hukou,
            "require_residency": self.require_residency,
            "low_income_only": self.low_income_only,
            "eligible_categories": sorted(self.eligible_categories),
            "rent_min_months": self.rent_min_months,
            "rent_max_months": self.rent_max_months,
            "tiers": [t.to_dict() for t in self.tiers],
        }


@dataclass(frozen=True)
class PolicyVersion:
    """地区政策版本：按申请日生效，决定后内容整体冻结。"""

    region: str
    version: str
    effective_from: date
    rules: tuple[RouteRule, ...]
    personal_annual_cap_cents: int
    effective_to: date | None = None

    def rule(self, route: str) -> RouteRule | None:
        for rule in self.rules:
            if rule.route == route:
                return rule
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyVersion":
        return cls(
            region=data["region"],
            version=data["version"],
            effective_from=parse_day(data["effective_from"]),
            effective_to=parse_day(data["effective_to"]) if data.get("effective_to") else None,
            rules=tuple(RouteRule.from_dict(r) for r in data["rules"]),
            personal_annual_cap_cents=_cents(data.get("personal_annual_cap_cents"), data.get("personal_annual_cap")),
        )

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "rules": [r.to_dict() for r in self.rules],
            "personal_annual_cap_cents": self.personal_annual_cap_cents,
        }


# ---- 设备目录 ------------------------------------------------------------

@dataclass(frozen=True)
class CatalogEntry:
    device_id: str
    name: str
    category: str
    allowed_routes: frozenset[str]
    price_cents: int = 0                 # 购置参考价
    monthly_rent_cents: int = 0          # 社区租赁月费
    monthly_service_cents: int = 0       # 机构服务月费

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogEntry":
        return cls(
            device_id=data["device_id"],
            name=data["name"],
            category=data["category"],
            allowed_routes=frozenset(data["allowed_routes"]),
            price_cents=_cents(data.get("price_cents"), data.get("price")),
            monthly_rent_cents=_cents(data.get("monthly_rent_cents"), data.get("monthly_rent")),
            monthly_service_cents=_cents(data.get("monthly_service_cents"), data.get("monthly_service")),
        )

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "category": self.category,
            "allowed_routes": sorted(self.allowed_routes),
            "price_cents": self.price_cents,
            "monthly_rent_cents": self.monthly_rent_cents,
            "monthly_service_cents": self.monthly_service_cents,
        }


@dataclass(frozen=True)
class CatalogVersion:
    region: str
    version: str
    effective_from: date
    entries: tuple[CatalogEntry, ...]
    effective_to: date | None = None

    def entry(self, device_id: str) -> CatalogEntry | None:
        for item in self.entries:
            if item.device_id == device_id:
                return item
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogVersion":
        return cls(
            region=data["region"],
            version=data["version"],
            effective_from=parse_day(data["effective_from"]),
            effective_to=parse_day(data["effective_to"]) if data.get("effective_to") else None,
            entries=tuple(CatalogEntry.from_dict(e) for e in data["entries"]),
        )

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "entries": [e.to_dict() for e in self.entries],
        }


# ---- 申请人证据 ----------------------------------------------------------

@dataclass(frozen=True)
class Applicant:
    """民政登记的老人档案。复核时取最新档案，申请时按申请日定格。"""

    applicant_id: str
    name: str
    birth_date: date
    region: str                       # 待遇归属地区
    hukou_region: str                 # 户籍地区
    has_local_residency: bool         # 本地居住证
    care_level: int                   # 0 能力完好 … 3 重度失能
    low_income: bool
    assessment_ref: str = ""          # 能力评估凭据编号
    income_ref: str = ""              # 低收入认定凭据编号

    def age_on(self, day: date) -> int:
        age = day.year - self.birth_date.year
        if (day.month, day.day) < (self.birth_date.month, self.birth_date.day):
            age -= 1
        return age

    @classmethod
    def from_dict(cls, data: dict) -> "Applicant":
        return cls(
            applicant_id=data["applicant_id"],
            name=data.get("name", ""),
            birth_date=parse_day(data["birth_date"]),
            region=data["region"],
            hukou_region=data.get("hukou_region", data["region"]),
            has_local_residency=bool(data.get("has_local_residency", False)),
            care_level=int(data.get("care_level", 0)),
            low_income=bool(data.get("low_income", False)),
            assessment_ref=data.get("assessment_ref", ""),
            income_ref=data.get("income_ref", ""),
        )

    def to_dict(self) -> dict:
        return {
            "applicant_id": self.applicant_id,
            "name": self.name,
            "birth_date": self.birth_date.isoformat(),
            "region": self.region,
            "hukou_region": self.hukou_region,
            "has_local_residency": self.has_local_residency,
            "care_level": self.care_level,
            "low_income": self.low_income,
            "assessment_ref": self.assessment_ref,
            "income_ref": self.income_ref,
        }


@dataclass(frozen=True)
class EvidenceSnapshot:
    """申请日冻结下来的资格证据。"""

    applicant: Applicant
    age: int
    captured_on: date
    assessment_ref: str
    income_ref: str

    def to_dict(self) -> dict:
        return {
            "applicant": self.applicant.to_dict(),
            "age": self.age,
            "captured_on": self.captured_on.isoformat(),
            "assessment_ref": self.assessment_ref,
            "income_ref": self.income_ref,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceSnapshot":
        applicant = Applicant.from_dict(data["applicant"])
        return cls(
            applicant=applicant,
            age=int(data["age"]),
            captured_on=parse_day(data["captured_on"]),
            assessment_ref=data.get("assessment_ref", ""),
            income_ref=data.get("income_ref", ""),
        )


@dataclass(frozen=True)
class ContractTerm:
    """合同要素：购置一次性，租赁/机构按月并给出租期。"""

    route: str
    apply_on: date
    reference_cents: int          # 目录参考金额（购置价或月租×月数）
    contract_cents: int           # 合同实际金额（优惠前）
    discount_cents: int           # 各类优惠合计
    lease_start: date | None = None
    lease_end: date | None = None
    months: int = 0

    @property
    def net_cents(self) -> int:
        """优惠后基数，补贴以此为限。"""
        return max(0, self.contract_cents - self.discount_cents)

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "apply_on": self.apply_on.isoformat(),
            "reference_cents": self.reference_cents,
            "contract_cents": self.contract_cents,
            "discount_cents": self.discount_cents,
            "net_cents": self.net_cents,
            "lease_start": self.lease_start.isoformat() if self.lease_start else None,
            "lease_end": self.lease_end.isoformat() if self.lease_end else None,
            "months": self.months,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContractTerm":
        return cls(
            route=data["route"],
            apply_on=parse_day(data["apply_on"]),
            reference_cents=int(data["reference_cents"]),
            contract_cents=int(data["contract_cents"]),
            discount_cents=int(data["discount_cents"]),
            lease_start=parse_day(data["lease_start"]) if data.get("lease_start") else None,
            lease_end=parse_day(data["lease_end"]) if data.get("lease_end") else None,
            months=int(data.get("months", 0)),
        )


@dataclass(frozen=True)
class ApplicationSnapshot:
    """决定所依据的完整事实：证据 + 政策版本 + 目录版本 + 三条路径各自的
    合同要素 + 设备。请求未指定租期时，月度路径的 term 为 None。"""

    evidence: EvidenceSnapshot
    policy: PolicyVersion
    catalog: CatalogVersion
    device: CatalogEntry
    terms: tuple[ContractTerm, ...]
    requested_route: str | None = None

    def term_for(self, route: str) -> ContractTerm | None:
        for term in self.terms:
            if term.route == route:
                return term
        return None

    @property
    def term(self) -> ContractTerm:
        """显式请求路径的合同要素；自动比选时回落到购置要素。"""
        if self.requested_route:
            found = self.term_for(self.requested_route)
            if found is not None:
                return found
        return self.term_for(PURCHASE)

    def to_dict(self) -> dict:
        return {
            "evidence": self.evidence.to_dict(),
            "policy": self.policy.to_dict(),
            "catalog": self.catalog.to_dict(),
            "device": self.device.to_dict(),
            "terms": [t.to_dict() for t in self.terms],
            "requested_route": self.requested_route,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ApplicationSnapshot":
        return cls(
            evidence=EvidenceSnapshot.from_dict(data["evidence"]),
            policy=PolicyVersion.from_dict(data["policy"]),
            catalog=CatalogVersion.from_dict(data["catalog"]),
            device=CatalogEntry.from_dict(data["device"]),
            terms=tuple(ContractTerm.from_dict(t) for t in data["terms"]),
            requested_route=data.get("requested_route"),
        )


def _cents(value, yuan_value):
    if value is not None:
        return int(value)
    if yuan_value is not None:
        return yuan_to_cents(yuan_value)
    raise ValueError("缺少金额字段")


def _opt_cents(value, yuan_value):
    if value is not None:
        return int(value)
    if yuan_value is not None:
        return yuan_to_cents(yuan_value)
    return None
