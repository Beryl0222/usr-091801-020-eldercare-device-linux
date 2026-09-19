"""种子数据：两地区、跨版本政策与目录、年度预算、不同条件的老人样例。

金额单位为分。政策与目录均给出 2025/2026 两个生效版本，便于用旧申请
演示“政策调整后逐项复算”。
"""

# 杭州：户籍或居住证均可，待遇较高；2026 版上调比例与年度上限。
HZ_POLICIES = [
    {
        "region": "HZ", "version": "HZ-2025",
        "effective_from": "2025-01-01", "effective_to": "2025-12-31",
        "personal_annual_cap_cents": 600_000,
        "rules": [
            {
                "route": "PURCHASE", "min_age": 60, "min_care_level": 2,
                "require_local_hukou": True,
                "eligible_categories": ["外骨骼", "看护机器人"],
                "tiers": [
                    {"name": "基础档", "ratio_bp": 5000, "one_time_cap_cents": 200_000},
                    {"name": "低收入照护档", "ratio_bp": 7000,
                     "one_time_cap_cents": 300_000, "low_income_only": True},
                ],
            },
            {
                "route": "RENT", "min_age": 60, "min_care_level": 1,
                "require_residency": True,
                "rent_min_months": 3, "rent_max_months": 12,
                "eligible_categories": ["外骨骼", "看护机器人"],
                "tiers": [
                    {"name": "基础档", "ratio_bp": 4000, "monthly_cap_cents": 30_000},
                    {"name": "低收入照护档", "ratio_bp": 6000,
                     "monthly_cap_cents": 40_000, "low_income_only": True},
                ],
            },
            {
                "route": "INSTITUTION", "min_age": 70, "min_care_level": 2,
                "require_local_hukou": True, "require_residency": True,
                "eligible_categories": ["看护机器人"],
                "tiers": [
                    {"name": "机构基础档", "ratio_bp": 3000, "monthly_cap_cents": 50_000},
                ],
            },
        ],
    },
    {
        "region": "HZ", "version": "HZ-2026",
        "effective_from": "2026-01-01",
        "personal_annual_cap_cents": 800_000,
        "rules": [
            {
                "route": "PURCHASE", "min_age": 60, "min_care_level": 2,
                "require_local_hukou": True,
                "eligible_categories": ["外骨骼", "看护机器人"],
                "tiers": [
                    {"name": "基础档", "ratio_bp": 6000, "one_time_cap_cents": 240_000},
                    {"name": "低收入照护档", "ratio_bp": 8000,
                     "one_time_cap_cents": 360_000, "low_income_only": True},
                ],
            },
            {
                "route": "RENT", "min_age": 60, "min_care_level": 1,
                "require_residency": True,
                "rent_min_months": 3, "rent_max_months": 24,
                "eligible_categories": ["外骨骼", "看护机器人"],
                "tiers": [
                    {"name": "基础档", "ratio_bp": 5000, "monthly_cap_cents": 35_000},
                    {"name": "低收入照护档", "ratio_bp": 7000,
                     "monthly_cap_cents": 45_000, "low_income_only": True},
                ],
            },
            {
                "route": "INSTITUTION", "min_age": 70, "min_care_level": 2,
                "require_local_hukou": True, "require_residency": True,
                "eligible_categories": ["看护机器人"],
                "tiers": [
                    {"name": "机构基础档", "ratio_bp": 4000, "monthly_cap_cents": 60_000},
                ],
            },
        ],
    },
]

HZ_CATALOGS = [
    {
        "region": "HZ", "version": "HZ-CAT-2025",
        "effective_from": "2025-01-01", "effective_to": "2025-12-31",
        "entries": [
            {"device_id": "EXO-01", "name": "助行外骨骼A型", "category": "外骨骼",
             "allowed_routes": ["PURCHASE", "RENT"],
             "price_cents": 3_000_000, "monthly_rent_cents": 150_000,
             "monthly_service_cents": 0},
            {"device_id": "BOT-01", "name": "居家看护机器人", "category": "看护机器人",
             "allowed_routes": ["PURCHASE", "RENT", "INSTITUTION"],
             "price_cents": 1_200_000, "monthly_rent_cents": 80_000,
             "monthly_service_cents": 200_000},
        ],
    },
    {
        "region": "HZ", "version": "HZ-CAT-2026",
        "effective_from": "2026-01-01",
        "entries": [
            {"device_id": "EXO-01", "name": "助行外骨骼A型", "category": "外骨骼",
             "allowed_routes": ["PURCHASE", "RENT"],
             "price_cents": 2_800_000, "monthly_rent_cents": 140_000,
             "monthly_service_cents": 0},
            {"device_id": "EXO-02", "name": "助行外骨骼B型(轻量)", "category": "外骨骼",
             "allowed_routes": ["PURCHASE", "RENT"],
             "price_cents": 1_800_000, "monthly_rent_cents": 90_000,
             "monthly_service_cents": 0},
            {"device_id": "BOT-01", "name": "居家看护机器人", "category": "看护机器人",
             "allowed_routes": ["PURCHASE", "RENT", "INSTITUTION"],
             "price_cents": 1_100_000, "monthly_rent_cents": 70_000,
             "monthly_service_cents": 190_000},
        ],
    },
]

# 成都：机构服务放开户籍限制，只要本地居住；购置要求本地户籍。
CD_POLICIES = [
    {
        "region": "CD", "version": "CD-2026",
        "effective_from": "2026-01-01",
        "personal_annual_cap_cents": 500_000,
        "rules": [
            {
                "route": "PURCHASE", "min_age": 65, "min_care_level": 2,
                "require_local_hukou": True,
                "tiers": [
                    {"name": "基础档", "ratio_bp": 4000, "one_time_cap_cents": 150_000},
                ],
            },
            {
                "route": "RENT", "min_age": 60, "min_care_level": 1,
                "require_residency": True,
                "rent_min_months": 1, "rent_max_months": 12,
                "tiers": [
                    {"name": "基础档", "ratio_bp": 4500, "monthly_cap_cents": 25_000},
                ],
            },
            {
                "route": "INSTITUTION", "min_age": 70, "min_care_level": 2,
                "require_residency": True,
                "tiers": [
                    {"name": "机构基础档", "ratio_bp": 5000, "monthly_cap_cents": 55_000},
                ],
            },
        ],
    },
]

CD_CATALOGS = [
    {
        "region": "CD", "version": "CD-CAT-2026",
        "effective_from": "2026-01-01",
        "entries": [
            {"device_id": "BOT-CD01", "name": "川渝看护终端", "category": "看护机器人",
             "allowed_routes": ["PURCHASE", "RENT", "INSTITUTION"],
             "price_cents": 900_000, "monthly_rent_cents": 60_000,
             "monthly_service_cents": 180_000},
        ],
    },
]

APPLICANTS = [
    # A001 高龄重度失能低收入，三路径条件最充分
    {"applicant_id": "A001", "name": "王桂英", "birth_date": "1945-03-10",
     "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
     "care_level": 3, "low_income": True,
     "assessment_ref": "PG-2025-A001", "income_ref": "LI-A001"},
    # A002 中龄中度失能，非低收入
    {"applicant_id": "A002", "name": "李建国", "birth_date": "1955-07-20",
     "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
     "care_level": 2, "low_income": False, "assessment_ref": "PG-2025-A002"},
    # A003 轻度失能，只能走租赁
    {"applicant_id": "A003", "name": "赵秀兰", "birth_date": "1962-02-05",
     "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
     "care_level": 1, "low_income": False, "assessment_ref": "PG-2026-A003"},
    # A004 外地户籍且无居住证：购置与机构被户籍挡下，租赁被居住挡下
    {"applicant_id": "A004", "name": "孙德海", "birth_date": "1948-11-01",
     "region": "HZ", "hukou_region": "AH", "has_local_residency": False,
     "care_level": 2, "low_income": False, "assessment_ref": "PG-2026-A004"},
    # A005 年龄不足
    {"applicant_id": "A005", "name": "周小梅", "birth_date": "1970-01-15",
     "region": "HZ", "hukou_region": "HZ", "has_local_residency": True,
     "care_level": 2, "low_income": False, "assessment_ref": "PG-2026-A005"},
    # A006 成都高龄老人
    {"applicant_id": "A006", "name": "吴志远", "birth_date": "1940-05-05",
     "region": "CD", "hukou_region": "CD", "has_local_residency": True,
     "care_level": 3, "low_income": True,
     "assessment_ref": "PG-2026-A006", "income_ref": "LI-A006"},
]

BUDGETS = [
    {"region": "HZ", "year": 2025, "amount_cents": 20_000_000},
    {"region": "HZ", "year": 2026, "amount_cents": 2_000_000},
    {"region": "CD", "year": 2026, "amount_cents": 1_500_000},
]


def load_seed(registry) -> None:
    for policy in [*HZ_POLICIES, *CD_POLICIES]:
        registry.add_policy(policy)
    for catalog in [*HZ_CATALOGS, *CD_CATALOGS]:
        registry.add_catalog(catalog)
    for applicant in APPLICANTS:
        registry.upsert_applicant(applicant)
    for budget in BUDGETS:
        registry.set_budget(budget["region"], budget["year"], budget["amount_cents"])
