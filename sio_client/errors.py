"""sio_client の例外。"""

from __future__ import annotations

from typing import Any


class SIOException(Exception):
    """sio_client が送出する例外の基底。"""


class SIOConfigError(SIOException):
    """接続先や token を決められない（config.json がない等）。"""


class SIOConnectionError(SIOException):
    """SIO Service と通信できない。

    outcome_unknown が True のときは、編集などの要求が実行されたかどうか分からない。
    同じ操作を送り直す前に、音符を読み取り直して確認すること。
    """

    def __init__(self, message: str, *, request_id: str | None = None, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.outcome_unknown = outcome_unknown


class SIOError(SIOException):
    """SIO がエラーを返した。code で種類を判別する。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None, *,
                 retryable: bool = False, request_id: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.request_id = request_id


class BridgeUnavailable(SIOError):
    """REAPER（SIO Bridge）とつながっていない。要求は実行されていない。"""


class RequestPending(SIOError):
    """待機時間内に結果が出なかった。要求は取り消されておらず、実行中か実行待ちである。"""


class OutcomeUnknown(SIOError):
    """要求が実行されたかどうか分からない。読み取り直して確認すること。"""


class ProjectMismatch(SIOError):
    """対象の曲が lock_project で固定した曲と違う。何も実行されていない。"""


class NoteMismatch(SIOError):
    """指定した音符が、読み取ったときと変わっている。何も変更されていない。"""


class NoteNotFound(SIOError):
    """指定した音符がない。"""


class PartNotFound(SIOError):
    """指定した名前のパートがない。"""


class PartNotSelected(SIOError):
    """パートを省略したが、既定のパートが選択されていない。"""


ERROR_CLASSES: dict[str, type[SIOError]] = {
    "BRIDGE_UNAVAILABLE": BridgeUnavailable,
    "REQUEST_PENDING": RequestPending,
    "OUTCOME_UNKNOWN": OutcomeUnknown,
    "PROJECT_MISMATCH": ProjectMismatch,
    "NOTE_MISMATCH": NoteMismatch,
    "NOTE_NOT_FOUND": NoteNotFound,
    "PART_NOT_FOUND": PartNotFound,
    "PART_NOT_SELECTED": PartNotSelected,
}


def error_from(error: Any, request_id: str | None) -> SIOError:
    """応答の error オブジェクトから例外を作る。"""
    if not isinstance(error, dict):
        return SIOError("INTERNAL_ERROR", "SIO の応答に error がありません。", request_id=request_id)
    code = str(error.get("code", "INTERNAL_ERROR"))
    details = error.get("details")
    return ERROR_CLASSES.get(code, SIOError)(
        code, str(error.get("message", "")), details if isinstance(details, dict) else {},
        retryable=bool(error.get("retryable", False)), request_id=request_id)
