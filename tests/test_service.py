"""SIO Service の単体試験。REAPER の代わりに、ファイル IPC を話す偽 Bridge を使う。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sio_service.app import create_app
from sio_service.config import Config, Limits, Timing

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeBridge:
    """requests/ を claim して responses/ へ書く最小の Bridge。"""

    def __init__(self, ipc: Path, handler=None, delay: float = 0.0) -> None:
        self.ipc = ipc
        self.instance_id = str(uuid.uuid4())
        self.handler = handler or (lambda method, params: {"echo": method})
        self.delay = delay
        self.claim = True          # False なら要求を拾わない
        self.respond = True        # False なら claim だけして応答しない
        self.calls: list[tuple[str, dict]] = []
        self.running = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        for d in ("requests", "claimed", "responses"):
            (ipc / d).mkdir(parents=True, exist_ok=True)

    def start(self) -> "FakeBridge":
        self._heartbeat()
        self._thread.start()
        return self

    def stop(self, state: str = "stopped") -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._heartbeat(state)

    def _heartbeat(self, state: str = "running") -> None:
        service = None
        try:
            service = json.loads((self.ipc / "service.json").read_text("utf-8"))["service_instance_id"]
        except (OSError, ValueError, KeyError):
            pass
        doc = {
            "bridge_version": "fake", "bridge_instance_id": self.instance_id, "state": state,
            "running_request_id": self.running, "service_instance_id": service,
            "active_project_name": "Fake.rpp", "selected_part_name": None, "playback_active": True,
        }
        tmp = self.ipc / f"bridge.json.{self.instance_id}.tmp"
        tmp.write_text(json.dumps(doc), "utf-8")
        for _ in range(50):
            try:
                os.replace(tmp, self.ipc / "bridge.json")
                return
            except OSError:
                time.sleep(0.005)

    def _run(self) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            if time.time() - last_beat > 0.1:
                self._heartbeat()
                last_beat = time.time()
            if self.claim:
                for path in list((self.ipc / "requests").glob("*.json")):
                    claimed = self.ipc / "claimed" / path.name
                    try:
                        os.rename(path, claimed)
                    except OSError:
                        continue
                    req = json.loads(claimed.read_text("utf-8"))
                    self.calls.append((req["method"], req["params"]))
                    self.running = req["request_id"]
                    self._heartbeat()
                    if not self.respond:
                        continue
                    if self.delay:
                        time.sleep(self.delay)
                    try:
                        response = {"request_id": req["request_id"], "ok": True,
                                    "result": self.handler(req["method"], req["params"])}
                    except KeyError as e:
                        response = {"request_id": req["request_id"], "ok": False,
                                    "error": {"code": str(e.args[0]), "message": "x", "details": {}, "retryable": False}}
                    doc = {"request_id": req["request_id"], "bridge_instance_id": self.instance_id, "response": response}
                    tmp = self.ipc / "responses" / (path.name + ".tmp")
                    tmp.write_text(json.dumps(doc), "utf-8")
                    os.rename(tmp, self.ipc / "responses" / path.name)
                    self.running = None
            time.sleep(0.01)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        resource_path=tmp_path,
        token=TOKEN,
        limits=Limits(max_waiting=2),
        timing=Timing(queue_wait=0.6, dispatch_wait=0.6, start_timeout=1.0, heartbeat_stale=1.0,
                      abandon_after=1.0, poll_interval=0.01),
    )


@pytest.fixture
def client(config: Config):
    with TestClient(create_app(config)) as c:
        yield c


@pytest.fixture
def bridge(config: Config, client):
    b = FakeBridge(config.ipc_dir).start()
    yield b
    b.stop()


def rpc(client, method, params=None, request_id=None, version="2.3"):
    body = {"protocol_version": version, "request_id": request_id or str(uuid.uuid4()),
            "method": method, "params": {} if params is None else params}
    return client.post("/v2/rpc", json=body, headers=AUTH)


def err(resp):
    body = resp.json()
    assert body["ok"] is False
    assert set(body["error"]) == {"code", "message", "details", "retryable"}
    return body["error"]["code"]


# ---------------------------------------------------------------- HTTP 層


def test_health_needs_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_auth_required(client):
    body = {"protocol_version": "2.3", "request_id": str(uuid.uuid4()), "method": "get_status", "params": {}}
    assert client.post("/v2/rpc", json=body).status_code == 401
    assert client.post("/v2/rpc", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(f"/v2/rpc?token={TOKEN}", json=body).status_code == 401


@pytest.mark.parametrize("raw", [
    b"{not json",
    b'{"protocol_version":"2.3","protocol_version":"2.3","request_id":"x","method":"m","params":{}}',
    b'{"protocol_version":"2.3","request_id":"23e4a562-38be-4f4a-b89d-729dd2cfa327","method":"get_notes","params":{"x":NaN}}',
    b'{"protocol_version":"2.3","request_id":"23e4a562-38be-4f4a-b89d-729dd2cfa327","method":"get_notes","params":{"x":1e999}}',
    b'[]',
    b'{"protocol_version":"2.3","request_id":"23E4A562-38BE-4F4A-B89D-729DD2CFA327","method":"get_status","params":{}}',
    b'{"protocol_version":"2.3","request_id":"23e4a562-38be-4f4a-b89d-729dd2cfa327","method":"get_status"}',
    b'{"protocol_version":"2.3","request_id":"23e4a562-38be-4f4a-b89d-729dd2cfa327","method":"get_status","params":[]}',
    b'{"protocol_version":"2.3","request_id":"23e4a562-38be-4f4a-b89d-729dd2cfa327","method":"get_status","params":{},"extra":1}',
])
def test_bad_envelope_is_400(client, raw):
    r = client.post("/v2/rpc", content=raw, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert err(r) == "INVALID_REQUEST"


def test_envelope_error_recovers_request_id(client):
    rid = str(uuid.uuid4())
    r = client.post("/v2/rpc", json={"request_id": rid, "method": "get_status"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["request_id"] == rid


def test_body_too_large_is_413(client):
    r = client.post("/v2/rpc", content=b" " * (1024 * 1024 + 1), headers=AUTH)
    assert r.status_code == 413


def test_version_and_method(client):
    assert err(rpc(client, "get_status", version="2.2")) == "VERSION_MISMATCH"
    assert err(rpc(client, "no_such_method")) == "UNKNOWN_METHOD"


# ---------------------------------------------------------------- 検証


@pytest.mark.parametrize("method,params", [
    ("get_status", {"x": 1}),
    ("get_notes", {"project_name": "Harmony.rpp"}),
    ("get_notes", {"part_names": []}),
    ("get_notes", {"part_names": ["a", "a"]}),
    ("get_notes", {"part_names": ["a"], "selected_part": True}),
    ("get_notes", {"all_parts": True}),
    ("get_notes", {"selected_part": 1}),
    ("get_notes", {"expect_project_name": ""}),
    ("list_projects", {"expect_project_name": "Harmony.rpp"}),
    ("stop", {"expect_project_name": "Harmony.rpp"}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "channel": 1}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "expect": {}}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "expect": {"selected": True}}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60, "start_qn": 0, "channel": 17}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60.0, "start_qn": 0}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60, "start_qn": 0, "start": {"qn": 0}}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "start": {"bar": 1, "beat": 1}}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60, "start": {"bar": 1}}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60, "start": 4}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "expect": {"start_qn": 0, "start": {"qn": 0}}}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1},
                                     "set": {"start_qn": 1, "start": {"bar": 1, "beat": 2}}}]}),
    ("apply_patch", {"operations": [{"op": "insert", "client_note_id": "a", "note": {
        "pitch": 60, "start_qn": 0, "start": {"bar": 1, "beat": 1},
        "duration_qn": 1, "velocity": 80, "channel": 1, "muted": False}}]}),
    ("get_notes", {"include_muted": None}),
    ("get_notes", {"relation": "sounding_at"}),
    ("get_notes", {"at": {"qn": 1}}),
    ("get_notes", {"relation": "sounding_at", "at": {"qn": 1}, "range": {"start": {"qn": 0}, "end": {"qn": 1}}}),
    ("get_notes", {"range": {"start": {"qn": 2}, "end": {"qn": 2}}}),
    ("get_notes", {"range": {"start": {"qn": 0, "bar": 1, "beat": 1}, "end": {"qn": 2}}}),
    ("get_notes", {"range": {"start": {"bar": 1}, "end": {"qn": 2}}}),
    ("get_notes", {"range": {"start": {"bar": 0, "beat": 1}, "end": {"qn": 2}}}),
    ("get_notes", {"range": {"start": {"bar": True, "beat": 1}, "end": {"qn": 2}}}),
    ("get_notes", {"range": {"start": {"qn": -1}, "end": {"qn": 2}}}),
    ("select_project", {}),
    ("select_project", {"project_name": ""}),
    ("select_part", {"part_name": "a", "project_name": "b"}),
    ("create_project", {"project_name": "New.rpp"}),
    ("add_track", {}),
    ("add_item", {}),
    ("add_item", {"part_name": "Piano"}),
    ("add_item", {"part_name": "", "range": {"start": {"qn": 0}, "end": {"qn": 4}}}),
    ("add_item", {"range": {"start": {"qn": 4}, "end": {"qn": 4}}}),
    ("add_item", {"range": {"start": {"qn": 0}, "end": {"qn": 4}}, "length_qn": 4}),
    ("add_track", {"part_name": ""}),
    ("add_track", {"part_name": "Piano", "instrument": ""}),
    ("add_track", {"part_name": "Piano", "instrument": None}),
    ("add_track", {"part_name": "Piano", "preset": "Grand"}),
    ("select_notes", {}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "pitch": 60, "start_qn": 0}}]}),
    ("select_notes", {"targets": [{"target": {"pitch": 60}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 0}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1.0}}]}),
    ("select_notes", {"targets": [{"target": {"note_id": 1, "guid": "x"}}]}),
    ("apply_patch", {"operations": []}),
    ("apply_patch", {"operations": [{"op": "move", "target": {"note_id": 1}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {"pitch": None}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {"pitch": 128}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {"selected": True}}]}),
    ("apply_patch", {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {"duration_qn": 0}}]}),
    ("apply_patch", {"operations": [{"op": "delete", "target": {"note_id": 1}, "set": {"pitch": 1}}]}),
    ("apply_patch", {"operations": [{"op": "insert", "client_note_id": "a", "note": {
        "pitch": 60, "start_qn": 0, "duration_qn": 1, "velocity": 80, "channel": 1}}]}),
    ("apply_patch", {"operations": [{"op": "insert", "client_note_id": "a", "note": {
        "pitch": 60, "start_qn": 0, "duration_qn": 1, "velocity": 0, "channel": 1, "muted": False}}]}),
    ("apply_patch", {"operations": [
        {"op": "insert", "client_note_id": "a", "note": {
            "pitch": 60, "start_qn": 0, "duration_qn": 1, "velocity": 80, "channel": 1, "muted": False}},
        {"op": "insert", "client_note_id": "a", "note": {
            "pitch": 62, "start_qn": 0, "duration_qn": 1, "velocity": 80, "channel": 1, "muted": False}}]}),
    ("play_range", {}),
    ("get_request_status", {"target_request_id": "nope"}),
])
def test_invalid_params(client, bridge, method, params):
    assert err(rpc(client, method, params)) == "INVALID_REQUEST"
    assert bridge.calls == []


def test_too_many_operations(client, bridge):
    ops = [{"op": "delete", "target": {"note_id": i + 1}} for i in range(10_001)]
    assert err(rpc(client, "apply_patch", {"operations": ops})) == "LIMIT_EXCEEDED"
    assert bridge.calls == []


def test_valid_params_reach_bridge(client, bridge):
    cases = [
        ("list_projects", {}),
        ("select_project", {"project_name": "Harmony.rpp"}),
        ("select_part", {"part_name": "チェロ"}),
        ("create_project", {}),
        ("add_item", {"range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 9, "beat": 1}}}),
        ("add_item", {"part_name": "ピアノ", "range": {"start": {"qn": 0}, "end": {"qn": 16}},
                      "expect_project_name": "Harmony.rpp"}),
        ("add_track", {"part_name": "ピアノ"}),
        ("add_track", {"part_name": "Violin I", "instrument": "ReaSynth", "expect_project_name": "Harmony.rpp"}),
        ("get_notes", {}),
        ("get_notes", {"part_names": ["第1バイオリン", "チェロ"], "at": {"bar": 4, "beat": 2}, "relation": "sounding_at"}),
        ("get_notes", {"range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}},
                       "selected_part": True, "expect_project_name": "Harmony.rpp"}),
        ("get_score_info", {"expect_project_name": "Harmony.rpp"}),
        ("select_notes", {"targets": []}),
        ("select_notes", {"targets": [{"part_name": "チェロ", "target": {"note_id": 5}},
                                      {"target": {"pitch": 65, "start_qn": 16.0, "channel": 2,
                                                  "expect": {"duration_qn": 1, "muted": False}}}]}),
        ("apply_patch", {"label": "CT", "dry_run": True, "operations": [
            {"op": "update", "part_name": "チェロ", "target": {"note_id": 5, "expect": {"pitch": 60}},
             "set": {"pitch": 64}},
            {"op": "delete", "target": {"pitch": 65, "start_qn": 16}},
            {"op": "insert", "client_note_id": "new-1", "note": {
                "pitch": 60, "start_qn": 20.0, "duration_qn": 1.0, "velocity": 80, "channel": 1, "muted": False}}]}),
        ("play_range", {"range": {"start": {"qn": 0}, "end": {"bar": 3, "beat": 1, "offset_qn": 0.5}}}),
        ("select_notes", {"targets": [{"target": {"pitch": 65, "start": {"bar": 5, "beat": 1},
                                                  "expect": {"start": {"qn": 16}}}}]}),
        ("apply_patch", {"operations": [
            {"op": "update", "target": {"pitch": 60, "start": {"bar": 5, "beat": 1, "offset_qn": 0.5}},
             "set": {"start": {"bar": 5, "beat": 2}}},
            {"op": "insert", "client_note_id": "n", "note": {
                "pitch": 60, "start": {"bar": 6, "beat": 1}, "duration_qn": 1.0, "velocity": 80,
                "channel": 1, "muted": False}}]}),
        ("stop", {}),
    ]
    for method, params in cases:
        r = rpc(client, method, params)
        assert r.status_code == 200 and r.json()["ok"] is True, (method, r.text)
    assert bridge.calls == cases


# ---------------------------------------------------------------- 状態・配送


def test_status_without_bridge(client):
    result = rpc(client, "get_status").json()["result"]
    assert result["bridge_state"] == "unavailable"
    assert result["bridge_instance_id"] is None and result["active_project_name"] is None
    assert result["playback_active"] is False
    assert err(rpc(client, "list_projects")) == "BRIDGE_UNAVAILABLE"


def test_status_with_bridge(client, bridge):
    time.sleep(0.3)
    result = rpc(client, "get_status").json()["result"]
    assert result["bridge_state"] == "connected"
    assert result["bridge_instance_id"] == bridge.instance_id
    assert result["active_project_name"] == "Fake.rpp" and result["playback_active"] is True
    assert result["protocol_version"] == "2.3"


def test_bridge_stopped_and_stale(client, config):
    b = FakeBridge(config.ipc_dir).start()
    b._stop.set()
    b._thread.join()
    time.sleep(1.3)  # heartbeat_stale を超える
    assert rpc(client, "get_status").json()["result"]["bridge_state"] == "unresponsive"
    assert err(rpc(client, "list_projects")) == "BRIDGE_UNAVAILABLE"
    b._heartbeat("stopped")
    assert rpc(client, "get_status").json()["result"]["bridge_state"] == "unavailable"


def test_bridge_error_passthrough(client, config):
    def handler(method, params):
        raise KeyError("PART_NOT_FOUND")

    b = FakeBridge(config.ipc_dir, handler).start()
    try:
        assert err(rpc(client, "select_part", {"part_name": "x"})) == "PART_NOT_FOUND"
    finally:
        b.stop()


def test_retry_same_id_does_not_reexecute(client, bridge):
    rid = str(uuid.uuid4())
    params = {"operations": [{"op": "delete", "target": {"note_id": 1}}]}
    first = rpc(client, "apply_patch", params, rid)
    second = rpc(client, "apply_patch", params, rid)
    assert first.json() == second.json() and first.json()["ok"] is True
    assert len(bridge.calls) == 1
    changed = {"operations": [{"op": "delete", "target": {"note_id": 2}}]}
    assert err(rpc(client, "apply_patch", changed, rid)) == "REQUEST_ID_REUSE"
    assert len(bridge.calls) == 1
    status = rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]
    assert status["state"] == "succeeded" and status["response"] == first.json()


def test_terminal_error_is_cached(client):
    rid = str(uuid.uuid4())
    assert err(rpc(client, "list_projects", request_id=rid)) == "BRIDGE_UNAVAILABLE"
    status = rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]
    assert status["state"] == "failed" and status["response"]["error"]["code"] == "BRIDGE_UNAVAILABLE"
    unknown = rpc(client, "get_request_status", {"target_request_id": str(uuid.uuid4())}).json()["result"]
    assert unknown == {"target_request_id": unknown["target_request_id"], "state": "unknown", "response": None}


def test_pending_then_completes(client, config):
    b = FakeBridge(config.ipc_dir, delay=1.2).start()
    try:
        rid = str(uuid.uuid4())
        r = rpc(client, "list_projects", request_id=rid)
        assert err(r) == "REQUEST_PENDING"
        assert r.json()["error"]["details"]["target_request_id"] == rid
        assert rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]["state"] == "running"
        time.sleep(1.0)
        assert rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]["state"] == "succeeded"
        assert rpc(client, "list_projects", request_id=rid).json()["ok"] is True
        assert len(b.calls) == 1
    finally:
        b.stop()


def test_unclaimed_request_is_cancelled_and_never_runs(client, config):
    b = FakeBridge(config.ipc_dir)
    b.claim = False
    b.start()
    try:
        rid = str(uuid.uuid4())
        assert err(rpc(client, "list_projects", request_id=rid)) == "REQUEST_PENDING"
        time.sleep(2.0)  # start_timeout + 1 秒で取消
        status = rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]
        assert status["state"] == "failed" and status["response"]["error"]["code"] == "BRIDGE_UNAVAILABLE"
        b.claim = True
        time.sleep(0.3)
        assert b.calls == []  # 取消後に Bridge が復帰しても実行されない
    finally:
        b.stop()


def test_claimed_without_response_is_outcome_unknown(client, config):
    b = FakeBridge(config.ipc_dir)
    b.respond = False
    b.start()
    rid = str(uuid.uuid4())
    assert err(rpc(client, "apply_patch", {"operations": [{"op": "delete", "target": {"note_id": 1}}]}, rid)) \
        == "REQUEST_PENDING"
    b.stop()  # Bridge 終了
    replacement = FakeBridge(config.ipc_dir).start()  # 別世代の Bridge が起動
    try:
        time.sleep(2.5)
        status = rpc(client, "get_request_status", {"target_request_id": rid}).json()["result"]
        assert status["state"] == "failed" and status["response"]["error"]["code"] == "OUTCOME_UNKNOWN"
        assert replacement.calls == []
    finally:
        replacement.stop()


def test_queue_full_is_503(client, config):
    b = FakeBridge(config.ipc_dir, delay=0.4).start()
    try:
        with ThreadPoolExecutor(8) as pool:
            ids = [str(uuid.uuid4()) for _ in range(6)]
            futures = []
            for rid in ids:
                futures.append(pool.submit(rpc, client, "list_projects", None, rid))
                time.sleep(0.03)
            codes = [f.result().status_code for f in futures]
        assert codes.count(503) >= 1 and codes.count(200) >= 3
        rejected = ids[codes.index(503)]
        # 受け付けられなかった ID は記録されず、後から同じ ID で実行できる。
        assert rpc(client, "get_request_status", {"target_request_id": rejected}).json()["result"]["state"] == "unknown"
    finally:
        b.stop()


def test_service_generation_is_published(client, config):
    doc = json.loads((config.ipc_dir / "service.json").read_text("utf-8"))
    assert doc["service_instance_id"] == rpc(client, "get_status").json()["result"]["service_instance_id"]
