"""SIO クラス: SIO Service への接続と、各要求のメソッド。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import OutcomeUnknown, RequestPending, SIOConfigError, SIOConnectionError, error_from
from .types import (
    Meter,
    Note,
    NoteList,
    Part,
    PatchResult,
    PitchLike,
    PositionLike,
    Project,
    ScoreInfo,
    Selection,
    Status,
    Track,
    _diagnostics,
    _is_int,
    _is_number,
    pitch_number,
    position_json,
    range_json,
    start_fields,
)

PROTOCOL_VERSION = "2.3"

# expect_project_name を受け付ける method（lock_project の対象）
PROJECT_SCOPED = frozenset({"select_part", "add_track", "add_item", "get_score_info", "get_notes",
                            "select_notes", "apply_patch", "play_range"})
# 結果が不明のときに、自動では送り直さない method（REAPER の状態を変えるもの）
MUTATING = frozenset({"select_project", "select_part", "create_project", "add_track", "add_item",
                      "select_notes", "apply_patch", "play_range", "stop"})

# (本文, ヘッダー, タイムアウト秒) -> (HTTP ステータス, 本文)。
# 送信前に接続を拒否されたら ConnectionRefusedError、それ以外の通信失敗は OSError を送出する。
Transport = Callable[[bytes, dict, float], tuple]


def default_resource_path() -> Path:
    """OS 標準の REAPER リソースフォルダ。"""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "REAPER"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "REAPER"


def _http_transport(url: str) -> Transport:
    def send(body: bytes, headers: dict, timeout: float) -> tuple:
        request = urllib.request.Request(url + "/v2/rpc", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                raise e.reason from None
            raise OSError(str(e.reason)) from None
    return send


class Patch:
    """複数の編集を 1 回の apply_patch（REAPER の「元に戻す」1 回分）にまとめる。

    with sio.patch() as p: の形で使う。ブロックを正常に抜けたときに送信し、結果を p.result に入れる。
    ブロックの中で例外が起きた場合は、何も送信しない。
    """

    def __init__(self, sio: "SIO", label: str | None, dry_run: bool) -> None:
        self._sio = sio
        self.label = label
        self.dry_run = dry_run
        self.operations: list[dict[str, Any]] = []
        self.result: PatchResult | None = None

    def insert(self, part: str | None, pitch: PitchLike, start: PositionLike, duration: float = 1.0, *,
               velocity: int = 80, channel: int = 1, muted: bool = False) -> None:
        """音符を追加する。part が None なら既定のパートに追加する。"""
        if not _is_number(duration):
            raise TypeError(f"duration は数値で指定してください: {duration!r}")
        op: dict[str, Any] = {"op": "insert", "client_note_id": f"n{len(self.operations) + 1}"}
        if part is not None:
            op["part_name"] = part
        note: dict[str, Any] = {"pitch": pitch_number(pitch)}
        note.update(start_fields(start))
        note.update({"duration_qn": duration, "velocity": velocity, "channel": channel, "muted": muted})
        op["note"] = note
        self.operations.append(op)

    def update(self, note: Any, *, pitch: PitchLike | None = None, start: PositionLike | None = None,
               duration: float | None = None, velocity: int | None = None, channel: int | None = None,
               muted: bool | None = None, part: str | None = None) -> None:
        """音符の属性を変更する。指定した属性だけが変わる。"""
        changes: dict[str, Any] = {}
        if pitch is not None:
            changes["pitch"] = pitch_number(pitch)
        if start is not None:
            changes.update(start_fields(start))
        if duration is not None:
            changes["duration_qn"] = duration
        if velocity is not None:
            changes["velocity"] = velocity
        if channel is not None:
            changes["channel"] = channel
        if muted is not None:
            changes["muted"] = muted
        if not changes:
            raise ValueError("update には変更する属性を 1 つ以上指定してください"
                             "（pitch, start, duration, velocity, channel, muted）。")
        self.operations.append({"op": "update", **target_entry(note, part), "set": changes})

    def delete(self, note: Any, *, part: str | None = None) -> None:
        """音符を削除する。"""
        self.operations.append({"op": "delete", **target_entry(note, part)})

    def send(self) -> PatchResult | None:
        """溜めた編集を送信する。編集がなければ何も送らず None を返す。"""
        if not self.operations:
            return None
        params: dict[str, Any] = {"operations": self.operations, "dry_run": self.dry_run}
        if self.label is not None:
            params["label"] = self.label
        self.result = PatchResult.from_json(self._sio._rpc("apply_patch", params))
        return self.result

    def __enter__(self) -> "Patch":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.send()
        return False


def target_entry(target: Any, part: str | None = None) -> dict[str, Any]:
    """音符の指定を、プロトコルの {part_name, target} にする。

    Note     読み取った音符。番号に加えて、読み取ったときの音高・開始位置・長さを照合条件にする。
    int      note_id
    tuple    (音高, 開始位置) または (音高, 開始位置, チャンネル)
    dict     プロトコルの target をそのまま
    """
    entry: dict[str, Any] = {}
    if isinstance(target, Note):
        entry["part_name"] = part if part is not None else target.part
        entry["target"] = {"note_id": target.id, "expect": {
            "pitch": target.pitch, "start_qn": target.start, "duration_qn": target.duration}}
        return entry
    if part is not None:
        entry["part_name"] = part
    if _is_int(target):
        entry["target"] = {"note_id": target}
    elif isinstance(target, tuple) and len(target) in (2, 3):
        tgt: dict[str, Any] = {"pitch": pitch_number(target[0])}
        tgt.update(start_fields(target[1]))
        if len(target) == 3:
            tgt["channel"] = target[2]
        entry["target"] = tgt
    elif isinstance(target, dict):
        entry["target"] = dict(target)
    else:
        raise TypeError("音符は、読み取った Note、note_id（整数）、(音高, 開始位置) のいずれかで指定してください: "
                        f"{target!r}")
    return entry


class SIO:
    """SIO Service への接続。

    引数をすべて省略すると、REAPER のリソースフォルダにある SIO/config.json から接続先と token を読む。

    resource_path    REAPER のリソースフォルダ（ポータブル導入のとき等）
    url, token       接続先と token を直接指定する
    timeout          1 回の HTTP 通信の待ち時間（秒）
    pending_timeout  処理が長引いたときに、結果を待ち続ける時間（秒）
    """

    def __init__(self, resource_path: str | os.PathLike | None = None, *, url: str | None = None,
                 token: str | None = None, timeout: float = 60.0, pending_timeout: float = 120.0,
                 transport: Transport | None = None) -> None:
        token = token if token is not None else os.environ.get("SIO_TOKEN")
        if url is None or token is None:
            resource = Path(resource_path or os.environ.get("SIO_REAPER_RESOURCE_PATH") or default_resource_path())
            config_path = resource / "SIO" / "config.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise SIOConfigError(
                    f"SIO の設定を読めません（{config_path}）。SIO Service を一度起動してください。: {e}") from None
            url = url or f"http://127.0.0.1:{config.get('port', 18741)}"
            token = token if token is not None else str(config.get("token", ""))
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.pending_timeout = pending_timeout
        self._transport = transport or _http_transport(self.url)
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic
        self.locked_project: str | None = None
        self.last_request_id: str | None = None

    def __repr__(self) -> str:
        return f"SIO({self.url!r})"

    # ------------------------------------------------------------ 通信

    def new_envelope(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """要求の外枠（protocol_version、新しい request_id、method、params）を作る。"""
        return {"protocol_version": PROTOCOL_VERSION, "request_id": str(uuid.uuid4()),
                "method": method, "params": params if params is not None else {}}

    def exchange(self, envelope: dict[str, Any]) -> tuple:
        """要求の外枠を 1 回送り、(HTTP ステータス, 応答の本文 bytes) をそのまま返す。

        待機、結果の問い合わせ、エラーの例外化を行わない低水準の送受信（sio_cli などの道具のため）。
        同じ外枠を渡せば、同じ request_id で再送できる。
        通信の失敗は OSError（送信前に接続を拒否された場合は ConnectionRefusedError）のまま送出する。
        """
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        return self._transport(body, headers, self.timeout)

    def _post(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """要求を 1 回送り、応答の外枠を返す。通信の失敗は ConnectionRefusedError / OSError のまま。"""
        status, raw = self.exchange(envelope)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            doc = None
        if not isinstance(doc, dict) or not isinstance(doc.get("ok"), bool):
            raise SIOConnectionError(f"SIO Service（{self.url}）から想定外の応答がありました（HTTP {status}）。",
                                     request_id=envelope["request_id"])
        return doc

    def _request_state(self, target_id: str) -> dict[str, Any]:
        try:
            doc = self._post(self.new_envelope("get_request_status", {"target_request_id": target_id}))
        except OSError as e:
            raise SIOConnectionError(f"SIO Service（{self.url}）に要求の状態を問い合わせられません: {e}",
                                     request_id=target_id) from None
        if not doc["ok"]:
            raise error_from(doc.get("error"), target_id)
        return doc["result"]

    def _wait(self, target_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """結果が出るまで get_request_status で照会する。要求を送り直すことはしない。"""
        deadline = self._clock() + self.pending_timeout
        delay = 0.25
        while True:
            if state is None:
                state = self._request_state(target_id)
            if state.get("state") in ("succeeded", "failed") and isinstance(state.get("response"), dict):
                return state["response"]
            if state.get("state") not in ("queued", "running"):
                raise OutcomeUnknown("OUTCOME_UNKNOWN", "要求の結果を確認できません。読み取り直して確認してください。",
                                     {"reason": f"request state is {state.get('state')}"}, request_id=target_id)
            if self._clock() >= deadline:
                raise RequestPending("REQUEST_PENDING",
                                     f"{self.pending_timeout:g} 秒待っても結果が出ません。要求は取り消されていません。",
                                     {"target_request_id": target_id}, request_id=target_id)
            self._sleep(delay)
            delay = min(1.0, delay * 1.5)
            state = None

    def _recover(self, envelope: dict[str, Any], cause: Exception) -> dict[str, Any]:
        """送信後に通信が切れた要求の結果を、request_id で問い合わせる。"""
        request_id, mutating = envelope["request_id"], envelope["method"] in MUTATING
        try:
            state = self._request_state(request_id)
            if state.get("state") == "unknown" and not mutating:
                return self._post(envelope)             # 読み取りは、同じ request_id で 1 回だけ送り直す
        except (OSError, SIOConnectionError):
            state = None
        if state is None or state.get("state") == "unknown":
            raise SIOConnectionError(
                f"SIO Service（{self.url}）との通信が切れました: {cause}"
                + ("。要求が実行されたかどうか分かりません。読み取り直して確認してください。" if mutating else ""),
                request_id=request_id, outcome_unknown=mutating) from None
        return self._wait(request_id, state)

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, scoped: bool = True) -> dict[str, Any]:
        """要求を送り、result を返す。エラーは例外にする。待機と結果の問い合わせもここで行う。"""
        params = dict(params or {})
        if scoped and self.locked_project is not None and method in PROJECT_SCOPED:
            params = {"expect_project_name": self.locked_project, **params}
        envelope = self.new_envelope(method, params)
        self.last_request_id = envelope["request_id"]
        try:
            doc = self._post(envelope)
        except ConnectionRefusedError:
            raise SIOConnectionError(
                f"SIO Service（{self.url}）に接続できません。SIO Service が起動しているか確認してください。",
                request_id=envelope["request_id"]) from None
        except OSError as e:
            doc = self._recover(envelope, e)
        if not doc["ok"]:
            error = doc.get("error") if isinstance(doc.get("error"), dict) else {}
            if error.get("code") == "REQUEST_PENDING" and method != "get_request_status":
                doc = self._wait(error.get("details", {}).get("target_request_id") or envelope["request_id"])
        if not doc["ok"]:
            raise error_from(doc.get("error"), envelope["request_id"])
        return doc["result"]

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """任意の要求をそのまま送り、result（辞書）を返す。lock_project の照合は付けない。"""
        return self._rpc(method, params, scoped=False)

    # ------------------------------------------------------------ 状態と曲

    def status(self) -> Status:
        """接続状態。REAPER とつながっていなくても例外にはならない（connected が False になる）。"""
        r = self._rpc("get_status")
        return Status(
            connected=r.get("bridge_state") == "connected", bridge_state=r.get("bridge_state", ""),
            project=r.get("active_project_name"), selected_part=r.get("selected_part_name"),
            playback_active=bool(r.get("playback_active")), service_version=r.get("service_version"),
            bridge_version=r.get("bridge_version"), raw=r)

    def projects(self) -> list[Project]:
        """開いている曲の一覧。"""
        return [Project(p["project_name"], p["active"], p["addressable"])
                for p in self._rpc("list_projects")["projects"]]

    def _selection(self, r: dict[str, Any]) -> Selection:
        return Selection(r["project_name"], r.get("selected_part_name"))

    def select_project(self, name: str) -> Selection:
        """その名前の曲のタブを表に出し、対象の曲にする。lock_project 中は、固定する曲もこの曲に替える。"""
        selection = self._selection(self._rpc("select_project", {"project_name": name}))
        if self.locked_project is not None:
            self.locked_project = selection.project
        return selection

    def create_project(self) -> Selection:
        """新しい曲（未保存）を作る。新しい曲が対象になる。名前がないので lock_project は解除する。"""
        selection = self._selection(self._rpc("create_project"))
        self.locked_project = None
        return selection

    def lock_project(self, name: str | None = None) -> str:
        """対象の曲を固定する。以後、対象の曲がこの名前でなければ ProjectMismatch になり、何も実行されない。

        name を省略すると、いま表に出ている曲に固定する。名前のない（未保存の）曲は固定できない。
        """
        if name is None:
            name = next((p.name for p in self.projects() if p.active), "")
        if not name:
            raise ValueError("名前のない（未保存の）曲は固定できません。REAPER で保存してから呼んでください。")
        self.locked_project = name
        return name

    def unlock_project(self) -> None:
        self.locked_project = None

    def score_info(self) -> ScoreInfo:
        """対象の曲のパート、拍子、音符のある範囲、制約。"""
        r = self._rpc("get_score_info")
        rng = r.get("note_range")
        return ScoreInfo(
            project=r["project_name"], selected_part=r.get("selected_part_name"),
            parts=[Part(p["part_name"], p["order"], p.get("addressable", True), p.get("selected", False))
                   for p in r.get("parts", [])],
            meters=[Meter.from_json(m) for m in r.get("meters", [])],
            note_range=(rng["start_qn"], rng["end_qn"]) if rng else None,
            note_range_complete=bool(r.get("note_range_complete")),
            constraints=_diagnostics(r.get("constraints")), warnings=_diagnostics(r.get("warnings")), raw=r)

    # ------------------------------------------------------------ パート

    def select_part(self, name: str) -> Selection:
        """既定のパートを選択する。REAPER でもそのトラックだけが選択される。"""
        return self._selection(self._rpc("select_part", {"part_name": name}))

    def add_track(self, name: str, instrument: str | None = None) -> Track:
        """トラック（パート）を追加し、既定のパートにする。

        instrument の音源が見つからなくても例外にはならない。結果の instrument_loaded で確認する。
        """
        params: dict[str, Any] = {"part_name": name}
        if instrument is not None:
            params["instrument"] = instrument
        r = self._rpc("add_track", params)
        return Track(project=r["project_name"], name=r["part_name"], order=r["order"],
                     instrument_loaded=bool(r.get("instrument_loaded")), instrument_name=r.get("instrument_name"),
                     warnings=_diagnostics(r.get("warnings")), raw=r)

    def add_item(self, part: str | None = None, *, bars: Any = None, range: Any = None) -> tuple:  # noqa: A002
        """音符を入力できる範囲（空の MIDI アイテム）を作り、作った範囲 (開始, 終了) を返す。

        part を省略すると既定のパートに作る。bars=(1, 8) は第 1〜8 小節。
        """
        rng = range_json(bars, range)
        if rng is None:
            raise ValueError("add_item には bars か range を指定してください。")
        params: dict[str, Any] = {"range": rng}
        if part is not None:
            params["part_name"] = part
        r = self._rpc("add_item", params)
        return r["range"]["start_qn"], r["range"]["end_qn"]

    # ------------------------------------------------------------ 読み取り・選択・再生

    def notes(self, *parts: str, selected_part: bool = False, bars: Any = None, range: Any = None,  # noqa: A002
              at: PositionLike | None = None, starts_in: bool = False, include_muted: bool = False) -> NoteList:
        """音符を読み取る。パート名を省略すると全パート。

        bars=(1, 2)     第 1〜2 小節にかかる音符
        range=(0, 8)    開始〜終了（終了は含まない）にかかる音符
        at=(4, 2)       第 4 小節第 2 拍の時点で鳴っている音符
        starts_in=True  範囲にかかる音符ではなく、範囲の中で始まる音符だけ
        """
        params: dict[str, Any] = {}
        if parts:
            params["part_names"] = list(parts)
        if selected_part:
            params["selected_part"] = True
        rng = range_json(bars, range)
        if at is not None:
            if rng is not None or starts_in:
                raise ValueError("at は bars、range、starts_in と同時に指定できません。")
            params["at"] = position_json(at, "at")
            params["relation"] = "sounding_at"
        else:
            if rng is not None:
                params["range"] = rng
            params["relation"] = "starts_in" if starts_in else "overlaps"
        params["include_muted"] = include_muted
        return NoteList.from_json(self._rpc("get_notes", params))

    def select_notes(self, notes: Any = None, *, part: str | None = None) -> int:
        """REAPER 上で、指定した音符だけを選択状態にする。空なら選択を解除する。選択した数を返す。

        notes は音符のリスト。音符 1 つ（Note、note_id、(音高, 開始位置) のタプル）だけでもよい。
        """
        if notes is None:
            notes = []
        elif isinstance(notes, (Note, tuple, dict)) or _is_int(notes):
            notes = [notes]
        targets = [target_entry(n, part) for n in notes]
        return self._rpc("select_notes", {"targets": targets})["selected_count"]

    def play(self, *, bars: Any = None, range: Any = None, wait: bool = False,  # noqa: A002
             timeout: float | None = None) -> None:
        """範囲を 1 回再生する。wait=True なら再生が終わる（または止められる）まで待つ。"""
        rng = range_json(bars, range)
        if rng is None:
            raise ValueError("play には bars か range を指定してください。")
        self._rpc("play_range", {"range": rng})
        if wait:
            self.wait_playback(timeout)

    def wait_playback(self, timeout: float | None = None) -> None:
        """SIO が始めた再生が終わるまで待つ。timeout 秒を過ぎたら TimeoutError。"""
        deadline = None if timeout is None else self._clock() + timeout
        while self.status().playback_active:
            if deadline is not None and self._clock() >= deadline:
                raise TimeoutError("再生が終わりません。")
            self._sleep(0.1)

    def stop(self) -> bool:
        """SIO が始めた再生を止める。止めたら True。"""
        return bool(self._rpc("stop")["stopped"])

    # ------------------------------------------------------------ 編集

    def patch(self, label: str | None = None, dry_run: bool = False) -> Patch:
        """複数の編集を 1 回にまとめる。with sio.patch() as p: の形で使う。"""
        return Patch(self, label, dry_run)

    def insert(self, part: str | None, pitch: PitchLike, start: PositionLike, duration: float = 1.0, *,
               velocity: int = 80, channel: int = 1, muted: bool = False, label: str | None = None,
               dry_run: bool = False) -> PatchResult:
        """音符を 1 つ追加する。part が None なら既定のパートに追加する。"""
        p = Patch(self, label, dry_run)
        p.insert(part, pitch, start, duration, velocity=velocity, channel=channel, muted=muted)
        return p.send()  # type: ignore[return-value]

    def update(self, note: Any, *, pitch: PitchLike | None = None, start: PositionLike | None = None,
               duration: float | None = None, velocity: int | None = None, channel: int | None = None,
               muted: bool | None = None, part: str | None = None, label: str | None = None,
               dry_run: bool = False) -> PatchResult:
        """音符 1 つの属性を変更する。"""
        p = Patch(self, label, dry_run)
        p.update(note, pitch=pitch, start=start, duration=duration, velocity=velocity, channel=channel,
                 muted=muted, part=part)
        return p.send()  # type: ignore[return-value]

    def delete(self, note: Any, *, part: str | None = None, label: str | None = None,
               dry_run: bool = False) -> PatchResult:
        """音符を 1 つ削除する。"""
        p = Patch(self, label, dry_run)
        p.delete(note, part=part)
        return p.send()  # type: ignore[return-value]
