"""领域错误与统一的拒绝原因码。"""


class DomainError(Exception):
    """所有可预期的业务错误基类，带稳定 code 供接口层映射。"""

    code = "domain_error"

    def __init__(self, message: str, code: str | None = None, details=None):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class ValidationError(DomainError):
    code = "invalid_request"


class ImmutableViolation(DomainError):
    code = "immutable_violation"


# 资格拒绝原因码（复算说明里逐项引用，保持稳定）
REJECT = {
    "AGE": "年龄不符合该补贴要求",
    "CARE_LEVEL": "照护等级不符合该补贴要求",
    "HUKOU": "户籍不在该政策适用地区",
    "LOW_INCOME": "不满足低收入身份要求",
    "RESIDENCY": "不满足本地居住要求",
    "REGION": "所在地区不适用该政策",
    "DEVICE_NOT_CATALOGED": "设备不在当期补贴目录",
    "DEVICE_CATEGORY": "设备品类不适用该补贴",
    "ROUTE_UNAVAILABLE": "该路径在本地区未开放",
    "POLICY_NOT_IN_FORCE": "申请日没有生效中的政策版本",
    "RENT_TERM": "租期不符合最短/最长限制",
    "PERSONAL_CAP_EXHAUSTED": "个人年度补贴额度已用尽",
    "REGION_BUDGET_EXHAUSTED": "地区年度财政预算已用尽",
    "APPLICATION_NOT_ACTIVE": "申请状态不允许该操作",
    "DEVICE_NOT_DELIVERED": "设备尚未启用交付",
    "DISPUTE_FROZEN": "故障责任争议处理中，结算已冻结",
    "HEALTH_DATA_FORBIDDEN": "健康监测数据由医疗服务方持有，补贴系统不得采集",
    "DUPLICATE_BENEFIT": "同类补贴已享受，不得重复申领",
}
