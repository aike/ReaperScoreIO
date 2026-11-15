"""プロトコル 2.3 の要求引数（params）の検証モデル。

未知の引数・不正な型・null による省略を拒否する。真偽値は整数として扱わない。
名前や音符の実在確認、小節・拍の解決は Bridge が実行時に行う。
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("number out of range") from None
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise ValueError("must be a canonical lowercase UUID string")
    return value


Number = Annotated[float, BeforeValidator(_finite_number)]
NonNegNumber = Annotated[float, BeforeValidator(_finite_number), Field(ge=0)]
PosNumber = Annotated[float, BeforeValidator(_finite_number), Field(gt=0)]
PosInt = Annotated[int, Field(strict=True, ge=1)]
Pitch = Annotated[int, Field(strict=True, ge=0, le=127)]
Velocity = Annotated[int, Field(strict=True, ge=1, le=127)]
Channel = Annotated[int, Field(strict=True, ge=1, le=16)]
Name = Annotated[str, Field(strict=True, min_length=1)]
UUIDStr = Annotated[str, BeforeValidator(_uuid)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def _has(self, name: str) -> bool:
        return name in self.model_fields_set


class Empty(Model):
    """引数なし（params は {}）。"""


class Position(Model):
    """{"qn": n} か {"bar": b, "beat": t, "offset_qn": o} のどちらか一方。"""

    qn: NonNegNumber = None  # type: ignore[assignment]
    bar: PosInt = None  # type: ignore[assignment]
    beat: PosInt = None  # type: ignore[assignment]
    offset_qn: NonNegNumber = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _one_form(self) -> "Position":
        if self._has("qn"):
            if self._has("bar") or self._has("beat") or self._has("offset_qn"):
                raise ValueError("position must be either {qn} or {bar, beat, offset_qn}, not both")
        elif not (self._has("bar") and self._has("beat")):
            raise ValueError("position requires qn, or both bar and beat")
        return self


class Range(Model):
    start: Position
    end: Position

    @model_validator(mode="after")
    def _ordered(self) -> "Range":
        if self.start._has("qn") and self.end._has("qn") and not self.end.qn > self.start.qn:
            raise ValueError("range end must be later than start")
        return self


class NoteExpect(Model):
    """target が解決した音符の照合条件。指定した属性だけを比較する。"""

    pitch: Pitch = None  # type: ignore[assignment]
    start_qn: NonNegNumber = None  # type: ignore[assignment]
    start: Position = None  # type: ignore[assignment]
    duration_qn: PosNumber = None  # type: ignore[assignment]
    velocity: Velocity = None  # type: ignore[assignment]
    channel: Channel = None  # type: ignore[assignment]
    muted: bool = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _not_empty(self) -> "NoteExpect":
        if not self.model_fields_set:
            raise ValueError("expect must contain at least one attribute")
        if self._has("start_qn") and self._has("start"):
            raise ValueError("start_qn and start cannot be combined")
        return self


class Target(Model):
    """{"note_id": n} か {"pitch": p, 開始位置[, "channel": c]} のどちらか一方。expect は任意。

    開始位置は start_qn（数値）か start（Position。小節・拍で書ける）のどちらか一方。
    """

    note_id: PosInt = None  # type: ignore[assignment]
    pitch: Pitch = None  # type: ignore[assignment]
    start_qn: NonNegNumber = None  # type: ignore[assignment]
    start: Position = None  # type: ignore[assignment]
    channel: Channel = None  # type: ignore[assignment]
    expect: NoteExpect = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _one_form(self) -> "Target":
        if self._has("note_id"):
            if self._has("pitch") or self._has("start_qn") or self._has("start") or self._has("channel"):
                raise ValueError("target must be either {note_id} or {pitch, start_qn|start, channel}, not both")
        elif not (self._has("pitch") and (self._has("start_qn") != self._has("start"))):
            raise ValueError("target requires note_id, or pitch with exactly one of start_qn and start")
        return self


class NewNote(Model):
    pitch: Pitch
    start_qn: NonNegNumber = None  # type: ignore[assignment]
    start: Position = None  # type: ignore[assignment]
    duration_qn: PosNumber
    velocity: Velocity
    channel: Channel
    muted: bool

    @model_validator(mode="after")
    def _one_start(self) -> "NewNote":
        if self._has("start_qn") == self._has("start"):
            raise ValueError("note requires exactly one of start_qn and start")
        return self


class NoteChanges(Model):
    pitch: Pitch = None  # type: ignore[assignment]
    start_qn: NonNegNumber = None  # type: ignore[assignment]
    start: Position = None  # type: ignore[assignment]
    duration_qn: PosNumber = None  # type: ignore[assignment]
    velocity: Velocity = None  # type: ignore[assignment]
    channel: Channel = None  # type: ignore[assignment]
    muted: bool = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _not_empty(self) -> "NoteChanges":
        if not self.model_fields_set:
            raise ValueError("set must contain at least one attribute")
        if self._has("start_qn") and self._has("start"):
            raise ValueError("start_qn and start cannot be combined")
        return self


class InsertOp(Model):
    op: Literal["insert"]
    client_note_id: Name
    part_name: Name = None  # type: ignore[assignment]
    note: NewNote


class UpdateOp(Model):
    op: Literal["update"]
    part_name: Name = None  # type: ignore[assignment]
    target: Target
    set: NoteChanges


class DeleteOp(Model):
    op: Literal["delete"]
    part_name: Name = None  # type: ignore[assignment]
    target: Target


Operation = Annotated[Union[InsertOp, UpdateOp, DeleteOp], Field(discriminator="op")]


class SelectProjectParams(Model):
    project_name: Name


class ProjectScoped(Model):
    """対象の曲（アクティブなタブの曲）を使う要求。expect_project_name は取り違え検出用の任意の照合。"""

    expect_project_name: Name = None  # type: ignore[assignment]


class GetScoreInfoParams(ProjectScoped):
    pass


class SelectPartParams(ProjectScoped):
    part_name: Name


class AddTrackParams(ProjectScoped):
    """トラック（パート）の追加。instrument は音源プラグイン名（REAPER の FX 検索にそのまま渡す）。"""

    part_name: Name
    instrument: Name = None  # type: ignore[assignment]


class AddItemParams(ProjectScoped):
    """空の MIDI アイテムの追加。part_name を省略すると選択中のパートを使う。"""

    part_name: Name = None  # type: ignore[assignment]
    range: Range


class GetNotesParams(ProjectScoped):
    part_names: Annotated[list[Name], Field(min_length=1)] = None  # type: ignore[assignment]
    selected_part: bool = False
    range: Range = None  # type: ignore[assignment]
    at: Position = None  # type: ignore[assignment]
    relation: Literal["starts_in", "overlaps", "sounding_at"] = "overlaps"
    include_muted: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "GetNotesParams":
        if self._has("part_names"):
            if len(set(self.part_names)) != len(self.part_names):
                raise ValueError("part_names must not contain duplicates")
            if self.selected_part:
                raise ValueError("selected_part:true cannot be combined with part_names")
        if self._has("range") and self._has("at"):
            raise ValueError("range and at cannot be combined")
        if self.relation == "sounding_at":
            if not self._has("at"):
                raise ValueError("sounding_at requires at")
        elif self._has("at"):
            raise ValueError("at is only allowed with relation sounding_at")
        return self


class GetRequestStatusParams(Model):
    target_request_id: UUIDStr


class NoteTargetEntry(Model):
    part_name: Name = None  # type: ignore[assignment]
    target: Target


class SelectNotesParams(ProjectScoped):
    targets: list[NoteTargetEntry]


class ApplyPatchParams(ProjectScoped):
    operations: Annotated[list[Operation], Field(min_length=1)]
    label: Annotated[str, Field(strict=True)] = "SIO: Edit notes"
    dry_run: bool = False

    @model_validator(mode="after")
    def _unique_client_ids(self) -> "ApplyPatchParams":
        seen: set[str] = set()
        for op in self.operations:
            if isinstance(op, InsertOp):
                if op.client_note_id in seen:
                    raise ValueError(f"duplicate client_note_id: {op.client_note_id!r}")
                seen.add(op.client_note_id)
        return self


class PlayRangeParams(ProjectScoped):
    range: Range


class Envelope(Model):
    """RPC 要求の外枠。4 項目すべて必須。"""

    protocol_version: Annotated[str, Field(strict=True)]
    request_id: UUIDStr
    method: Annotated[str, Field(strict=True)]
    params: dict[str, Any]


# method -> (params モデル, Bridge へ配送するか)
METHODS: dict[str, tuple[type[Model], bool]] = {
    "get_status": (Empty, False),
    "list_projects": (Empty, True),
    "select_project": (SelectProjectParams, True),
    "select_part": (SelectPartParams, True),
    "create_project": (Empty, True),
    "add_track": (AddTrackParams, True),
    "add_item": (AddItemParams, True),
    "get_score_info": (GetScoreInfoParams, True),
    "get_notes": (GetNotesParams, True),
    "get_request_status": (GetRequestStatusParams, False),
    "select_notes": (SelectNotesParams, True),
    "apply_patch": (ApplyPatchParams, True),
    "play_range": (PlayRangeParams, True),
    "stop": (Empty, True),
}


def error_details(exc: ValidationError) -> dict[str, Any]:
    """検証エラーを details 用の JSON 化可能な形にする（入力値そのものは含めない）。"""
    errors = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False)[:20]:
        errors.append(
            {
                "path": ".".join(str(part) for part in err["loc"]),
                "reason": err["msg"],
            }
        )
    return {"errors": errors}
