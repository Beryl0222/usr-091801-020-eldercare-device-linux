"""金额与日期的基础工具。

金额一律以整数“分”在领域内流转，杜绝浮点误差；周期按自然日
精确分摊，跨年时用最大余数法保证各段之和与总额严格相等。
"""

from datetime import date, timedelta


def yuan_to_cents(value) -> int:
    """把元（数字或最多两位小数的字符串）换算为整数分。"""
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        # 先经定点化避免 0.07*100 一类的二进制误差。
        return int(round(value * 100))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("金额不能为空")
        sign = -1 if text.startswith("-") else 1
        text = text.lstrip("+-")
        if "." in text:
            whole, frac = text.split(".", 1)
            if len(frac) > 2:
                raise ValueError(f"金额精度超过分: {value}")
            frac = (frac + "00")[:2]
        else:
            whole, frac = text, "00"
        if not whole.isdigit() or not frac.isdigit():
            raise ValueError(f"非法金额: {value}")
        return sign * (int(whole) * 100 + int(frac))
    raise ValueError(f"不支持的金额类型: {type(value)!r}")


def cents_to_yuan(cents: int) -> str:
    """整数分格式化为两位小数字符串。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_day(value) -> date:
    """解析 YYYY-MM-DD，或原样返回 date。"""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def inclusive_days(start: date, end: date) -> int:
    """闭区间 [start, end] 的天数。"""
    if end < start:
        raise ValueError(f"日期区间倒置: {start} > {end}")
    return (end - start).days + 1


def largest_remainder_split(total: int, weights: list[int]) -> list[int]:
    """按权重（天数）用最大余数法拆分整数总额，各份之和恒等于总额。"""
    if not weights:
        return []
    if any(w < 0 for w in weights):
        raise ValueError("权重不能为负")
    weight_sum = sum(weights)
    if weight_sum == 0:
        # 权重全为 0 时整额落到最后一份，调用方通常不会走到这里。
        result = [0] * len(weights)
        result[-1] = total
        return result
    floors = [total * w // weight_sum for w in weights]
    remainder = total - sum(floors)
    if remainder:
        order = sorted(
            range(len(weights)),
            key=lambda i: (total * weights[i] - floors[i] * weight_sum, -i),
            reverse=True,
        )
        for i in order[:remainder]:
            floors[i] += 1
    return floors


def split_by_calendar_year(start: date, end: date, amount: int):
    """把一笔金额按自然年覆盖天数分摊到各年度。

    返回 [(year, days, amount_cents), ...]，分摊金额之和恒等于原额。
    """
    segments: list[tuple[int, date, date]] = []
    cursor = start
    while cursor <= end:
        year_end = date(cursor.year, 12, 31)
        seg_end = min(end, year_end)
        segments.append((cursor.year, cursor, seg_end))
        cursor = seg_end + timedelta(days=1)
    weights = [inclusive_days(s, e) for _, s, e in segments]
    shares = largest_remainder_split(amount, weights)
    return [
        {"year": year, "days": days, "amount_cents": share}
        for (year, _, _), days, share in zip(segments, weights, shares)
    ]
