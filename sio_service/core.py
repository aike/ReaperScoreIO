"""要求の受付・再送判定・直列配送・応答の保存。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from . import PROTOCOL_VERSION, SERVICE_VERSION
from .config import Config
from .ipc import BridgeLink, BridgeStatus
from .jsonutil import canonical, dumps
from .models import METHODS, error_details

log = logging.getLogger("sio.service")

QUEUED, RUNNING, SUCCEEDED, FAILED, EXPIRED = "queued", "running", "succeeded", "failed", "expired"


def error_body(request_id: str | None, code: str, message: str,
               details: dict[str, Any] | None = None, retryable: bool = False) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "details": details or {}, "retryable": retryable},
    }


class QueueFull(Exception):
    pass


@dataclass
class Record:
    request_id: str
    digest: bytes
    method: str
    params: dict[str, Any]
    state: str = QUEUED
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None
    blob: bytes | None = None  # zlib 圧縮した終端応答
    dispatched: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def response_bytes(self) -> bytes:
        assert self.blob is not None
        return zlib.decompress(self.blob)


class Core:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.instance_id = str(uuid.uuid4())
        self.link = BridgeLink(config.ipc_dir, self.instance_id, config.timing.heartbeat_stale)
        self.records: dict[str, Record] = {}
        self.finished_order: deque[str] = deque()
        self.retained_bytes = 0
        self.queue: deque[Record] = deque()
        self.wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ 起動・終了

    async def start(self) -> None:
        self.link.start()
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="sio-dispatch"),
            asyncio.create_task(self._sweep_loop(), name="sio-sweep"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ------------------------------------------------------------ 受付

    async def handle(self, envelope: dict[str, Any]) -> bytes:
        """検証済みの外枠を処理し、HTTP 200 の応答本文を返す。キュー満杯なら QueueFull。"""
        rid: str = envelope["request_id"]
        digest = hashlib.sha256(canonical(envelope).encode("utf-8")).digest()

        rec = self.records.get(rid)
        if rec is not None:
            if rec.state == EXPIRED:
                return dumps(error_body(rid, "REQUEST_EXPIRED", "この request_id の保存期間は終了しました。"))
            if rec.digest != digest:
                return dumps(error_body(rid, "REQUEST_ID_REUSE", "同じ request_id が異なる内容で再利用されました。"))
            return await self._wait(rec)

        method: str = envelope["method"]
        params: dict[str, Any] = envelope["params"]
        rec = Record(request_id=rid, digest=digest, method=method, params=params)

        immediate = self._immediate(rec, envelope)
        if immediate is not None:
            self.records[rid] = rec
            self._finish(rec, immediate)
            return rec.response_bytes()

        if len(self.queue) >= self.config.limits.max_waiting:
            raise QueueFull()
        self.records[rid] = rec
        self.queue.append(rec)
        self.wakeup.set()
        return await self._wait(rec)

    def _immediate(self, rec: Record, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Bridge へ配送せずに確定する応答（検証エラー、get_status、get_request_status）。"""
        rid = rec.request_id
        if envelope["protocol_version"] != PROTOCOL_VERSION:
            return error_body(rid, "VERSION_MISMATCH", "protocol_version が一致しません。",
                              {"supported": PROTOCOL_VERSION})
        spec = METHODS.get(rec.method)
        if spec is None:
            return error_body(rid, "UNKNOWN_METHOD", "未知の method です。", {"method": rec.method[:64]})
        model, to_bridge = spec
        params = rec.params
        limits = self.config.limits
        for key, limit in (("operations", limits.max_operations), ("targets", limits.max_targets)):
            value = params.get(key)
            if isinstance(value, list) and len(value) > limit:
                return error_body(rid, "LIMIT_EXCEEDED", f"{key} の件数が上限を超えています。",
                                  {"field": key, "limit": limit, "count": len(value)})
        try:
            parsed = model.model_validate(params)
        except ValidationError as e:
            return error_body(rid, "INVALID_REQUEST", "要求の引数が不正です。", error_details(e))
        if rec.method == "get_status":
            return {"request_id": rid, "ok": True, "result": self.status_result()}
        if rec.method == "get_request_status":
            return {"request_id": rid, "ok": True, "result": self.request_status(parsed.target_request_id)}
        assert to_bridge
        return None

    async def _wait(self, rec: Record) -> bytes:
        timing = self.config.timing
        if not rec.done.is_set():
            try:
                await asyncio.wait_for(rec.dispatched.wait(), timing.queue_wait)
                await asyncio.wait_for(rec.done.wait(), timing.dispatch_wait)
            except asyncio.TimeoutError:
                pass
        if rec.done.is_set() and rec.blob is not None:
            return rec.response_bytes()
        if rec.state == EXPIRED:
            return dumps(error_body(rec.request_id, "REQUEST_EXPIRED", "この request_id の保存期間は終了しました。"))
        return dumps(error_body(rec.request_id, "REQUEST_PENDING",
                                "処理結果はまだ確定していません。状態を確認してください。",
                                {"target_request_id": rec.request_id}))

    # ------------------------------------------------------------ 状態照会

    def status_result(self) -> dict[str, Any]:
        st = self.link.bridge_status()
        # Bridge がこの Service 世代をまだ確認していなければ、選択は解除される予定である。
        current = st.service_instance_id == self.instance_id
        return {
            "protocol_version": PROTOCOL_VERSION,
            "service_version": SERVICE_VERSION,
            "service_instance_id": self.instance_id,
            "bridge_state": st.state,
            "bridge_version": st.version,
            "bridge_instance_id": st.instance_id,
            "running_request_id": st.running_request_id,
            "active_project_name": st.active_project_name if current else None,
            "selected_part_name": st.selected_part_name if current else None,
            "playback_active": st.playback_active if current and st.state != "unavailable" else False,
        }

    def request_status(self, target_id: str) -> dict[str, Any]:
        rec = self.records.get(target_id)
        state = "unknown" if rec is None else rec.state
        response = None
        if rec is not None and rec.state in (SUCCEEDED, FAILED) and rec.blob is not None:
            import json

            response = json.loads(rec.response_bytes())
        return {"target_request_id": target_id, "state": state, "response": response}

    # ------------------------------------------------------------ 終端処理

    def _finish(self, rec: Record, body: dict[str, Any]) -> None:
        if rec.done.is_set():
            return
        raw = dumps(body)
        if len(raw) > self.config.limits.response_bytes:
            body = error_body(rec.request_id, "LIMIT_EXCEEDED", "応答が大きすぎます。範囲やパートを絞ってください。",
                              {"limit": self.config.limits.response_bytes, "unit": "bytes"})
            raw = dumps(body)
        rec.blob = zlib.compress(raw, 1)
        rec.state = SUCCEEDED if body.get("ok") is True else FAILED
        rec.finished = time.monotonic()
        rec.params = {}
        self.finished_order.append(rec.request_id)
        self.retained_bytes += len(rec.blob)
        rec.dispatched.set()
        rec.done.set()
        self._enforce_budget()

    def _expire(self, rec: Record) -> None:
        if rec.blob is not None:
            self.retained_bytes -= len(rec.blob)
            rec.blob = None
        rec.state = EXPIRED

    def _enforce_budget(self) -> None:
        now = time.monotonic()
        limits = self.config.limits
        while self.finished_order:
            rec = self.records[self.finished_order[0]]
            too_old = rec.finished is not None and now - rec.finished > limits.retention_seconds
            if not too_old and self.retained_bytes <= limits.retained_bytes:
                break
            if not too_old and len(self.finished_order) == 1:
                break  # 直近の応答は常に保持する
            self.finished_order.popleft()
            self._expire(rec)

    async def _sweep_loop(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(60)
            self._enforce_budget()
            ticks += 1
            if ticks % 60 == 0:
                await asyncio.to_thread(self.link.sweep, self.config.limits.retention_seconds)

    # ------------------------------------------------------------ 配送

    async def _dispatch_loop(self) -> None:
        while True:
            if not self.queue:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            rec = self.queue.popleft()
            try:
                body = await self._deliver(rec)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("dispatch failed for %s", rec.request_id)
                body = error_body(rec.request_id, "INTERNAL_ERROR", "Service の内部エラーです。",
                                  {"reason": type(e).__name__})
            self._finish(rec, body)

    def _unavailable(self, rec: Record, st: BridgeStatus, reason: str) -> dict[str, Any]:
        return error_body(rec.request_id, "BRIDGE_UNAVAILABLE",
                          "REAPER の SIO Bridge に接続できません。要求は実行されていません。",
                          {"bridge_state": st.state, "reason": reason}, retryable=True)

    def _outcome_unknown(self, rec: Record, reason: str) -> dict[str, Any]:
        return error_body(rec.request_id, "OUTCOME_UNKNOWN",
                          "Bridge が応答しないため、実行結果は不明です。現在のデータを取得して確認してください。",
                          {"reason": reason})

    async def _deliver(self, rec: Record) -> dict[str, Any]:
        timing = self.config.timing
        rid = rec.request_id
        st = self.link.bridge_status()
        if st.state != "connected" or st.instance_id is None:
            return self._unavailable(rec, st, "bridge_not_connected")
        bridge_id = st.instance_id
        deadline = time.time() + timing.start_timeout
        self.link.write_request(rid, rec.method, rec.params, bridge_id, deadline)
        rec.state = RUNNING
        rec.dispatched.set()

        claimed_at: float | None = None
        next_status = 0.0
        while True:
            doc = await asyncio.to_thread(self.link.read_response, rid)
            if doc is not None:
                return self._accept_response(rec, doc)
            now = time.time()
            if claimed_at is None and now > deadline + 1.0:
                if self.link.try_cancel(rid):
                    return self._unavailable(rec, self.link.bridge_status(), "not_started_before_deadline")
                claimed_at = now  # Bridge が claim 済み。応答を待つ。
            if claimed_at is not None and now >= next_status:
                next_status = now + 0.5
                st = self.link.bridge_status()
                gone = st.state == "unavailable" or (st.instance_id is not None and st.instance_id != bridge_id)
                abandoned = st.state == "unresponsive" and now - claimed_at > timing.abandon_after
                if gone or abandoned:
                    doc = await asyncio.to_thread(self.link.read_response, rid)
                    if doc is not None:
                        return self._accept_response(rec, doc)
                    return self._outcome_unknown(rec, "bridge_restarted" if gone else "bridge_unresponsive")
            await asyncio.sleep(timing.poll_interval)

    def _accept_response(self, rec: Record, doc: dict[str, Any]) -> dict[str, Any]:
        response = doc.get("response")
        if (
            doc.get("request_id") != rec.request_id
            or not isinstance(response, dict)
            or not isinstance(response.get("ok"), bool)
        ):
            return self._outcome_unknown(rec, "invalid_bridge_response")
        if response["ok"]:
            if "result" not in response:
                return self._outcome_unknown(rec, "invalid_bridge_response")
            return {"request_id": rec.request_id, "ok": True, "result": response["result"]}
        err = response.get("error")
        if not isinstance(err, dict) or not isinstance(err.get("code"), str):
            return self._outcome_unknown(rec, "invalid_bridge_response")
        details = err.get("details")
        return error_body(
            rec.request_id,
            err["code"],
            str(err.get("message", "")),
            details if isinstance(details, dict) else {},
            bool(err.get("retryable", False)),
        )
