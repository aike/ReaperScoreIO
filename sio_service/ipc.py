"""Bridge とのファイル IPC（<REAPERリソース>/SIO/ipc/）。

  service.json              Service が書く。起動世代と開始時刻。
  bridge.json               Bridge が書く。起動世代、heartbeat、実行中要求、選択状態。
  requests/<id>.json        Service が書く。Bridge は claimed/ へ rename して所有権を得る。
  responses/<id>.json       Bridge が書く。

一時ファイルを close した後、同じディレクトリ内の rename で公開する。
要求の取消（rename で requests/ から外す）と Bridge の claim は排他であり、
取消に成功した要求は決して実行されない。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SERVICE_VERSION


@dataclass
class BridgeStatus:
    state: str  # connected / unavailable / unresponsive
    version: str | None = None
    instance_id: str | None = None
    running_request_id: str | None = None
    service_instance_id: str | None = None
    active_project_name: str | None = None
    selected_part_name: str | None = None
    playback_active: bool = False


class ServiceLock:
    """Service の多重起動を OS のファイルロックで防ぐ。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


class BridgeLink:
    def __init__(self, ipc_dir: Path, service_instance_id: str, heartbeat_stale: float = 3.0) -> None:
        self.ipc_dir = ipc_dir
        self.requests = ipc_dir / "requests"
        self.claimed = ipc_dir / "claimed"
        self.responses = ipc_dir / "responses"
        self.cancelled = ipc_dir / "cancelled"
        self.service_instance_id = service_instance_id
        self.heartbeat_stale = heartbeat_stale
        self._last: tuple[float, dict[str, Any], float] | None = None  # (読取時刻, 内容, mtime)

    # ---- 起動・掃除

    def start(self) -> None:
        for d in (self.requests, self.claimed, self.responses, self.cancelled):
            d.mkdir(parents=True, exist_ok=True)
        # 旧世代の未実行要求を排除する（claim 済みのものには触れない）。
        for path in self.requests.iterdir():
            self._unlink(path)
        self.sweep(24 * 3600)
        self._write_atomic(
            self.ipc_dir / "service.json",
            {
                "protocol_version": PROTOCOL_VERSION,
                "service_version": SERVICE_VERSION,
                "service_instance_id": self.service_instance_id,
                "started_at": time.time(),
            },
        )

    def sweep(self, max_age: float) -> None:
        """max_age 秒より古い完了ファイルだけを掃除する。"""
        limit = time.time() - max_age
        for d in (self.responses, self.claimed, self.cancelled):
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for path in entries:
                try:
                    if path.stat().st_mtime < limit:
                        self._unlink(path)
                except OSError:
                    pass

    # ---- Bridge 状態

    def bridge_status(self) -> BridgeStatus:
        path = self.ipc_dir / "bridge.json"
        data: dict[str, Any] | None = None
        mtime = 0.0
        now = time.time()
        # Bridge は remove → rename で置換するため、一瞬だけ読めないことがある。
        for attempt in range(3):
            try:
                mtime = path.stat().st_mtime
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                    break
            except (OSError, ValueError):
                pass
            if attempt < 2:
                time.sleep(0.015)
        if data is not None:
            self._last = (now, data, mtime)
        elif self._last is not None and now - self._last[0] < self.heartbeat_stale:
            _, data, mtime = self._last
        if data is None:
            return BridgeStatus(state="unavailable")

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        status = BridgeStatus(
            state="connected",
            version=text("bridge_version"),
            instance_id=text("bridge_instance_id"),
            running_request_id=text("running_request_id"),
            service_instance_id=text("service_instance_id"),
            active_project_name=text("active_project_name"),
            selected_part_name=text("selected_part_name"),
            playback_active=data.get("playback_active") is True,
        )
        if data.get("state") != "running":
            status.state = "unavailable"
            status.running_request_id = None
        elif now - mtime > self.heartbeat_stale:
            status.state = "unresponsive"
        return status

    # ---- 要求と応答

    def write_request(self, request_id: str, method: str, params: dict[str, Any],
                      bridge_instance_id: str, start_deadline: float) -> None:
        doc = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": method,
            "params": params,
            "service_instance_id": self.service_instance_id,
            "bridge_instance_id": bridge_instance_id,
            "issued_at": time.time(),
            "start_deadline": int(start_deadline),
        }
        final = self.requests / f"{request_id}.json"
        if final.exists():
            raise FileExistsError(final)
        self._write_atomic(final, doc, replace=False)

    def try_cancel(self, request_id: str) -> bool:
        """未 claim の要求を取り消す。成功すれば Bridge は決して実行しない。"""
        src = self.requests / f"{request_id}.json"
        dst = self.cancelled / f"{request_id}.{int(time.time() * 1000)}.json"
        for _ in range(3):
            try:
                os.rename(src, dst)
                return True
            except FileNotFoundError:
                return False
            except OSError:
                time.sleep(0.01)
        return not src.exists() and dst.exists()

    def read_response(self, request_id: str) -> dict[str, Any] | None:
        path = self.responses / f"{request_id}.json"
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return {"invalid": True}
        if not isinstance(doc, dict):
            return {"invalid": True}
        return doc

    # ---- 内部

    def _write_atomic(self, final: Path, doc: dict[str, Any], replace: bool = True) -> None:
        tmp = final.with_name(f"{final.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, allow_nan=False)
        last_error: OSError | None = None
        for _ in range(20):
            try:
                if replace:
                    os.replace(tmp, final)
                else:
                    os.rename(tmp, final)
                return
            except FileExistsError:
                self._unlink(tmp)
                raise
            except OSError as e:  # 置換競合は少し待って再試行
                last_error = e
                time.sleep(0.01)
        self._unlink(tmp)
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass
