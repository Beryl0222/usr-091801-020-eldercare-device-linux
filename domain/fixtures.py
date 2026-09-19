"""演示与自检用样例：地方政策、设备目录、家庭证据样例。

金额单位为分。年度上限刻意设置得较小，以便自检中演示并发占用与跨年分段。
"""
from __future__ import annotations

# 杭州市 2025 版政策：三条路径并存，比例与上限各不相同
POLICY_HZ_2025 = {
    "region_code": "330100",
    "version": "HZ-2025",
    "name": "杭州市适老设备补贴办法（2025版）",
    "valid_from": "2025-01-01",
    "valid_to": "2025-12-31",
    "rules": [
        {  # 家庭购置：七成补贴，单件最高 20000 元，年度 30000 元
            "program": "purchase",
            "rate_permille": 700,
            "per_item_cap_cents": 2_000_000,
            "annual_cap_cents": 3_000_000,
            "min_age": 70,
            "care_levels": [2, 3, 4, 5],
            "hukou_types": ["local"],
            "low_income_only": False,
        },
        {  # 社区租赁：六成补贴，单件年度最高 8000 元，年度 12000 元
            "program": "rental",
            "rate_permille": 600,
            "per_item_cap_cents": 800_000,
            "annual_cap_cents": 1_200_000,
            "min_age": 65,
            "care_levels": [1, 2, 3, 4, 5],
            "hukou_types": ["local", "nonlocal"],
            "low_income_only": False,
        },
        {  # 机构服务：五成补贴，低收入家庭八成；这里用通用五成为准
            "program": "institution",
            "rate_permille": 500,
            "per_item_cap_cents": 600_000,
            "annual_cap_cents": 1_000_000,
            "min_age": 60,
            "care_levels": [3, 4, 5],
            "hukou_types": ["local", "nonlocal"],
            "low_income_only": False,
        },
    ],
}

# 2026 版政策调整：购置比例降至六成、租赁提至七成（用于复算演示）
POLICY_HZ_2026 = {
    "region_code": "330100",
    "version": "HZ-2026",
    "name": "杭州市适老设备补贴办法（2026版）",
    "valid_from": "2026-01-01",
    "valid_to": None,
    "rules": [
        {
            "program": "purchase",
            "rate_permille": 600,
            "per_item_cap_cents": 1_800_000,
            "annual_cap_cents": 3_000_000,
            "min_age": 70,
            "care_levels": [2, 3, 4, 5],
            "hukou_types": ["local"],
            "low_income_only": False,
        },
        {
            "program": "rental",
            "rate_permille": 700,
            "per_item_cap_cents": 900_000,
            "annual_cap_cents": 1_500_000,
            "min_age": 65,
            "care_levels": [1, 2, 3, 4, 5],
            "hukou_types": ["local", "nonlocal"],
            "low_income_only": False,
        },
        {
            "program": "institution",
            "rate_permille": 500,
            "per_item_cap_cents": 600_000,
            "annual_cap_cents": 1_000_000,
            "min_age": 60,
            "care_levels": [3, 4, 5],
            "hukou_types": ["local", "nonlocal"],
            "low_income_only": False,
        },
    ],
}

# 设备目录：外骨骼与看护机器人，含购置价、月租、机构月服务费（分）
CATALOG_HZ_V1 = {
    "region_code": "330100",
    "version": "CAT-V1",
    "items": [
        {
            "sku": "EXO-A1",
            "name": "助行外骨骼 A1",
            "category": "外骨骼",
            "purchase_price_cents": 2_500_000,   # 25000 元
            "monthly_rent_cents": 120_000,       # 1200 元/月
            "monthly_service_cents": 150_000,    # 1500 元/月（机构）
            "programs": ["purchase", "rental", "institution"],
        },
        {
            "sku": "BOT-C2",
            "name": "看护机器人 C2",
            "category": "看护机器人",
            "purchase_price_cents": 1_800_000,   # 18000 元
            "monthly_rent_cents": 90_000,        # 900 元/月
            "monthly_service_cents": 110_000,
            "programs": ["purchase", "rental"],  # 该型号不走机构服务
        },
    ],
}

# 家庭样例：本地户籍中照护等级老人
SAMPLE_LOCAL_FAMILY = {
    "sample_id": "family-zhang",
    "evidence": {
        "person_id": "person-zhang-01",
        "household_id": "hh-zhang",
        "region_code": "330100",
        "hukou": "local",
        "birth_date": "1948-05-12",   # 2025 年 77 岁
        "care_level": 3,
        "low_income": False,
        "assessed_at": "2025-02-01",
        "document_ids": ["DOC-CARE-3-2025", "DOC-HUKOU-001"],
    },
}

# 家庭样例：非本地户籍、67 岁、一级照护（只符合租赁路径）
SAMPLE_NONLOCAL_FAMILY = {
    "sample_id": "family-li",
    "evidence": {
        "person_id": "person-li-02",
        "household_id": "hh-li",
        "region_code": "330100",
        "hukou": "nonlocal",
        "birth_date": "1958-03-20",   # 2025 年 67 岁
        "care_level": 1,
        "low_income": False,
        "assessed_at": "2025-03-10",
        "document_ids": ["DOC-CARE-1-2025"],
    },
}


def seed(service):
    """把样例注册进服务，返回关键标识字典。"""
    service.register_policy(POLICY_HZ_2025)
    service.register_policy(POLICY_HZ_2026)
    service.register_catalog(CATALOG_HZ_V1)
    service.register_sample(
        SAMPLE_LOCAL_FAMILY["sample_id"], SAMPLE_LOCAL_FAMILY["evidence"]
    )
    service.register_sample(
        SAMPLE_NONLOCAL_FAMILY["sample_id"], SAMPLE_NONLOCAL_FAMILY["evidence"]
    )
    return {
        "region": "330100",
        "policy_2025": "HZ-2025",
        "policy_2026": "HZ-2026",
        "catalog": "CAT-V1",
        "samples": ["family-zhang", "family-li"],
    }
