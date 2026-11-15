"""戻り値の型と、引数（音高・位置・範囲）の変換。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Union

# 位置: 数値（QN）、(小節, 拍)、(小節, 拍, QN のずれ)、またはプロトコルの Position の dict
PositionLike = Union[int, float, tuple, list, dict]
# 音高: MIDI ノート番号、または "C4"（＝60）のような音名
PitchLike = Union[int, str]

_PITCH_RE = re.compile(r"^([A-Ga-g])([#b]?)(-1|[0-9])$")
_PITCH_BASE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def pitch_number(value: PitchLike) -> int:
    """音高を MIDI ノート番号にする。音名は中央 C を C4＝60 とする。"""
    if _is_int(value):
        number = value
    elif isinstance(value, str):
        m = _PITCH_RE.match(value.strip())
        if not m:
            raise ValueError(f"音名として解釈できません: {value!r}（例: C4、F#3、Bb2）")
        letter, accidental, octave = m.groups()
        number = (int(octave) + 1) * 12 + _PITCH_BASE[letter.upper()] + {"#": 1, "b": -1, "": 0}[accidental]
    else:
        raise TypeError(f"音高は整数か音名の文字列で指定してください: {value!r}")
    if not 0 <= number <= 127:
        raise ValueError(f"音高は 0〜127 の範囲で指定してください: {value!r}")
    return number


def pitch_name(number: int) -> str:
    """MIDI ノート番号を音名にする（半音は # で表す。C4＝60）。"""
    return f"{_PITCH_NAMES[number % 12]}{number // 12 - 1}"


def position_json(value: PositionLike, what: str = "位置") -> dict[str, Any]:
    """位置をプロトコルの Position にする。"""
    if _is_number(value):
        return {"qn": value}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (tuple, list)) and len(value) in (2, 3):
        bar, beat = value[0], value[1]
        if not (_is_int(bar) and _is_int(beat)):
            raise TypeError(f"{what}の小節と拍は整数で指定してください: {value!r}")
        pos: dict[str, Any] = {"bar": bar, "beat": beat}
        if len(value) == 3:
            if not _is_number(value[2]):
                raise TypeError(f"{what}の 3 番目（QN のずれ）は数値で指定してください: {value!r}")
            pos["offset_qn"] = value[2]
        return pos
    raise TypeError(f"{what}は、数値（四分音符単位）か (小節, 拍) で指定してください: {value!r}")


def start_fields(value: PositionLike, what: str = "開始位置") -> dict[str, Any]:
    """開始位置を、start_qn（数値）か start（小節・拍）のどちらかの項目にする。"""
    pos = position_json(value, what)
    if set(pos) == {"qn"}:
        return {"start_qn": pos["qn"]}
    return {"start": pos}


def range_json(bars: Any = None, range: Any = None) -> dict[str, Any] | None:  # noqa: A002
    """bars（両端を含む小節番号）または range（開始, 終了）を、プロトコルの Range にする。"""
    if bars is not None and range is not None:
        raise ValueError("bars と range は同時に指定できません。")
    if bars is not None:
        if _is_int(bars):
            first, last = bars, bars
        elif isinstance(bars, (tuple, list)) and len(bars) == 2 and _is_int(bars[0]) and _is_int(bars[1]):
            first, last = bars
        else:
            raise TypeError(f"bars は小節番号か (最初の小節, 最後の小節) で指定してください: {bars!r}")
        if first < 1 or last < first:
            raise ValueError(f"bars は 1 以上で、最初 ≦ 最後 となるように指定してください: {bars!r}")
        return {"start": {"bar": first, "beat": 1}, "end": {"bar": last + 1, "beat": 1}}
    if range is not None:
        if not (isinstance(range, (tuple, list)) and len(range) == 2):
            raise TypeError(f"range は (開始, 終了) で指定してください: {range!r}")
        return {"start": position_json(range[0], "range の開始"), "end": position_json(range[1], "range の終了")}
    return None


@dataclass(frozen=True)
class Note:
    """音符 1 つ。位置と長さは四分音符を 1 とする数値。"""

    id: int                 # パート内の一時的な番号（note_id）
    part: str               # パート名
    pitch: int              # MIDI ノート番号
    start: float            # 開始位置
    duration: float         # 長さ
    bar: int                # 開始位置の小節
    beat: int               # 開始位置の拍
    offset: float           # 拍の頭からのずれ
    velocity: int
    channel: int
    muted: bool             # 音符自体のミュート
    item_muted: bool
    track_muted: bool
    selected: bool          # REAPER 上で選択されているか
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def pitch_name(self) -> str:
        return pitch_name(self.pitch)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Note":
        pos = d.get("position") or {}
        return cls(
            id=d["note_id"], part=d["part_name"], pitch=d["pitch"], start=d["start_qn"],
            duration=d["duration_qn"], bar=pos.get("bar", 0), beat=pos.get("beat", 0),
            offset=pos.get("offset_qn", 0.0), velocity=d.get("velocity", 0), channel=d.get("channel", 1),
            muted=d.get("muted", False), item_muted=d.get("item_muted", False),
            track_muted=d.get("track_muted", False), selected=d.get("selected", False), raw=d)


@dataclass(frozen=True)
class Meter:
    """拍子。start の位置（bar 小節の頭）から有効。"""

    start: float
    bar: int
    numerator: int
    denominator: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Meter":
        return cls(d["start_qn"], d["bar"], d["numerator"], d["denominator"])


@dataclass(frozen=True)
class Diagnostic:
    """警告や制約。code で種類を判別する。"""

    code: str
    message: str
    details: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Diagnostic":
        return cls(d.get("code", ""), d.get("message", ""), d.get("details") or {})


def _diagnostics(items: Any) -> list[Diagnostic]:
    return [Diagnostic.from_json(d) for d in (items or [])]


@dataclass(frozen=True)
class Part:
    name: str
    order: int                      # トラック番号（1 始まり）
    addressable: bool = True        # 名前で指定できるか（同名のパートがあると False）
    selected: bool = False          # 既定のパートか


@dataclass(frozen=True)
class Project:
    name: str                       # 未保存の曲は ""
    active: bool                    # REAPER で表に出ているか（＝対象の曲か）
    addressable: bool               # select_project で指定できるか


@dataclass(frozen=True)
class Selection:
    """対象の曲と、その曲の既定のパート。"""

    project: str
    part: str | None


@dataclass(frozen=True)
class Status:
    connected: bool                 # REAPER（Bridge）とつながっているか
    bridge_state: str               # connected / unavailable / unresponsive
    project: str | None             # 対象の曲（表のタブの曲）の名前
    selected_part: str | None
    playback_active: bool           # SIO が始めた再生が続いているか
    service_version: str | None
    bridge_version: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ScoreInfo:
    project: str
    selected_part: str | None
    parts: list[Part]
    meters: list[Meter]
    note_range: tuple[float, float] | None      # 音符のある範囲（開始, 終了）
    note_range_complete: bool
    constraints: list[Diagnostic]
    warnings: list[Diagnostic]
    raw: dict[str, Any] = field(repr=False, compare=False)


class NoteList(list):
    """notes() の結果。音符のリストに、取得時の情報を添えたもの。"""

    project: str = ""
    parts: list[Part]
    meters: list[Meter]
    range: tuple[float, float] | None = None
    warnings: list[Diagnostic]
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "NoteList":
        out = cls(Note.from_json(n) for n in d.get("notes", []))
        out.project = d.get("project_name", "")
        out.parts = [Part(p["part_name"], p["order"]) for p in d.get("parts", [])]
        out.meters = [Meter.from_json(m) for m in d.get("meters", [])]
        rng = d.get("range")
        out.range = (rng["start_qn"], rng["end_qn"]) if rng else None
        out.warnings = _diagnostics(d.get("warnings"))
        out.raw = d
        return out


@dataclass(frozen=True)
class Track:
    """add_track の結果。"""

    project: str
    name: str
    order: int
    instrument_loaded: bool         # 音源を載せられたか
    instrument_name: str | None     # 載せた音源の名前
    warnings: list[Diagnostic]
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Inserted:
    """追加した音符の、編集後の番号。"""

    part: str
    id: int
    client_id: str
    operation_index: int


@dataclass(frozen=True)
class PatchResult:
    """編集の結果。"""

    project: str
    added: int
    updated: int
    deleted: int
    dry_run: bool
    resolved: list[Note]            # update / delete が対象にした音符（変更前の内容）
    inserted: list[Inserted]        # 追加した音符（dry_run では空）
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "PatchResult":
        return cls(
            project=d.get("project_name", ""), added=d.get("added", 0), updated=d.get("updated", 0),
            deleted=d.get("deleted", 0), dry_run=d.get("dry_run", False),
            resolved=[Note.from_json(r["note"]) for r in d.get("resolved", [])],
            inserted=[Inserted(i["part_name"], i["note_id"], i["client_note_id"], i["operation_index"])
                      for i in d.get("inserted", [])],
            raw=d)
