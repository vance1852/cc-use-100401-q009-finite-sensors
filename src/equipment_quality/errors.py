"""装备质量服务向 API 与调用方暴露的结构化错误。"""

from __future__ import annotations


class EquipmentError(Exception):
    """携带 HTTP 状态、字段与规则信息的业务错误。"""

    code = "equipment_error"
    status = 400

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        rule: str | None = None,
        failures: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.rule = rule
        self.failures = failures

    def details(self) -> dict:
        payload: dict = {}
        if self.field is not None:
            payload["field"] = self.field
        if self.rule is not None:
            payload["rule"] = self.rule
        if self.failures is not None:
            payload["failures"] = self.failures
        return payload


class ValidationFailed(EquipmentError):
    code = "validation_failed"
    status = 422


class Conflict(EquipmentError):
    code = "conflict"
    status = 409


class NotFound(EquipmentError):
    code = "not_found"
    status = 404
