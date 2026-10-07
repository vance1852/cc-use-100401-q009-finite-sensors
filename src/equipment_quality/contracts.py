"""观测进入持久化之前的统一数值契约。

井口遥测与测试观测在写入测量表和事件流之前，必须先通过本模块校验：
按测点类型与设备量程检查时间、频率、响应、噪声字段，拒绝 NaN、正负无穷、
非数值字符串与布尔值，并为同一观测标识的重放一致性提供内容摘要。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ValidationFailed

CONTRACT_VERSION = "equipment-observation-contract/1"

MIN_OBSERVED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE_TOLERANCE = timedelta(seconds=300)


class ContractViolation(ValidationFailed):
    """单个字段违反数值契约，携带字段名与规则名。"""

    def __init__(self, field: str, rule: str, message: str) -> None:
        super().__init__(message, field=field, rule=rule)


@dataclass(frozen=True, slots=True)
class PointType:
    """测点类型允许的物理硬边界，设备量程不得超出。"""

    key: str
    frequency_max_hz: Decimal
    response_min: Decimal
    response_max: Decimal
    noise_max: Decimal


POINT_TYPES: dict[str, PointType] = {
    "wellhead_telemetry": PointType(
        "wellhead_telemetry", Decimal("20000"), Decimal("-1000000"), Decimal("1000000"), Decimal("1000")
    ),
    "optical_response": PointType(
        "optical_response", Decimal("1000000000"), Decimal("0"), Decimal("10000"), Decimal("100")
    ),
    "general": PointType(
        "general", Decimal("1000000000"), Decimal("-1000000000"), Decimal("1000000000"), Decimal("1000000")
    ),
}


@dataclass(frozen=True, slots=True)
class InstrumentRange:
    """单台仪器在测点类型硬边界内声明的工作量程。"""

    instrument_id: str
    point_type: str
    frequency_min_hz: Decimal
    frequency_max_hz: Decimal
    response_min: Decimal
    response_max: Decimal
    noise_max: Decimal

    def field_range(self, field: str) -> tuple[Decimal, Decimal]:
        if field == "test_frequency_hz":
            return self.frequency_min_hz, self.frequency_max_hz
        if field == "response":
            return self.response_min, self.response_max
        if field == "noise":
            return Decimal(0), self.noise_max
        raise KeyError(field)


def parse_finite_number(value: object, field: str) -> Decimal:
    """把输入解析为有限十进制数，拒绝布尔、非数值字符串、NaN 与正负无穷。"""

    if isinstance(value, bool) or value is None:
        raise ContractViolation(field, "numeric", f"{field} 必须是数值，收到 {type(value).__name__}")
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ContractViolation(field, "numeric", f"{field} 必须是数值，收到空字符串")
    else:
        raise ContractViolation(field, "numeric", f"{field} 必须是数值，收到 {type(value).__name__}")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ContractViolation(field, "numeric", f"{field} 不是可解析的数值: {value!r}") from exc
    if not number.is_finite():
        raise ContractViolation(field, "finite", f"{field} 必须是有限数值，拒绝 NaN 与正负无穷")
    return number


def canonical_number(number: Decimal) -> str:
    """数值的规范文本形式，用于内容摘要与事件载荷。"""

    return format(number.normalize(), "f")


def check_range(number: Decimal, field: str, low: Decimal, high: Decimal) -> None:
    if number < low or number > high:
        raise ContractViolation(
            field,
            "range",
            f"{field}={canonical_number(number)} 超出设备量程 "
            f"[{canonical_number(low)}, {canonical_number(high)}]",
        )


def parse_observed_at(value: object, now: datetime, field: str = "measured_at") -> str:
    """校验观测时间并规范化为 UTC ISO-8601 文本。"""

    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(field, "timestamp_format", f"{field} 必须是带时区的 ISO-8601 时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractViolation(field, "timestamp_format", f"{field} 不是合法 ISO-8601 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContractViolation(field, "timestamp_format", f"{field} 必须显式携带时区")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now.astimezone(timezone.utc) + FUTURE_TOLERANCE:
        raise ContractViolation(field, "timestamp_not_future", f"{field} 晚于当前时间，超出 300 秒时钟容差")
    if parsed < MIN_OBSERVED_AT:
        raise ContractViolation(field, "timestamp_not_ancient", f"{field} 早于 2000-01-01，疑似设备时钟错误")
    return parsed.isoformat()


def parse_observation_key(value: object, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ContractViolation(
                "observation_key", "required", "observation_key 必须提供以支持重放一致性检查"
            )
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation("observation_key", "text", "observation_key 必须是非空字符串")
    key = value.strip()
    if len(key) > 128:
        raise ContractViolation("observation_key", "length", "observation_key 不能超过 128 个字符")
    return key


def observation_digest(parts: Mapping[str, Any]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidatedObservation:
    """通过契约、可以安全持久化的一次观测。"""

    observation_key: str | None
    instrument: str
    measured_at: str
    test_frequency_hz: Decimal
    response: Decimal
    noise: Decimal
    content_sha256: str


def validate_observation(
    raw: Mapping[str, Any],
    instrument: InstrumentRange,
    *,
    now: datetime,
    lot_id: str,
    require_key: bool = False,
) -> ValidatedObservation:
    """按测点类型与设备量程校验一条观测，全部通过后才允许持久化。"""

    if not isinstance(raw, Mapping):
        raise ContractViolation("observation", "mapping", "观测必须是对象")
    key = parse_observation_key(raw.get("observation_key"), required=require_key)
    frequency = parse_finite_number(raw.get("test_frequency_hz"), "test_frequency_hz")
    response = parse_finite_number(raw.get("response"), "response")
    noise = parse_finite_number(raw.get("noise", 0), "noise")
    for field, number in (
        ("test_frequency_hz", frequency),
        ("response", response),
        ("noise", noise),
    ):
        low, high = instrument.field_range(field)
        check_range(number, field, low, high)
    measured_at = raw.get("measured_at")
    if measured_at is None:
        measured = now.astimezone(timezone.utc).isoformat()
    else:
        measured = parse_observed_at(measured_at, now)
    digest = observation_digest({
        "lot_id": lot_id,
        "observation_key": key,
        "instrument": instrument.instrument_id,
        "measured_at": measured,
        "test_frequency_hz": canonical_number(frequency),
        "response": canonical_number(response),
        "noise": canonical_number(noise),
    })
    return ValidatedObservation(
        observation_key=key,
        instrument=instrument.instrument_id,
        measured_at=measured,
        test_frequency_hz=frequency,
        response=response,
        noise=noise,
        content_sha256=digest,
    )


def parse_instrument(raw: Mapping[str, Any]) -> InstrumentRange:
    """校验仪器注册请求：量程必须是有限数值且落在测点类型硬边界内。"""

    instrument_id = raw.get("instrument_id")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise ContractViolation("instrument_id", "text", "instrument_id 必须是非空字符串")
    point_type = raw.get("point_type")
    if point_type not in POINT_TYPES:
        raise ContractViolation(
            "point_type", "point_type_known", f"point_type 必须是 {sorted(POINT_TYPES)} 之一"
        )
    bounds = POINT_TYPES[point_type]
    frequency_min = parse_finite_number(raw.get("frequency_min_hz"), "frequency_min_hz")
    frequency_max = parse_finite_number(raw.get("frequency_max_hz"), "frequency_max_hz")
    response_min = parse_finite_number(raw.get("response_min"), "response_min")
    response_max = parse_finite_number(raw.get("response_max"), "response_max")
    noise_max = parse_finite_number(raw.get("noise_max"), "noise_max")
    if frequency_min <= 0:
        raise ContractViolation("frequency_min_hz", "positive", "频率量程下界必须大于零")
    if frequency_min >= frequency_max:
        raise ContractViolation("frequency_max_hz", "ordering", "频率量程上界必须大于下界")
    if response_min >= response_max:
        raise ContractViolation("response_max", "ordering", "响应量程上界必须大于下界")
    if noise_max < 0:
        raise ContractViolation("noise_max", "non_negative", "噪声上限不能为负")
    if frequency_max > bounds.frequency_max_hz:
        raise ContractViolation(
            "frequency_max_hz", "point_type_bounds", f"频率量程超出测点类型 {point_type} 的硬边界"
        )
    if response_min < bounds.response_min or response_max > bounds.response_max:
        raise ContractViolation(
            "response_min", "point_type_bounds", f"响应量程超出测点类型 {point_type} 的硬边界"
        )
    if noise_max > bounds.noise_max:
        raise ContractViolation(
            "noise_max", "point_type_bounds", f"噪声上限超出测点类型 {point_type} 的硬边界"
        )
    return InstrumentRange(
        instrument_id=instrument_id.strip(),
        point_type=point_type,
        frequency_min_hz=frequency_min,
        frequency_max_hz=frequency_max,
        response_min=response_min,
        response_max=response_max,
        noise_max=noise_max,
    )
