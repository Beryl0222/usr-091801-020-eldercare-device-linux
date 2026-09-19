"""版本注册表：政策、设备目录、老人档案与地区年度预算。

政策与目录只增不改：新版本登记新的 version 与生效日，解析时按申请日
取“当日生效的最新版本”。老人档案允许更新（复核用新证据），但申请
当时的证据已冻结进快照，档案更新不影响历史决定。
"""

from datetime import date

from .errors import ConflictError, NotFoundError, ValidationError
from .models import Applicant, CatalogVersion, PolicyVersion
from .money import yuan_to_cents


class Registry:
    def __init__(self):
        self._policies: dict[str, list[PolicyVersion]] = {}
        self._catalogs: dict[str, list[CatalogVersion]] = {}
        self._applicants: dict[str, Applicant] = {}
        # (region, year) -> 年度财政预算（分）
        self._budgets: dict[tuple[str, int], int] = {}

    # ---- 政策版本 -------------------------------------------------------

    def add_policy(self, data: dict) -> PolicyVersion:
        policy = PolicyVersion.from_dict(data)
        bucket = self._policies.setdefault(policy.region, [])
        for existing in bucket:
            if existing.version == policy.version:
                raise ConflictError(
                    f"政策版本已存在: {policy.region}/{policy.version}",
                    details={"region": policy.region, "version": policy.version},
                )
            if self._intervals_overlap(
                existing.effective_from, existing.effective_to,
                policy.effective_from, policy.effective_to,
            ):
                raise ConflictError(
                    f"政策生效区间与 {existing.version} 重叠",
                    details={"region": policy.region, "version": policy.version},
                )
        bucket.append(policy)
        bucket.sort(key=lambda p: p.effective_from)
        return policy

    def resolve_policy(self, region: str, on: date) -> PolicyVersion:
        candidates = [
            p for p in self._policies.get(region, ())
            if p.effective_from <= on and (p.effective_to is None or on <= p.effective_to)
        ]
        if not candidates:
            raise NotFoundError(
                f"{region} 在 {on.isoformat()} 没有生效中的政策版本",
                code="policy_not_in_force",
                details={"region": region, "date": on.isoformat()},
            )
        return max(candidates, key=lambda p: p.effective_from)

    # ---- 设备目录版本 ----------------------------------------------------

    def add_catalog(self, data: dict) -> CatalogVersion:
        catalog = CatalogVersion.from_dict(data)
        bucket = self._catalogs.setdefault(catalog.region, [])
        for existing in bucket:
            if existing.version == catalog.version:
                raise ConflictError(
                    f"目录版本已存在: {catalog.region}/{catalog.version}",
                    details={"region": catalog.region, "version": catalog.version},
                )
            if self._intervals_overlap(
                existing.effective_from, existing.effective_to,
                catalog.effective_from, catalog.effective_to,
            ):
                raise ConflictError(
                    f"目录生效区间与 {existing.version} 重叠",
                    details={"region": catalog.region, "version": catalog.version},
                )
        bucket.append(catalog)
        bucket.sort(key=lambda c: c.effective_from)
        return catalog

    def resolve_catalog(self, region: str, on: date) -> CatalogVersion:
        candidates = [
            c for c in self._catalogs.get(region, ())
            if c.effective_from <= on and (c.effective_to is None or on <= c.effective_to)
        ]
        if not candidates:
            raise NotFoundError(
                f"{region} 在 {on.isoformat()} 没有生效中的设备目录",
                code="catalog_not_in_force",
                details={"region": region, "date": on.isoformat()},
            )
        return max(candidates, key=lambda c: c.effective_from)

    # ---- 老人档案 --------------------------------------------------------

    def upsert_applicant(self, data: dict) -> Applicant:
        applicant = Applicant.from_dict(data)
        self._applicants[applicant.applicant_id] = applicant
        return applicant

    def get_applicant(self, applicant_id: str) -> Applicant:
        applicant = self._applicants.get(applicant_id)
        if applicant is None:
            raise NotFoundError(
                f"老人档案不存在: {applicant_id}",
                details={"applicant_id": applicant_id},
            )
        return applicant

    # ---- 地区年度预算 ----------------------------------------------------

    def set_budget(self, region: str, year: int, amount) -> int:
        cents = amount if isinstance(amount, int) else yuan_to_cents(amount)
        if cents < 0:
            raise ValidationError("年度预算不能为负")
        self._budgets[(region, int(year))] = cents
        return cents

    def get_budget(self, region: str, year: int) -> int | None:
        """未登记的年度预算返回 None（不限地区总量）；显式置 0 即额度为零。"""
        return self._budgets.get((region, int(year)))

    def export_seed(self) -> dict:
        return {
            "policies": [p.to_dict() for bucket in self._policies.values() for p in bucket],
            "catalogs": [c.to_dict() for bucket in self._catalogs.values() for c in bucket],
            "applicants": [a.to_dict() for a in self._applicants.values()],
            "budgets": [
                {"region": region, "year": year, "amount_cents": cents}
                for (region, year), cents in sorted(self._budgets.items())
            ],
        }

    @staticmethod
    def _intervals_overlap(start_a, end_a, start_b, end_b) -> bool:
        end_a = end_a or date(9999, 12, 31)
        end_b = end_b or date(9999, 12, 31)
        return start_a <= end_b and start_b <= end_a
