"""观测进入持久化之前的统一数值契约。

所有测点(时间、频率、响应、噪声)在写入测量表和事件流之前必须通过本模块校验;
校验失败时抛出携带字段与规则的 ContractViolation,而不是把问题推迟到分析阶段。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# 规则版本:契约每次变更都需要升级,隔离记录会保存处置时使用的版本。
CONTRACT_VERSION = "eq-numeric-contract-v1"

MAX_FUTURE_SKEW = timedelta(minutes=5)
MIN_OBSERVED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)


class ContractViolation(ValueError):
    """输入不满足数值契约;issues 逐条指出字段和规则。"""

    code = "contract_violation"

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = [dict(issue) for issue in issues]
        message = "; ".join(
            f"{issue.get('field') or 'row'}: {issue['message']}" for issue in self.issues
        )
        super().__init__(message)


def _issue(
    issues: list[dict[str, Any]],
    row: int | None,
    field: str | None,
    rule: str,
    message: str,
) -> None:
    item: dict[str, Any] = {"field": field, "rule": rule, "message": message}
    if row is not None:
        item["row"] = row
    issues.append(item)


def _finite_number(value: object, field: str, issues: list[dict[str, Any]], row: int | None) -> float | None:
    """把输入收敛为有限浮点数;拒绝 NaN、正负无穷、布尔和非数值字符串。"""

    if value is None:
        _issue(issues, row, field, "required", "字段必填")
        return None
    number: float | None = None
    if isinstance(value, bool):
        _issue(issues, row, field, "numeric_type", "必须是数值,不能是布尔")
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            _issue(issues, row, field, "numeric_string", "数值字符串不能为空")
        else:
            try:
                number = float(text)
            except ValueError:
                _issue(issues, row, field, "numeric_string", f"{text!r} 不是数值字符串")
    else:
        _issue(issues, row, field, "numeric_type", f"不支持的类型 {type(value).__name__},必须是数值")
    if number is not None and not math.isfinite(number):
        _issue(issues, row, field, "finite", "必须是有限数值,拒绝 NaN 和正负无穷")
        return None
    return number


def _measured_at(
    value: object, now: datetime, issues: list[dict[str, Any]], row: int | None
) -> str | None:
    """校验观测时间并规范化为 UTC ISO 文本。"""

    field = "measured_at"
    if value is None:
        _issue(issues, row, field, "required", "字段必填")
        return None
    if not isinstance(value, str):
        _issue(issues, row, field, "time_format", "必须是 ISO-8601 时间字符串")
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        _issue(issues, row, field, "time_format", f"{value!r} 不是有效的 ISO-8601 时间")
        return None
    if parsed.tzinfo is None:
        _issue(issues, row, field, "time_format", "必须携带时区偏移")
        return None
    moment = parsed.astimezone(timezone.utc)
    if moment > now + MAX_FUTURE_SKEW:
        _issue(issues, row, field, "time_not_future", "观测时间不能晚于接收时间")
        return None
    if moment < MIN_OBSERVED_AT:
        _issue(issues, row, field, "time_range", "观测时间早于允许下限 2000-01-01")
        return None
    return moment.isoformat()


def _required_text(value: object, field: str, issues: list[dict[str, Any]], row: int | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _issue(issues, row, field, "required", "必须是非空字符串")
        return None
    return value.strip()


@dataclass(frozen=True, slots=True)
class InstrumentRanges:
    """一台已注册设备的测点类型和量程。"""

    instrument_id: str
    point_type: str
    frequency_min_hz: float
    frequency_max_hz: float
    response_min: float
    response_max: float
    noise_max: float


def validate_instrument(
    instrument_id: object,
    point_type: object,
    ranges: Mapping[str, Any],
) -> InstrumentRanges:
    """校验设备注册信息;量程本身也必须满足数值契约。"""

    issues: list[dict[str, Any]] = []
    instrument = _required_text(instrument_id, "instrument_id", issues, None)
    kind = _required_text(point_type, "point_type", issues, None)
    if not isinstance(ranges, Mapping):
        _issue(issues, None, "ranges", "object", "量程必须是对象")
        raise ContractViolation(issues)
    frequency_min = _finite_number(ranges.get("frequency_min_hz"), "frequency_min_hz", issues, None)
    frequency_max = _finite_number(ranges.get("frequency_max_hz"), "frequency_max_hz", issues, None)
    response_min = _finite_number(ranges.get("response_min"), "response_min", issues, None)
    response_max = _finite_number(ranges.get("response_max"), "response_max", issues, None)
    noise_max = _finite_number(ranges.get("noise_max"), "noise_max", issues, None)
    if frequency_min is not None and frequency_min <= 0:
        _issue(issues, None, "frequency_min_hz", "positive", "频率量程下限必须大于零")
    if (
        frequency_min is not None
        and frequency_max is not None
        and not frequency_min < frequency_max
    ):
        _issue(issues, None, "frequency_max_hz", "range_order", "频率量程上限必须大于下限")
    if response_min is not None and response_max is not None and not response_min < response_max:
        _issue(issues, None, "response_max", "range_order", "响应量程上限必须大于下限")
    if noise_max is not None and noise_max <= 0:
        _issue(issues, None, "noise_max", "positive", "噪声上限必须大于零")
    if issues or instrument is None or kind is None:
        raise ContractViolation(issues)
    return InstrumentRanges(
        instrument_id=instrument,
        point_type=kind,
        frequency_min_hz=frequency_min,
        frequency_max_hz=frequency_max,
        response_min=response_min,
        response_max=response_max,
        noise_max=noise_max,
    )


def _canonical_number(value: object) -> str:
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return repr(value)


def measurement_digest(
    instrument_id: str,
    measured_at: str,
    test_frequency_hz: object,
    response: object,
    noise: object,
) -> str:
    """观测内容的确定性摘要,用于识别同一观测标识下内容不一致的重放。"""

    payload = json.dumps(
        [
            instrument_id,
            measured_at,
            _canonical_number(test_frequency_hz),
            _canonical_number(response),
            _canonical_number(noise),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedMeasurement:
    """通过契约、可以进入持久化的测点。"""

    measurement_id: str
    instrument_id: str
    measured_at: str
    test_frequency_hz: float
    response: float
    noise: float

    @property
    def content_sha256(self) -> str:
        return measurement_digest(
            self.instrument_id,
            self.measured_at,
            self.test_frequency_hz,
            self.response,
            self.noise,
        )


def validate_measurement(
    raw: object,
    instruments: Mapping[str, InstrumentRanges],
    *,
    now: datetime,
    row: int | None = None,
) -> NormalizedMeasurement:
    """按测点类型和设备量程校验一条测点;失败时抛出全部字段问题。"""

    issues: list[dict[str, Any]] = []
    if not isinstance(raw, Mapping):
        _issue(issues, row, None, "object", "测点必须是对象")
        raise ContractViolation(issues)
    measurement_id = _required_text(raw.get("measurement_id"), "measurement_id", issues, row)
    instrument_id = _required_text(raw.get("instrument"), "instrument", issues, row)
    ranges = instruments.get(instrument_id) if instrument_id is not None else None
    if instrument_id is not None and ranges is None:
        _issue(issues, row, "instrument", "instrument_unknown", "设备未注册,无法核对测点类型和量程")
    measured_at = _measured_at(raw.get("measured_at"), now, issues, row)
    frequency = _finite_number(raw.get("test_frequency_hz"), "test_frequency_hz", issues, row)
    if frequency is not None and frequency <= 0:
        _issue(issues, row, "test_frequency_hz", "positive", "频率必须大于零")
        frequency = None
    response = _finite_number(raw.get("response"), "response", issues, row)
    noise = _finite_number(raw.get("noise"), "noise", issues, row)
    if noise is not None and noise < 0:
        _issue(issues, row, "noise", "non_negative", "噪声不能为负")
        noise = None
    if ranges is not None:
        if frequency is not None and not ranges.frequency_min_hz <= frequency <= ranges.frequency_max_hz:
            _issue(
                issues, row, "test_frequency_hz", "device_range",
                f"超出设备量程 [{ranges.frequency_min_hz}, {ranges.frequency_max_hz}]",
            )
        if response is not None and not ranges.response_min <= response <= ranges.response_max:
            _issue(
                issues, row, "response", "device_range",
                f"超出设备量程 [{ranges.response_min}, {ranges.response_max}]",
            )
        if noise is not None and noise > ranges.noise_max:
            _issue(issues, row, "noise", "device_range", f"超出设备噪声上限 {ranges.noise_max}")
    if issues:
        raise ContractViolation(issues)
    return NormalizedMeasurement(
        measurement_id=measurement_id,
        instrument_id=instrument_id,
        measured_at=measured_at,
        test_frequency_hz=frequency,
        response=response,
        noise=noise,
    )


def stored_row_issues(
    instrument_id: str,
    measured_at: str,
    test_frequency_hz: object,
    response: object,
    noise: object,
    ranges: InstrumentRanges | None,
) -> list[dict[str, Any]]:
    """复核库中已存在的测点,供识别隔离和分析过滤使用。"""

    issues: list[dict[str, Any]] = []
    for field, value in (
        ("test_frequency_hz", test_frequency_hz),
        ("response", response),
        ("noise", noise),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            _issue(issues, None, field, "finite", "库中数值缺失或非有限(NaN/Infinity)")
    try:
        parsed = datetime.fromisoformat(str(measured_at))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        _issue(issues, None, "measured_at", "time_format", "库中观测时间不是带时区的 ISO-8601")
    if ranges is None:
        return issues
    if isinstance(test_frequency_hz, (int, float)) and math.isfinite(test_frequency_hz):
        if not ranges.frequency_min_hz <= test_frequency_hz <= ranges.frequency_max_hz:
            _issue(issues, None, "test_frequency_hz", "device_range", "超出已注册设备量程")
    if isinstance(response, (int, float)) and math.isfinite(response):
        if not ranges.response_min <= response <= ranges.response_max:
            _issue(issues, None, "response", "device_range", "超出已注册设备量程")
    if isinstance(noise, (int, float)) and math.isfinite(noise):
        if noise < 0 or noise > ranges.noise_max:
            _issue(issues, None, "noise", "device_range", "超出已注册设备噪声上限")
    return issues
