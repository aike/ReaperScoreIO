"""sio_client の単体試験（偽の通信路を使い、REAPER も Service も不要）。"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from sio_client import (
    SIO,
    BridgeUnavailable,
    Note,
    NoteMismatch,
    OutcomeUnknown,
    RequestPending,
    SIOConfigError,
    SIOConnectionError,
    SIOError,
    pitch_name,
    pitch_number,
)


def note_json(note_id=1, part="Cello", pitch=60, start=0.0, dur=1.0, bar=1, beat=1):
    return {"note_id": note_id, "part_name": part, "pitch": pitch, "start_qn": start, "duration_qn": dur,
            "position": {"bar": bar, "beat": beat, "offset_qn": 0.0}, "velocity": 80, "channel": 1,
            "muted": False, "item_muted": False, "track_muted": False, "selected": False}


RESULTS = {
    "get_status": {"bridge_state": "connected", "active_project_name": "Harmony.rpp", "selected_part_name": None,
                   "playback_active": False, "service_version": "0.6.0", "bridge_version": "0.6.0"},
    "list_projects": {"projects": [{"project_name": "Harmony.rpp", "active": True, "addressable": True},
                                   {"project_name": "", "active": False, "addressable": False}]},
    "select_project": {"project_name": "Study.rpp", "selected_part_name": "Solo"},
    "create_project": {"project_name": "", "selected_part_name": None},
    "select_part": {"project_name": "Harmony.rpp", "selected_part_name": "Cello"},
    "add_track": {"project_name": "Harmony.rpp", "part_name": "Viola", "order": 3, "selected_part_name": "Viola",
                  "instrument_loaded": False, "instrument_name": None,
                  "warnings": [{"code": "INSTRUMENT_NOT_FOUND", "message": "m", "details": {"instrument": "X"}}]},
    "add_item": {"project_name": "Harmony.rpp", "part_name": "Viola", "range": {"start_qn": 0.0, "end_qn": 16.0}},
    "get_score_info": {"project_name": "Harmony.rpp", "selected_part_name": "Cello",
                       "parts": [{"part_name": "Cello", "order": 2, "addressable": True, "selected": True}],
                       "meters": [{"start_qn": 0.0, "bar": 1, "numerator": 4, "denominator": 4}],
                       "note_range": {"start_qn": 0.0, "end_qn": 8.0}, "note_range_complete": True,
                       "constraints": [], "warnings": []},
    "get_notes": {"project_name": "Harmony.rpp", "parts": [{"part_name": "Cello", "order": 2}],
                  "meters": [{"start_qn": 0.0, "bar": 1, "numerator": 4, "denominator": 4}],
                  "range": {"start_qn": 0.0, "end_qn": 8.0}, "warnings": [],
                  "notes": [note_json(1, pitch=36), note_json(2, pitch=43, start=4.0, dur=2.0, bar=2)]},
    "select_notes": {"project_name": "Harmony.rpp", "selected_count": 2},
    "apply_patch": {"project_name": "Harmony.rpp", "added": 1, "updated": 1, "deleted": 0, "dry_run": False,
                    "resolved": [{"operation_index": 0, "op": "update", "note": note_json(1, pitch=36)}],
                    "inserted": [{"operation_index": 1, "client_note_id": "n2", "part_name": "Cello", "note_id": 3}]},
    "play_range": {"project_name": "Harmony.rpp", "started": True},
    "stop": {"stopped": True},
}


class Fake:
    """要求を記録し、用意した応答を返す通信路。script に積んだものを先に使う。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list = []          # 応答の dict、例外、または (status, bytes)
        self.headers: dict = {}

    def __call__(self, body: bytes, headers: dict, timeout: float):
        env = json.loads(body)
        self.requests.append(env)
        self.headers = headers
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, tuple):
                return item
            if callable(item):
                item = item(env)
            return 200, json.dumps({"request_id": env["request_id"], **item}).encode()
        return 200, json.dumps({"request_id": env["request_id"], "ok": True,
                                "result": RESULTS[env["method"]]}).encode()

    @property
    def last(self):
        return self.requests[-1]["method"], self.requests[-1]["params"]


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def sio(fake):
    s = SIO(url="http://sio.test", token="secret", transport=fake)
    s._sleep = lambda seconds: None
    return s


def error(code, **details):
    return {"ok": False, "error": {"code": code, "message": "m", "details": details, "retryable": False}}


# ---------------------------------------------------------------- 接続と外枠


def test_config_is_read_from_resource_path(tmp_path, fake):
    (tmp_path / "SIO").mkdir()
    (tmp_path / "SIO" / "config.json").write_text(json.dumps({"port": 19999, "token": "tok"}), encoding="utf-8")
    s = SIO(tmp_path, transport=fake)
    assert s.url == "http://127.0.0.1:19999"
    s.status()
    assert fake.headers["Authorization"] == "Bearer tok"
    with pytest.raises(SIOConfigError):
        SIO(tmp_path / "missing")


def test_envelope_and_status(sio, fake):
    st = sio.status()
    env = fake.requests[0]
    assert env["protocol_version"] == "2.3" and env["method"] == "get_status" and env["params"] == {}
    assert len(env["request_id"]) == 36 and sio.last_request_id == env["request_id"]
    assert fake.headers["Authorization"] == "Bearer secret"
    assert (st.connected, st.project, st.selected_part, st.playback_active) == (True, "Harmony.rpp", None, False)
    assert st.raw["service_version"] == "0.6.0"


def test_projects_selection_and_tracks(sio, fake):
    assert [(p.name, p.active, p.addressable) for p in sio.projects()] == [
        ("Harmony.rpp", True, True), ("", False, False)]
    sel = sio.select_project("Study.rpp")
    assert fake.last == ("select_project", {"project_name": "Study.rpp"})
    assert (sel.project, sel.part) == ("Study.rpp", "Solo")
    assert sio.select_part("Cello").part == "Cello"
    track = sio.add_track("Viola", "X")
    assert fake.last == ("add_track", {"part_name": "Viola", "instrument": "X"})
    assert (track.name, track.order, track.instrument_loaded) == ("Viola", 3, False)
    assert [w.code for w in track.warnings] == ["INSTRUMENT_NOT_FOUND"]
    sio.add_track("Viola")
    assert fake.last == ("add_track", {"part_name": "Viola"})
    info = sio.score_info()
    assert info.note_range == (0.0, 8.0) and info.parts[0].selected and info.meters[0].numerator == 4


# ---------------------------------------------------------------- 位置・範囲・音高


def test_pitch_names():
    assert [pitch_number(x) for x in (60, "C4", "c4", "F#3", "Bb2", "C-1", "G9")] == [60, 60, 60, 54, 46, 0, 127]
    assert pitch_name(60) == "C4" and pitch_name(61) == "C#4" and pitch_name(0) == "C-1"
    for bad in ("H4", "C", "C10", 128, -1):
        with pytest.raises(ValueError):
            pitch_number(bad)
    for bad in (60.0, True, None):
        with pytest.raises(TypeError):
            pitch_number(bad)


def test_notes_arguments(sio, fake):
    sio.notes()
    assert fake.last == ("get_notes", {"relation": "overlaps", "include_muted": False})
    sio.notes("Cello", "Violin I", bars=(1, 2), include_muted=True)
    assert fake.last[1] == {"part_names": ["Cello", "Violin I"],
                            "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}},
                            "relation": "overlaps", "include_muted": True}
    sio.notes(bars=5, starts_in=True)
    assert fake.last[1]["range"] == {"start": {"bar": 5, "beat": 1}, "end": {"bar": 6, "beat": 1}}
    assert fake.last[1]["relation"] == "starts_in"
    sio.notes(range=(0, (3, 1, 0.5)), selected_part=True)
    assert fake.last[1] == {"selected_part": True, "relation": "overlaps", "include_muted": False,
                            "range": {"start": {"qn": 0}, "end": {"bar": 3, "beat": 1, "offset_qn": 0.5}}}
    sio.notes(at=(4, 2))
    assert fake.last[1] == {"at": {"bar": 4, "beat": 2}, "relation": "sounding_at", "include_muted": False}
    sio.notes(at=13.5)
    assert fake.last[1]["at"] == {"qn": 13.5}


@pytest.mark.parametrize("kwargs,exc", [
    ({"bars": (1, 2), "range": (0, 4)}, ValueError),
    ({"bars": (2, 1)}, ValueError),
    ({"bars": 0}, ValueError),
    ({"bars": (1.0, 2)}, TypeError),
    ({"range": (0,)}, TypeError),
    ({"range": ("a", 4)}, TypeError),
    ({"at": (4, 2), "bars": 1}, ValueError),
    ({"at": (4, 2.5)}, TypeError),
    ({"at": True}, TypeError),
])
def test_bad_arguments_send_nothing(sio, fake, kwargs, exc):
    with pytest.raises(exc):
        sio.notes(**kwargs)
    assert fake.requests == []


def test_note_list(sio):
    notes = sio.notes("Cello")
    assert isinstance(notes, list) and len(notes) == 2
    n = notes[1]
    assert (n.id, n.part, n.pitch, n.start, n.duration, n.end, n.bar, n.beat) == (2, "Cello", 43, 4.0, 2.0, 6.0, 2, 1)
    assert n.pitch_name == "G2" and n.raw["note_id"] == 2
    assert notes.project == "Harmony.rpp" and notes.range == (0.0, 8.0) and notes.warnings == []
    assert notes.parts[0].name == "Cello" and notes.meters[0].bar == 1
    with pytest.raises(AttributeError):
        n.pitch = 1                                             # 音符オブジェクトは変更できない


def test_add_item_and_play(sio, fake):
    assert sio.add_item(bars=(1, 4)) == (0.0, 16.0)
    assert fake.last == ("add_item", {"range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 5, "beat": 1}}})
    sio.add_item("Viola", range=(0, 16))
    assert fake.last[1] == {"range": {"start": {"qn": 0}, "end": {"qn": 16}}, "part_name": "Viola"}
    with pytest.raises(ValueError):
        sio.add_item("Viola")
    sio.play(bars=(1, 2))
    assert fake.last == ("play_range", {"range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}}})
    assert sio.stop() is True
    with pytest.raises(ValueError):
        sio.play()


def test_play_wait_polls_status(sio, fake):
    playing = {"ok": True, "result": {**RESULTS["get_status"], "playback_active": True}}
    fake.script = [{"ok": True, "result": RESULTS["play_range"]}, playing, playing]
    sio.play(bars=1, wait=True)
    assert [r["method"] for r in fake.requests] == ["play_range", "get_status", "get_status", "get_status"]
    fake.script = [playing] * 5
    ticks = iter(range(100))
    sio._clock = lambda: next(ticks)
    with pytest.raises(TimeoutError):
        sio.wait_playback(timeout=2)


# ---------------------------------------------------------------- 音符の指定と編集


def test_select_notes_targets(sio, fake):
    notes = sio.notes("Cello")
    assert sio.select_notes(notes) == 2
    assert fake.last[1]["targets"] == [
        {"part_name": "Cello", "target": {"note_id": 1, "expect": {"pitch": 36, "start_qn": 0.0, "duration_qn": 1.0}}},
        {"part_name": "Cello", "target": {"note_id": 2, "expect": {"pitch": 43, "start_qn": 4.0, "duration_qn": 2.0}}}]
    sio.select_notes([5, ("C4", (4, 2)), (60, 16.0, 2), {"note_id": 9}], part="Viola")
    assert fake.last[1]["targets"] == [
        {"part_name": "Viola", "target": {"note_id": 5}},
        {"part_name": "Viola", "target": {"pitch": 60, "start": {"bar": 4, "beat": 2}}},
        {"part_name": "Viola", "target": {"pitch": 60, "start_qn": 16.0, "channel": 2}},
        {"part_name": "Viola", "target": {"note_id": 9}}]
    # タプルは常に音符 1 つ（音高, 開始位置）。音符 1 つをそのまま渡してもよい。
    sio.select_notes((60, 16))
    assert fake.last[1]["targets"] == [{"target": {"pitch": 60, "start_qn": 16}}]
    sio.select_notes(notes[0])
    assert len(fake.last[1]["targets"]) == 1
    sio.select_notes()
    assert fake.last[1] == {"targets": []}
    with pytest.raises(TypeError):
        sio.select_notes(["C4"])


def test_single_edits(sio, fake):
    res = sio.insert("Cello", "C3", (5, 1), duration=2, velocity=100)
    assert fake.last == ("apply_patch", {"dry_run": False, "operations": [
        {"op": "insert", "client_note_id": "n1", "part_name": "Cello",
         "note": {"pitch": 48, "start": {"bar": 5, "beat": 1}, "duration_qn": 2, "velocity": 100, "channel": 1,
                  "muted": False}}]})
    assert (res.added, res.updated, res.inserted[0].id, res.inserted[0].part) == (1, 1, 3, "Cello")
    assert res.resolved[0].pitch == 36
    sio.insert(None, 60, 16.0)
    assert "part_name" not in fake.last[1]["operations"][0]
    assert fake.last[1]["operations"][0]["note"]["start_qn"] == 16.0

    note = Note.from_json(note_json(5, pitch=60, start=16.0))
    sio.update(note, pitch="E4", start=(5, 2), duration=0.5, velocity=90, channel=2, muted=True,
               label="直し", dry_run=True)
    assert fake.last[1] == {"dry_run": True, "label": "直し", "operations": [{
        "op": "update", "part_name": "Cello",
        "target": {"note_id": 5, "expect": {"pitch": 60, "start_qn": 16.0, "duration_qn": 1.0}},
        "set": {"pitch": 64, "start": {"bar": 5, "beat": 2}, "duration_qn": 0.5, "velocity": 90, "channel": 2,
                "muted": True}}]}
    sio.delete(7, part="Viola")
    assert fake.last[1]["operations"] == [{"op": "delete", "part_name": "Viola", "target": {"note_id": 7}}]
    sio.delete((60, 16))
    assert fake.last[1]["operations"] == [{"op": "delete", "target": {"pitch": 60, "start_qn": 16}}]
    with pytest.raises(ValueError):
        sio.update(note)


def test_patch_collects_operations_into_one_request(sio, fake):
    note = Note.from_json(note_json(2))
    with sio.patch(label="模範解答") as p:
        p.update(note, pitch=62)
        p.insert("Cello", 72, (3, 1), duration=2)
        p.delete(4, part="Cello")
        assert fake.requests == []                              # ブロックを抜けるまで送らない
    assert len(fake.requests) == 1
    ops = fake.last[1]["operations"]
    assert [o["op"] for o in ops] == ["update", "insert", "delete"] and ops[1]["client_note_id"] == "n2"
    assert fake.last[1]["label"] == "模範解答" and p.result.added == 1

    with sio.patch():                                           # 空なら送らない
        pass
    with pytest.raises(RuntimeError):
        with sio.patch() as q:                                  # 途中で例外が起きたら送らない
            q.delete(1)
            raise RuntimeError("x")
    assert len(fake.requests) == 1 and q.result is None


# ---------------------------------------------------------------- 曲の固定


def test_lock_project_adds_expectation(sio, fake):
    assert sio.lock_project() == "Harmony.rpp"
    sio.notes()
    assert fake.last[1]["expect_project_name"] == "Harmony.rpp"
    sio.insert(None, 60, 0)
    assert fake.last[1]["expect_project_name"] == "Harmony.rpp"
    for call in (sio.status, sio.projects, sio.stop):
        call()
        assert "expect_project_name" not in fake.last[1]
    sio.call("get_notes", {})                                   # call は何も付けない
    assert fake.last[1] == {}
    sio.select_project("Study.rpp")                             # 明示的に切り替えたら、固定する曲も替わる
    assert sio.locked_project == "Study.rpp"
    sio.create_project()                                        # 名前のない曲は固定できないので解除
    assert sio.locked_project is None
    sio.lock_project("X.rpp")
    sio.unlock_project()
    sio.notes()
    assert "expect_project_name" not in fake.last[1]


def test_lock_project_rejects_unnamed(sio, fake):
    fake.script = [{"ok": True, "result": {"projects": [{"project_name": "", "active": True, "addressable": False}]}}]
    with pytest.raises(ValueError):
        sio.lock_project()
    assert sio.locked_project is None


# ---------------------------------------------------------------- エラー・待機・通信断


def test_errors_become_exceptions(sio, fake):
    fake.script = [error("NOTE_MISMATCH", part_name="Cello", mismatched_fields=["pitch"])]
    with pytest.raises(NoteMismatch) as e:
        sio.delete(1)
    assert e.value.code == "NOTE_MISMATCH" and e.value.details["mismatched_fields"] == ["pitch"]
    assert e.value.request_id == fake.requests[0]["request_id"] and "NOTE_MISMATCH" in str(e.value)
    fake.script = [error("BRIDGE_UNAVAILABLE", bridge_state="unavailable")]
    with pytest.raises(BridgeUnavailable):
        sio.notes()
    fake.script = [error("RANGE_OCCUPIED")]
    with pytest.raises(SIOError) as e:
        sio.add_item(bars=1)
    assert type(e.value) is SIOError and e.value.code == "RANGE_OCCUPIED"
    fake.script = [(401, json.dumps({"request_id": None, **error("UNAUTHORIZED")}).encode())]
    with pytest.raises(SIOError) as e:
        sio.status()
    assert e.value.code == "UNAUTHORIZED"
    fake.script = [(502, b"<html>bad gateway</html>")]
    with pytest.raises(SIOConnectionError):
        sio.status()


def test_pending_is_polled_until_done(sio, fake):
    def status(state, response=None):
        return lambda env: {"ok": True, "result": {"target_request_id": env["params"]["target_request_id"],
                                                    "state": state, "response": response}}

    done = {"request_id": "x", "ok": True, "result": RESULTS["apply_patch"]}
    fake.script = [lambda env: error("REQUEST_PENDING", target_request_id=env["request_id"]),
                   status("running"), status("running"), status("succeeded", done)]
    res = sio.insert(None, 60, 0)
    assert res.added == 1
    assert [r["method"] for r in fake.requests] == ["apply_patch"] + ["get_request_status"] * 3
    first = fake.requests[0]["request_id"]
    assert all(r["params"]["target_request_id"] == first for r in fake.requests[1:])   # 送り直さず、照会だけ

    failed = {"request_id": "x", **error("WRITE_FAILED")}
    fake.script = [lambda env: error("REQUEST_PENDING", target_request_id=env["request_id"]), status("failed", failed)]
    with pytest.raises(SIOError) as e:
        sio.insert(None, 60, 0)
    assert e.value.code == "WRITE_FAILED"

    fake.script = [lambda env: error("REQUEST_PENDING", target_request_id=env["request_id"]), status("expired")]
    with pytest.raises(OutcomeUnknown):
        sio.insert(None, 60, 0)

    ticks = iter(range(0, 10000, 50))
    sio._clock = lambda: next(ticks)
    fake.script = [lambda env: error("REQUEST_PENDING", target_request_id=env["request_id"])] + [status("running")] * 5
    with pytest.raises(RequestPending) as e:
        sio.insert(None, 60, 0)
    assert e.value.details["target_request_id"] == e.value.request_id


def test_connection_refused_means_not_sent(sio, fake):
    fake.script = [ConnectionRefusedError("refused")]
    with pytest.raises(SIOConnectionError) as e:
        sio.insert(None, 60, 0)
    assert e.value.outcome_unknown is False and len(fake.requests) == 1


def test_lost_response_is_recovered_by_request_id(sio, fake):
    def status(state, response=None):
        return lambda env: {"ok": True, "result": {"target_request_id": env["params"]["target_request_id"],
                                                    "state": state, "response": response}}

    done = {"request_id": "x", "ok": True, "result": RESULTS["apply_patch"]}
    # 編集: 応答を失っても、同じ request_id の結果を照会して受け取る（送り直さない）。
    fake.script = [TimeoutError("timed out"), status("succeeded", done)]
    assert sio.insert(None, 60, 0).added == 1
    assert [r["method"] for r in fake.requests] == ["apply_patch", "get_request_status"]
    # 編集: Service が要求を知らなければ、実行されたか不明として止める。
    fake.requests.clear()
    fake.script = [TimeoutError("timed out"), status("unknown")]
    with pytest.raises(SIOConnectionError) as e:
        sio.insert(None, 60, 0)
    assert e.value.outcome_unknown is True and [r["method"] for r in fake.requests] == ["apply_patch",
                                                                                         "get_request_status"]
    # 読み取り: 同じ request_id で 1 回だけ送り直す。
    fake.requests.clear()
    fake.script = [TimeoutError("timed out"), status("unknown")]
    assert len(sio.notes()) == 2
    assert [r["method"] for r in fake.requests] == ["get_notes", "get_request_status", "get_notes"]
    assert fake.requests[0]["request_id"] == fake.requests[2]["request_id"]
    # 照会もできなければ、通信エラー。
    fake.script = [TimeoutError("timed out"), OSError("down")]
    with pytest.raises(SIOConnectionError) as e:
        sio.delete(1)
    assert e.value.outcome_unknown is True


def test_low_level_exchange_returns_the_response_as_is(sio, fake):
    env = sio.new_envelope("get_notes", {"all_parts": True})
    assert env["protocol_version"] == "2.3" and env["params"] == {"all_parts": True} and len(env["request_id"]) == 36
    assert sio.new_envelope("stop")["params"] == {}
    fake.script = [error("REQUEST_PENDING", target_request_id=env["request_id"]), (502, b"<html>")]
    status, raw = sio.exchange(env)                             # 待機も例外化もしない
    assert status == 200 and json.loads(raw)["error"]["code"] == "REQUEST_PENDING"
    assert sio.exchange(env) == (502, b"<html>")                # 同じ外枠で再送できる
    assert [r["request_id"] for r in fake.requests] == [env["request_id"]] * 2
    fake.script = [ConnectionRefusedError("refused")]
    with pytest.raises(OSError):
        sio.exchange(env)


# ---------------------------------------------------------------- 実際の HTTP


def test_real_http_transport():
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(path=self.path, auth=self.headers["Authorization"], method=body["method"])
            ok = self.headers["Authorization"] == "Bearer good"
            doc = {"request_id": body["request_id"], "ok": True, "result": RESULTS["get_status"]} if ok else \
                {"request_id": None, **error("UNAUTHORIZED")}
            raw = json.dumps(doc).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert SIO(url=url, token="good").status().connected is True
        assert seen == {"path": "/v2/rpc", "auth": "Bearer good", "method": "get_status"}
        with pytest.raises(SIOError) as e:
            SIO(url=url, token="bad").status()
        assert e.value.code == "UNAUTHORIZED"
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(SIOConnectionError) as e:
        SIO(url=url, token="good", timeout=5).status()             # サーバー停止後は接続できない
    assert e.value.outcome_unknown is False
