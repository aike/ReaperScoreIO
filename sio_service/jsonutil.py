"""厳格な JSON 解析（重複キー・NaN/Infinity を拒否）と正規化。"""

from __future__ import annotations

import json
import math
from typing import Any


class StrictJSONError(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise StrictJSONError(f"non-finite number is not allowed: {name}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError(f"duplicate JSON key: {key!r}")
        obj[key] = value
    return obj


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise StrictJSONError(f"number out of range: {text[:32]}")
    return value


def strict_loads(data: bytes) -> Any:
    """UTF-8 の JSON を解析する。不正なら StrictJSONError。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise StrictJSONError("body is not valid UTF-8") from e
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except StrictJSONError:
        raise
    except (ValueError, RecursionError) as e:
        raise StrictJSONError(f"invalid JSON: {e}") from e


def canonical(value: Any) -> str:
    """キー順・空白に依存しない正規表現。配列順と値の表記型（1 と 1.0）は区別する。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
