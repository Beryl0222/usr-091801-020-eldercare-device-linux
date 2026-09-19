"""资格评估与补贴计算引擎。

输入不可变快照，输出逐路径的资格结论、拒绝原因与补贴金额。规则：

* 三条路径（购置/租赁/机构服务）各自独立评估，命中条件的待遇档
  “比例就高”；同一申请只能享受一条路径，不重复享受——引擎给出
  补贴最高的一条作为建议决定。
* 购置类受单件上限约束；租赁/机构服务按月计算并受每月上限约束。
* 跨年租期把补贴按自然日、最大余数法分摊到各年度，供年度额度
  分别占用；分摊之和与补贴总额严格相等。
* 本引擎不读写台账，个人年度上限与地区预算的占用在账本层完成。
"""

from dataclasses import dataclass, field
from datetime import timedelta

from .errors import REJECT
from .models import (
    MONTHLY_ROUTES,
    PURCHASE,
    RENT,
    ROUTES,
    ApplicationSnapshot,
    RuleTier,
)
from .money import largest_remainder_split, split_by_calendar_year

BASIS_POINTS = 10_000


def add_months(day, months: int):
    """月份加减：目标月天数不足时落到月末。"""
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    if month == 12:
        next_month_first = day.replace(year=year + 1, month=1, day=1)
    else:
        next_month_first = day.replace(year=year, month=month + 1, day=1)
    last_day = (next_month_first - timedelta(days=1)).day
    return day.replace(year=year, month=month, day=min(day.day, last_day))


@dataclass(frozen=True)
class RouteEvaluation:
    route: str
    eligible: bool
    reject_codes: list[str]
    reject_reasons: list[str]
    tier_name: str | None
    ratio_bp: int
    months: int
    monthly_subsidy_cents: int
    subsidy_cents: int
    annual_split: list[dict] = field(default_factory=list)
    # 结算计划：购置为单个一次性分段；租赁/机构为逐月分段，
    # 每段带自身起止日与按自然年的分摊，供提前归还时精确截断。
    schedule: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "eligible": self.eligible,
            "reject_codes": self.reject_codes,
            "reject_reasons": self.reject_reasons,
            "tier_name": self.tier_name,
            "ratio_bp": self.ratio_bp,
            "months": self.months,
            "monthly_subsidy_cents": self.monthly_subsidy_cents,
            "subsidy_cents": self.subsidy_cents,
            "annual_split": self.annual_split,
            "schedule": self.schedule,
        }


@dataclass(frozen=True)
class Evaluation:
    evaluations: dict[str, RouteEvaluation]
    winning_route: str | None

    @property
    def winner(self) -> RouteEvaluation | None:
        return self.evaluations.get(self.winning_route) if self.winning_route else None

    def to_dict(self) -> dict:
        return {
            "routes": {r: self.evaluations[r].to_dict() for r in ROUTES if r in self.evaluations},
            "winning_route": self.winning_route,
        }


def evaluate(snapshot: ApplicationSnapshot) -> Evaluation:
    """对快照逐条路径评估资格并计算补贴。"""
    results: dict[str, RouteEvaluation] = {}
    for route in ROUTES:
        results[route] = _evaluate_route(snapshot, route)
    eligible = [e for e in results.values() if e.eligible and e.subsidy_cents > 0]
    # 比例就高、不重复享受：补贴最高者胜出；同额时购置优先（一次性结清）。
    winner = max(
        eligible,
        default=None,
        key=lambda e: (e.subsidy_cents, 1 if e.route == PURCHASE else 0),
    )
    return Evaluation(evaluations=results, winning_route=winner.route if winner else None)


def _evaluate_route(snapshot: ApplicationSnapshot, route: str) -> RouteEvaluation:
    evidence = snapshot.evidence
    applicant = evidence.applicant
    policy = snapshot.policy
    device = snapshot.device
    term = snapshot.term_for(route)
    rule = policy.rule(route)

    codes: list[str] = []
    if rule is None:
        codes.append("ROUTE_UNAVAILABLE")
    if route not in device.allowed_routes:
        codes.append("DEVICE_CATEGORY")
    if term is None:
        # 自动比选且请求未给租期：月度路径无法计价，不作为候选。
        codes.append("RENT_TERM")

    if rule is not None:
        if applicant.region != policy.region:
            codes.append("REGION")
        if evidence.age < rule.min_age:
            codes.append("AGE")
        if evidence.age is not None and applicant.care_level < rule.min_care_level:
            codes.append("CARE_LEVEL")
        if rule.low_income_only and not applicant.low_income:
            codes.append("LOW_INCOME")
        hukou_ok = applicant.hukou_region == policy.region
        residency_ok = applicant.has_local_residency
        if rule.require_local_hukou and rule.require_residency:
            if not (hukou_ok or residency_ok):
                codes.append("HUKOU")
        elif rule.require_local_hukou and not hukou_ok:
            codes.append("HUKOU")
        elif rule.require_residency and not residency_ok:
            codes.append("RESIDENCY")
        if rule.eligible_categories and device.category not in rule.eligible_categories:
            codes.append("DEVICE_CATEGORY")
        if term is not None and route in MONTHLY_ROUTES:
            months = term.months
            if months <= 0:
                codes.append("RENT_TERM")
            elif rule.rent_min_months and months < rule.rent_min_months:
                codes.append("RENT_TERM")
            elif rule.rent_max_months and months > rule.rent_max_months:
                codes.append("RENT_TERM")

    if codes:
        return RouteEvaluation(
            route=route, eligible=False, reject_codes=codes,
            reject_reasons=[REJECT.get(c, c) for c in codes],
            tier_name=None, ratio_bp=0, months=term.months if term else 0,
            monthly_subsidy_cents=0, subsidy_cents=0, annual_split=[], schedule=[],
        )

    tier = _pick_tier(rule.tiers, applicant.care_level, applicant.low_income)
    if tier is None:
        return RouteEvaluation(
            route=route, eligible=False,
            reject_codes=["CARE_LEVEL"], reject_reasons=[REJECT["CARE_LEVEL"]],
            tier_name=None, ratio_bp=0, months=term.months if term else 0,
            monthly_subsidy_cents=0, subsidy_cents=0, annual_split=[], schedule=[],
        )

    if route == PURCHASE:
        raw = term.net_cents * tier.ratio_bp // BASIS_POINTS
        subsidy = min(raw, tier.one_time_cap_cents) if tier.one_time_cap_cents is not None else raw
        split = [{"year": term.apply_on.year, "days": 0, "amount_cents": subsidy}]
        schedule = [{
            "index": 0, "start": term.apply_on.isoformat(),
            "end": term.apply_on.isoformat(),
            "subsidy_cents": subsidy, "annual_split": split,
        }]
        return RouteEvaluation(
            route=route, eligible=True, reject_codes=[], reject_reasons=[],
            tier_name=tier.name, ratio_bp=tier.ratio_bp, months=0,
            monthly_subsidy_cents=0, subsidy_cents=subsidy, annual_split=split,
            schedule=schedule,
        )

    # 租赁与机构服务：把优惠后总额按最大余数法分到每个月，
    # 逐月套比例与月上限，避免月度四舍五入后总额对不上。
    months = term.months
    monthly_bases = largest_remainder_split(term.net_cents, [1] * months)
    monthly_subsidies = []
    schedule = []
    for index, base in enumerate(monthly_bases):
        raw = base * tier.ratio_bp // BASIS_POINTS
        if tier.monthly_cap_cents is not None:
            raw = min(raw, tier.monthly_cap_cents)
        monthly_subsidies.append(raw)
        seg_start = add_months(term.lease_start, index)
        seg_end = add_months(term.lease_start, index + 1) - timedelta(days=1)
        seg_split = split_by_calendar_year(seg_start, seg_end, raw)
        schedule.append({
            "index": index,
            "start": seg_start.isoformat(), "end": seg_end.isoformat(),
            "subsidy_cents": raw, "annual_split": seg_split,
        })
    monthly_subsidy = monthly_subsidies[0] if monthly_subsidies else 0
    subsidy = sum(monthly_subsidies)
    # 年度总额与逐月分段的年度分摊必然一致，直接聚合即可。
    split: dict[int, dict] = {}
    for seg in schedule:
        for part in seg["annual_split"]:
            agg = split.setdefault(part["year"], {"year": part["year"], "days": 0, "amount_cents": 0})
            agg["days"] += part["days"]
            agg["amount_cents"] += part["amount_cents"]
    annual_split = [split[y] for y in sorted(split)]
    return RouteEvaluation(
        route=route, eligible=True, reject_codes=[], reject_reasons=[],
        tier_name=tier.name, ratio_bp=tier.ratio_bp, months=months,
        monthly_subsidy_cents=monthly_subsidy, subsidy_cents=subsidy,
        annual_split=annual_split, schedule=schedule,
    )


def _pick_tier(tiers, care_level: int, low_income: bool) -> RuleTier | None:
    """挑出申请人可命中的待遇档，比例就高；并列时低收入专档优先。"""
    usable = [
        t for t in tiers
        if care_level >= t.min_care_level and (not t.low_income_only or low_income)
    ]
    if not usable:
        return None
    return max(
        usable,
        key=lambda t: (t.ratio_bp, 1 if t.low_income_only else 0, t.min_care_level),
    )


def money_breakdown(snapshot: ApplicationSnapshot, evaluation: RouteEvaluation) -> dict:
    """逐项金额说明：优惠前、优惠、补贴、家庭自付。"""
    term = snapshot.term
    return {
        "route": evaluation.route,
        "reference_cents": term.reference_cents,
        "contract_cents": term.contract_cents,
        "discount_cents": term.discount_cents,
        "net_cents": term.net_cents,
        "subsidy_cents": evaluation.subsidy_cents,          # 财政责任
        "household_paid_cents": term.net_cents - evaluation.subsidy_cents,  # 家庭自付
        "annual_split": evaluation.annual_split,
    }
