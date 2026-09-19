"""金额、比例与日期工具。

金额一律使用整数"分"，全程不出现浮点；比例使用整数千分比（permille，
800 表示 80%）。租期按半开区间 ``[start, end)`` 计日，跨年在自然年边界切分。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

PURCHASE = "purchase"
RENTAL = "rental"
INSTITUTION = "institution"

PROGRAM_LABELS = {
    PURCHASE: "家庭购置补贴",
    RENTAL: "社区租赁补贴",
    INSTITUTION: "机构服务补贴",
}
PROGRAM_ORDER = (PURCHASE, RENTAL, INSTITUTION)


def yuan(cents: int) -> str:
    """把分格式化为人民币元字符串，仅用于展示。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def ratio_amount(base_cents: int, permille: int) -> int:
    """按千分比计算金额，四舍五入到分。"""
    return (base_cents * permille * 2 + 1000) // 2000


def daily_from_monthly(monthly_cents: int) -> int:
    """月费用换算日费用：年总额 / 365，四舍五入到分。"""
    return (monthly_cents * 12 * 2 + 365) // 730


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def age_on(birth_date: date, on_date: date) -> int:
    """申请当日的周岁年龄。"""
    return on_date.year - birth_date.year - (
        (on_date.month, on_date.day) < (birth_date.month, birth_date.day)
    )


def split_by_year(start: date, end: date):
    """把半开区间 [start, end) 按自然年切成 (year, seg_start, seg_end, days)。"""
    if end <= start:
        raise ValueError("租期结束日必须晚于开始日")
    segments = []
    cursor = start
    while cursor < end:
        next_year = date(cursor.year + 1, 1, 1)
        seg_end = min(end, next_year)
        segments.append((cursor.year, cursor, seg_end, (seg_end - cursor).days))
        cursor = seg_end
    return segments
