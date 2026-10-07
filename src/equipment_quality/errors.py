"""服务层可观察错误。"""


class ServiceError(RuntimeError):
    code = "service_error"


class NotFound(ServiceError):
    code = "not_found"


class Conflict(ServiceError):
    code = "conflict"


class InvalidState(ServiceError):
    code = "invalid_state"
