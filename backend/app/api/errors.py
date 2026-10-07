"""Public errors contain fixed messages and safe field paths only."""

MESSAGES = {
    "AUTHENTICATION_FAILED": "用户名或密码无效。",
    "UNAUTHENTICATED": "请重新登录。",
    "FORBIDDEN": "无权访问此咨询。",
    "INVALID_REQUEST": "请求格式无效。",
    "ORDER_REFERENCE_MISSING": "此咨询没有订单绑定。",
    "CONTEXT_REQUIRED": "请先取得有效的当前 Context。",
    "RESOURCE_NOT_FOUND": "资源不存在。",
    "INTERNAL_ERROR": "请求无法完成，请凭请求 ID 联系支持。",
    "VERSION_CONFLICT": "咨询版本已变化，请重新读取。",
    "STATE_CONFLICT": "当前咨询或操作状态不允许此请求。",
    "BUSY": "此咨询已有进行中的操作。",
    "IDEMPOTENCY_CONFLICT": "此幂等键已用于另一请求。",
    "SOURCE_NOT_FOUND": "必需来源未返回记录。",
    "SOURCE_TIMEOUT": "必需来源读取超时。",
    "SOURCE_UNAVAILABLE": "必需来源当前不可用。",
    "SOURCE_INVALID_RESPONSE": "必需来源返回了无效记录。",
    "SOURCE_BINDING_MISMATCH": "来源记录与咨询绑定不一致。",
    "SOURCE_REJECTED": "必需来源拒绝此读取。",
    "SOURCE_CONFLICT": "必需来源请求冲突。",
    "INTERRUPTED": "操作已由运维确认中断，请使用新请求重新取数。",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.details = details or {}
        super().__init__(code)

    def payload(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": MESSAGES[self.code],
                "request_id": request_id,
                "details": self.details,
            }
        }
