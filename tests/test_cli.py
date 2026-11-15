"""sio_cli の試験。コマンド行から params への変換と、偽の HTTP サーバーに対する送信・表示を確認する。"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sio_cli.__main__ import main
from sio_cli.app import Session, execute_line
from sio_cli.parse import CommandError, parse_line

NOTE = {"duration_qn": 1.0, "velocity": 80, "channel": 1, "muted": False}


def build(line: str, session: Session | None = None):
    session = session or Session("http://unused", "t", io.StringIO())
    return session.interpret(parse_line(line))


def patch(operations, label, dry_run=False):
    return ("apply_patch", {"operations": operations, "label": label, "dry_run": dry_run})


# ---------------------------------------------------------------- 変換（仕様書 4 章の例）


@pytest.mark.parametrize("line,expected", [
    ("get_status", ("get_status", {})),
    ("list_projects", ("list_projects", {})),
    ("get_score_info", ("get_score_info", {})),
    ("stop", ("stop", {})),
    ('select_project "Harmony.rpp"', ("select_project", {"project_name": "Harmony.rpp"})),
    ('select_part "piano"', ("select_part", {"part_name": "piano"})),
    ("select_part piano", ("select_part", {"part_name": "piano"})),
    ("select_part 1", ("select_part", {"part_name": "1"})),
    ('select_part "第1 バイオリン"', ("select_part", {"part_name": "第1 バイオリン"})),
    (r'select_part "a \"b\" \\c"', ("select_part", {"part_name": 'a "b" \\c'})),
    ("create_project", ("create_project", {})),
    ('add_track "Piano"', ("add_track", {"part_name": "Piano"})),
    ('add_track "Violin I" ReaSynth', ("add_track", {"part_name": "Violin I", "instrument": "ReaSynth"})),
    ('add_track Cello instrument="VSTi: ReaSynth (Cockos)"',
     ("add_track", {"part_name": "Cello", "instrument": "VSTi: ReaSynth (Cockos)"})),
    ("add_item b1..b9", ("add_item", {"range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 9, "beat": 1}}})),
    ('add_item "Violin I" b1..b9', ("add_item", {"part_name": "Violin I", "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 9, "beat": 1}}})),
    ("add_item Cello 0..16", ("add_item", {"part_name": "Cello", "range": {"start": {"qn": 0}, "end": {"qn": 16}}})),
    ("add_item range=b1..b9 part=Cello", ("add_item", {"part_name": "Cello", "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 9, "beat": 1}}})),
    ("get_request_status 23e4a562-38be-4f4a-b89d-729dd2cfa327",
     ("get_request_status", {"target_request_id": "23e4a562-38be-4f4a-b89d-729dd2cfa327"})),
    # get_notes
    ("get_notes", ("get_notes", {"selected_part": False, "relation": "overlaps", "include_muted": False})),
    ("get_notes Cello range=b1..b3", ("get_notes", {
        "part_names": ["Cello"], "selected_part": False,
        "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}},
        "relation": "overlaps", "include_muted": False})),
    ('get_notes "Violin I" Cello range=0..8.5 relation=starts_in', ("get_notes", {
        "part_names": ["Violin I", "Cello"], "selected_part": False,
        "range": {"start": {"qn": 0}, "end": {"qn": 8.5}}, "relation": "starts_in", "include_muted": False})),
    ("get_notes at=4:2", ("get_notes", {
        "selected_part": False, "at": {"bar": 4, "beat": 2}, "relation": "sounding_at", "include_muted": False})),
    ("get_notes at=4:2+0.5", ("get_notes", {
        "selected_part": False, "at": {"bar": 4, "beat": 2, "offset_qn": 0.5},
        "relation": "sounding_at", "include_muted": False})),
    ("get_notes selected=true muted=true", ("get_notes", {
        "selected_part": True, "relation": "overlaps", "include_muted": True})),
    # select_notes
    ("select_notes", ("select_notes", {"targets": []})),
    ('select_notes 5 "Violin I":65@16', ("select_notes", {"targets": [
        {"target": {"note_id": 5}},
        {"part_name": "Violin I", "target": {"pitch": 65, "start_qn": 16}}]})),
    ("select_notes 5 Cello:7 part=Viola", ("select_notes", {"targets": [
        {"part_name": "Viola", "target": {"note_id": 5}}, {"part_name": "Cello", "target": {"note_id": 7}}]})),
    ("select_notes C3@4:2 Cello:60@4:2+0.5/2 Bb3@b5", ("select_notes", {"targets": [
        {"target": {"pitch": 48, "start": {"bar": 4, "beat": 2}}},
        {"part_name": "Cello", "target": {"pitch": 60, "start": {"bar": 4, "beat": 2, "offset_qn": 0.5},
                                           "channel": 2}},
        {"target": {"pitch": 58, "start": {"bar": 5, "beat": 1}}}]})),
    ('select_notes "A:B":5', ("select_notes", {"targets": [{"part_name": "A:B", "target": {"note_id": 5}}]})),
    # 編集
    ("insert C4@20", patch([{"op": "insert", "client_note_id": "cli-1",
                             "note": {"pitch": 60, "start_qn": 20, **NOTE}}], "sio_cli: insert")),
    ("insert Cello:60@20 dur=0.5 vel=100", patch([
        {"op": "insert", "client_note_id": "cli-1", "part_name": "Cello",
         "note": {"pitch": 60, "start_qn": 20, "duration_qn": 0.5, "velocity": 100, "channel": 1,
                  "muted": False}}], "sio_cli: insert")),
    ("insert F#3@6:1 ch=3 muted=true part=Cello dry=true", patch([
        {"op": "insert", "client_note_id": "cli-1", "part_name": "Cello",
         "note": {"pitch": 54, "start": {"bar": 6, "beat": 1}, "duration_qn": 1.0, "velocity": 80,
                  "channel": 3, "muted": True}}], "sio_cli: insert", True)),
    ("update 5 pitch=64 expect=60@16", patch([
        {"op": "update", "target": {"note_id": 5, "expect": {"pitch": 60, "start_qn": 16}},
         "set": {"pitch": 64}}], "sio_cli: update")),
    ("update Cello:60@16 dur=2 dry=true", patch([
        {"op": "update", "part_name": "Cello", "target": {"pitch": 60, "start_qn": 16},
         "set": {"duration_qn": 2}}], "sio_cli: update", True)),
    ('update C4@4:2 start=4:3 pitch=E4 vel=90 ch=2 muted=false expect=C4@4:2~1 label="直し"', patch([
        {"op": "update",
         "target": {"pitch": 60, "start": {"bar": 4, "beat": 2},
                    "expect": {"pitch": 60, "start": {"bar": 4, "beat": 2}, "duration_qn": 1}},
         "set": {"pitch": 64, "start": {"bar": 4, "beat": 3}, "velocity": 90, "channel": 2, "muted": False}}],
        "直し")),
    ("update 5 start_qn=17.5 duration_qn=0.25", patch([
        {"op": "update", "target": {"note_id": 5}, "set": {"start_qn": 17.5, "duration_qn": 0.25}}],
        "sio_cli: update")),
    ("delete 60@16/2", patch([{"op": "delete", "target": {"pitch": 60, "start_qn": 16, "channel": 2}}],
                             "sio_cli: delete")),
    ("delete 5 expect=60", patch([{"op": "delete", "target": {"note_id": 5, "expect": {"pitch": 60}}}],
                                 "sio_cli: delete")),
    # 再生
    ("play_range b1..b3", ("play_range", {"range": {"start": {"bar": 1, "beat": 1},
                                                    "end": {"bar": 3, "beat": 1}}})),
    ("play_range 0..4:1", ("play_range", {"range": {"start": {"qn": 0}, "end": {"bar": 4, "beat": 1}}})),
])
def test_command_to_request(line, expected):
    assert build(line) == expected


@pytest.mark.parametrize("line", [
    "no_such_command",
    "get_status extra",
    "get_status x=1",
    "select_part",
    "select_part a b",
    'select_part "unterminated',
    "create_project x",
    "add_item",
    "add_item Cello",
    "add_item a b b1..b9",
    "add_item Cello b1..b9 part=Viola",
    "add_item Cello b1..b9 range=b1..b2",
    "add_track",
    "add_track a b c",
    "add_track a b instrument=c",
    "add_track a preset=x",
    "get_notes range=1",
    "get_notes at=x",
    "get_notes muted=yes",
    "get_notes range=b1..b3 range=b1..b2",
    "select_notes abc",
    "select_notes 60@",
    "select_notes H4@16",
    'select_notes "5"',
    "insert 60",
    "insert 60@16 ch=2/3",
    "insert 60@16/2 ch=3",
    "insert 60@16 vel=loud",
    "update 5",
    "update 5 pitch=64 velocity=1 vel=2",
    "update 5 expect=@16",
    "delete",
    "play_range",
    "play_range 4",
    "commit",
    "abort",
    "get_request_status",
    "set echo maybe",
    "set nothing 1",
    "get_status > a b",
])
def test_syntax_errors_are_not_sent(line):
    with pytest.raises(CommandError):
        build(line)


def test_comments_and_blank_lines():
    assert parse_line("") is None and parse_line("   # get_status") is None


def test_expect_project_is_added_to_project_scoped_commands():
    session = Session("http://unused", "t", io.StringIO())
    assert build('set expect_project "Harmony.rpp"', session) is None
    assert build("get_score_info", session) == ("get_score_info", {"expect_project_name": "Harmony.rpp"})
    assert build("delete 5", session)[1]["expect_project_name"] == "Harmony.rpp"
    assert build("play_range 0..4", session)[1]["expect_project_name"] == "Harmony.rpp"
    assert build("add_track Piano", session) == ("add_track", {"expect_project_name": "Harmony.rpp",
                                                                "part_name": "Piano"})
    for line in ("get_status", "list_projects", "stop", "select_project x", "create_project"):
        assert "expect_project_name" not in build(line, session)[1]
    build("set expect_project off", session)
    assert build("get_score_info", session) == ("get_score_info", {})


def test_insert_ids_are_unique_and_batch_collects_operations():
    session = Session("http://unused", "t", io.StringIO())
    assert build("insert 60@0", session)[1]["operations"][0]["client_note_id"] == "cli-1"
    assert build("begin", session) is None and session.prompt == "sio(0件)> "
    assert build("delete 3", session) is None
    assert build("insert G3@20 dur=2", session) is None
    assert session.prompt == "sio(2件)> "
    with pytest.raises(CommandError):
        build("delete 4 dry=true", session)
    method, params = build('commit dry=true label="和声の修正"', session)
    assert method == "apply_patch" and params["dry_run"] is True and params["label"] == "和声の修正"
    assert [op["op"] for op in params["operations"]] == ["delete", "insert"]
    assert params["operations"][1]["client_note_id"] == "cli-2"
    assert build("abort", session) is None and session.prompt == "sio> "


# ---------------------------------------------------------------- 送信と表示


class FakeService:
    def __init__(self):
        self.requests: list[dict] = []
        self.auth: list[str | None] = []
        self.reply = lambda env: (200, {"request_id": env["request_id"], "ok": True, "result": {"echo": env["method"]}})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                env = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append(env)
                outer.auth.append(self.headers.get("Authorization"))
                status, doc = outer.reply(env)
                raw = doc if isinstance(doc, bytes) else json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def service():
    s = FakeService()
    yield s
    s.close()


@pytest.fixture
def session(service):
    return Session(service.url, "secret-token", io.StringIO())


def run(session, *lines):
    session.out = io.StringIO()
    for line in lines:
        execute_line(session, line)
    return session.out.getvalue()


def test_envelope_and_indented_output(session, service):
    out = run(session, 'select_part "ピアノ"')
    env = service.requests[0]
    assert env["protocol_version"] == "2.3" and env["method"] == "select_part"
    assert env["params"] == {"part_name": "ピアノ"} and len(env["request_id"]) == 36
    assert service.auth == ["Bearer secret-token"]
    body, status = out.rsplit("-- ", 1)
    assert json.loads(body) == {"request_id": env["request_id"], "ok": True, "result": {"echo": "select_part"}}
    assert '\n  "ok": true,' in body                      # インデント付き
    assert status.startswith("ok / HTTP 200 / ") and "secret-token" not in out
    assert session.exit_code == 0


def test_error_response_and_exit_code(session, service):
    service.reply = lambda env: (200, {"request_id": env["request_id"], "ok": False, "error": {
        "code": "PART_NOT_FOUND", "message": "指定した名前のパートがありません。", "details": {}, "retryable": False}})
    out = run(session, "select_part x")
    assert "指定した名前のパートがありません。" in out and "-- error PART_NOT_FOUND / HTTP 200" in out
    assert session.exit_code == 1


def test_syntax_error_sends_nothing(session, service):
    out = run(session, "select_part")
    assert out.startswith("! ") and service.requests == [] and session.exit_code == 2


def test_retry_resends_same_id_and_status_defaults_to_last_request(session, service):
    run(session, "delete 5", "retry", "get_request_status", "get_request_status")
    first, again, status1, status2 = service.requests
    assert again == first
    assert status1["params"] == {"target_request_id": first["request_id"]}
    assert status2["params"] == {"target_request_id": first["request_id"]}     # 照会自身は照会先にならない
    assert status1["request_id"] != first["request_id"]


def test_pending_hint(session, service):
    service.reply = lambda env: (200, {"request_id": env["request_id"], "ok": False, "error": {
        "code": "REQUEST_PENDING", "message": "x", "details": {"target_request_id": env["request_id"]},
        "retryable": False}})
    assert "get_request_status で状態を確認できます" in run(session, "list_projects")


def test_raw_sends_params_unchanged(session, service):
    run(session, 'raw get_notes {"all_parts": true, "x": [1, "a b"]}', "raw stop", "set expect_project P",
        'raw get_score_info {}')
    assert service.requests[0]["method"] == "get_notes"
    assert service.requests[0]["params"] == {"all_parts": True, "x": [1, "a b"]}
    assert service.requests[1]["params"] == {} and service.requests[2]["params"] == {}
    assert run(session, "raw get_notes {bad").startswith("! ") and len(service.requests) == 3


def test_echo_shows_request(session, service):
    out = run(session, "set echo on", "get_notes at=4:2")
    assert ">>>" in out and "<<<" in out and '"relation": "sounding_at"' in out and "secret-token" not in out


def test_truncation_last_and_redirect(session, service, tmp_path):
    notes = [{"note_id": i} for i in range(300)]
    service.reply = lambda env: (200, {"request_id": env["request_id"], "ok": True, "result": {"notes": notes}})
    out = run(session, "set max_lines 20", "get_notes")
    assert "行を省略（last で全表示" in out and len(out.splitlines()) == 23
    full = run(session, "last")
    assert json.loads(full)["result"]["notes"] == notes
    target = tmp_path / "notes.json"
    out = run(session, f'get_notes > "{target}"')
    assert out.count("\n") == 1 and "に保存（300 音符）" in out
    assert json.loads(target.read_text(encoding="utf-8"))["result"]["notes"] == notes


def test_batch_commit_clears_after_real_apply_only(session, service):
    run(session, "begin", "delete 3", "update 4 vel=90", "commit dry=true")
    assert session.batch is not None and len(session.batch) == 2            # dry_run では保持
    run(session, "commit")
    assert session.batch is None
    dry, real = service.requests
    assert dry["params"]["operations"] == real["params"]["operations"]
    assert (dry["params"]["dry_run"], real["params"]["dry_run"]) == (True, False)
    assert real["params"]["label"] == "sio_cli: patch"


def test_non_json_body_and_http_error(session, service):
    service.reply = lambda env: (502, b"Bad Gateway")
    out = run(session, "get_status")
    assert "Bad Gateway" in out and "-- HTTP 502" in out and session.exit_code == 2
    service.reply = lambda env: (401, {"request_id": None, "ok": False, "error": {
        "code": "UNAUTHORIZED", "message": "x", "details": {}, "retryable": False}})
    assert "-- error UNAUTHORIZED / HTTP 401" in run(session, "get_status")


def test_connection_failure_is_reported():
    session = Session("http://127.0.0.1:9", "t", io.StringIO(), timeout=2)
    out = run(session, "get_status")
    assert out.startswith("! SIO Service に接続できません") and session.exit_code == 2


def test_exit_and_one_shot_mode(session, service, capsys):
    run(session, "exit")
    assert session.done
    assert main(["--url", service.url, "--token", "t", "-c", "get_status", "-c", "select_part piano"]) == 0
    assert [r["method"] for r in service.requests] == ["get_status", "select_part"]
    assert '"echo": "select_part"' in capsys.readouterr().out
    assert main(["--url", service.url, "--token", "t", "-c", "select_part"]) == 2


# ---------------------------------------------------------------- sio_client との共通部分


def test_connection_settings_come_from_config_json(service, tmp_path, capsys):
    port = service.url.rsplit(":", 1)[1]
    (tmp_path / "SIO").mkdir()
    (tmp_path / "SIO" / "config.json").write_text(json.dumps({"port": int(port), "token": "from-config"}),
                                                  encoding="utf-8")
    assert main(["--resource-path", str(tmp_path), "-c", "get_status"]) == 0
    assert service.requests[-1]["method"] == "get_status" and service.auth[-1] == "Bearer from-config"
    capsys.readouterr()
    assert main(["--resource-path", str(tmp_path / "missing"), "-c", "get_status"]) == 2
    err = capsys.readouterr().err
    assert "設定を読めません" in err and "--url と --token" in err


def test_connection_summary(service):
    from sio_cli.__main__ import connection_summary

    status = {"service_version": "0.6.0", "bridge_state": "connected", "active_project_name": "曲.rpp"}
    service.reply = lambda env: (200, {"request_id": env["request_id"], "ok": True, "result": status})
    session = Session(service.url, "secret-token", io.StringIO(), timeout=30)
    assert connection_summary(session) == f'{service.url} / Service 0.6.0 / Bridge connected / 曲 "曲.rpp"'
    assert session.timeout == 30                                 # 確認用の短いタイムアウトは元に戻す
    denied = {"code": "UNAUTHORIZED", "message": "m", "details": {}, "retryable": False}
    service.reply = lambda env: (401, {"request_id": None, "ok": False, "error": denied})
    assert "UNAUTHORIZED" in connection_summary(session) and "token" in connection_summary(session)
    down = Session("http://127.0.0.1:9", "t", io.StringIO(), timeout=2)
    assert connection_summary(down).startswith("! SIO Service")


def test_set_timeout_applies_to_the_client(session, service):
    run(session, "set timeout 7")
    assert session.timeout == 7 and session.client.timeout == 7
