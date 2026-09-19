"""领域错误：携带稳定 code 与可展示详情，便于接口层映射状态码。"""


class DomainError(Exception):
    """所有领域错误的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class VersionConflict(DomainError):
    """政策/目录版本不可覆盖：同名同版本只能注册一次。"""

    code = "version_conflict"
    http_status = 409


class QuotaExceeded(DomainError):
    """年度额度不足；details 携带超占前后的额度证明。"""

    code = "quota_exceeded"
    http_status = 422


class InvalidTransition(DomainError):
    code = "invalid_transition"
    http_status = 409


class SettlementFrozen(DomainError):
    """故障责任争议期间结算冻结，但替代设备等履约不受影响。"""

    code = "settlement_frozen"
    http_status = 409


class PayloadRejected(DomainError):
    """事件或证据不在白名单（含健康监测字段），整份拒绝、不留存。"""

    code = "payload_rejected"
    http_status = 422
