"""コマンドの解釈（1 行 → RPC 要求）、送信、表示。"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, TextIO

from sio_client import SIO
from sio_client.client import PROJECT_SCOPED

from .parse import (
    CommandError,
    ParsedLine,
    Token,
    parse_bool,
    parse_coordinate,
    parse_expect,
    parse_int,
    parse_line,
    parse_number,
    parse_pitch,
    parse_position,
    parse_range,
    parse_target,
    split_part_prefix,
    start_fields,
)

# コマンド名 -> (書式, 説明, 例)。help の表示と、存在するコマンドの定義を兼ねる。
HELP: dict[str, tuple[str, str, list[str]]] = {
    "get_status": ("get_status", "接続状態、起動世代、最後に確認した対象の曲・既定パート・再生状態", ["get_status"]),
    "list_projects": ("list_projects", "開いている曲の一覧", ["list_projects"]),
    "select_project": ("select_project 名前", "その曲のタブを表に出す（表のタブの曲が対象になる）", ['select_project "Harmony.rpp"']),
    "select_part": ("select_part 名前", "既定のパートを選択（REAPER でもそのトラックだけが選択される）", ['select_part "piano"']),
    "create_project": ("create_project", "新しい曲（未保存のタブ）を作る。新しい曲が表のタブになる", ["create_project"]),
    "add_track": (
        "add_track 名前 [音源名]",
        "表のタブの曲の末尾にトラック（パート）を追加し、既定のパートにする。音源が見つからなければ空のトラックになる",
        ['add_track "Piano"', 'add_track "Violin I" ReaSynth', 'add_track Cello instrument="VSTi: ReaSynth (Cockos)"'],
    ),
    "add_item": (
        "add_item [パート名] 範囲",
        "パートに空の MIDI アイテムを追加（音符を入力できる範囲を用意）。パート名なしは選択中のパート",
        ["add_item b1..b9", 'add_item "Violin I" b1..b9', "add_item Cello 0..16"],
    ),
    "get_score_info": ("get_score_info", "表のタブの曲のパート、拍子、範囲、制約", ["get_score_info"]),
    "get_notes": (
        "get_notes [パート名 ...] [range=範囲 | at=位置] [relation=starts_in|overlaps|sounding_at] "
        "[muted=true|false] [selected=true|false]",
        "音符の取得。パート名なしは全パート。at= を付けると relation は sounding_at になる",
        ["get_notes", "get_notes Cello range=b1..b3", "get_notes at=4:2", "get_notes selected=true > notes.json"],
    ),
    "get_request_status": (
        "get_request_status [request_id]",
        "要求の状態と保存済み応答。ID を省略すると直前に送った要求を照会する",
        ["get_request_status"],
    ),
    "select_notes": (
        "select_notes [音符 ...] [part=パート名]",
        "音符を選択。引数なしは選択解除",
        ['select_notes 5 "Violin I":65@16', "select_notes C3@4:2"],
    ),
    "insert": (
        "insert [パート:]音高@開始 [dur=1] [vel=80] [ch=1] [muted=false] [part=パート名] [dry=true] [label=\"…\"]",
        "音符を 1 つ追加（apply_patch）",
        ["insert C4@20", "insert Cello:60@6:1 dur=0.5 vel=100"],
    ),
    "update": (
        "update 音符 属性=値 ... [expect=音高[@開始][~長さ]] [part=パート名] [dry=true] [label=\"…\"]",
        "音符の属性を変更（apply_patch）。属性は pitch start dur vel ch muted",
        ["update 5 pitch=64 expect=60@16", "update Cello:60@16 dur=2 dry=true", "update C4@4:2 start=4:3"],
    ),
    "delete": (
        "delete 音符 [expect=音高[@開始][~長さ]] [part=パート名] [dry=true] [label=\"…\"]",
        "音符を 1 つ削除（apply_patch）",
        ["delete 5", "delete 60@16/2"],
    ),
    "begin": ("begin", "編集の溜め込みを始める。以後の insert/update/delete は commit まで送らない", ["begin"]),
    "commit": ("commit [dry=true] [label=\"…\"]", "溜めた編集を 1 件の apply_patch として送る", ['commit label="修正"']),
    "abort": ("abort", "溜めた編集を破棄する", ["abort"]),
    "play_range": ("play_range 範囲", "範囲を 1 回再生", ["play_range b1..b3", "play_range 0..8"]),
    "stop": ("stop", "SIO 管理の再生を停止", ["stop"]),
    "raw": ("raw method [params の JSON]", "補完なしで任意の要求を送る", ['raw get_notes {"all_parts": true}']),
    "retry": ("retry", "直前の要求を同じ request_id・同じ内容で再送する", ["retry"]),
    "last": ("last", "直前の応答を行数制限なしで表示する", ["last"]),
    "set": (
        "set echo on|off / set max_lines 行数 / set expect_project 名前|off / set timeout 秒",
        "設定を変更する",
        ["set echo on", 'set expect_project "Harmony.rpp"'],
    ),
    "show": ("show", "現在の設定、接続先、直前の request_id を表示する", ["show"]),
    "help": ("help [コマンド]", "コマンド一覧、または指定コマンドの書式と例", ["help get_notes"]),
    "exit": ("exit", "終了する（quit も同じ）", ["exit"]),
}

NOTATION_HELP = """\
記法:
  位置   13.5（QN） / 4:2（小節:拍） / 4:2+0.5 / b4（第4小節の冒頭）
  範囲   開始..終了        例: b1..b3   0..8   1:1..3:1
  音符   5（note_id） / 60@16（音高@開始） / C4@4:2 / 60@16/2（チャンネル2） / Cello:5（パート指定）
  文字列 空白を含むときは "二重引用符" で囲む
  保存   行末に > ファイル名 を付けると、応答をファイルに保存する"""


class Options:
    """名前=値 と位置引数を消費し、使われなかったものをエラーにする。"""

    def __init__(self, parsed: ParsedLine) -> None:
        self.name = parsed.name
        self.args = list(parsed.args)
        self.options = dict(parsed.options)

    def take(self, *names: str) -> Token | None:
        found = [n for n in names if n in self.options]
        if len(found) > 1:
            raise CommandError(f"{found[0]}= と {found[1]}= は同じ意味です。片方だけ指定してください。")
        return self.options.pop(found[0]) if found else None

    def text(self, *names: str) -> str | None:
        tok = self.take(*names)
        return None if tok is None else tok.text

    def one_arg(self, what: str) -> Token:
        if len(self.args) != 1:
            raise CommandError(f"{self.name} には{what}を 1 つ指定してください。  書式: {HELP[self.name][0]}")
        return self.args.pop()

    def no_args(self) -> None:
        if self.args:
            raise CommandError(f"{self.name} に余分な引数があります: {self.args[0].text}  書式: {HELP[self.name][0]}")

    def finish(self) -> None:
        self.no_args()
        if self.options:
            key = next(iter(self.options))
            raise CommandError(f"{self.name} に {key}= というオプションはありません。  書式: {HELP[self.name][0]}")


class Session:
    def __init__(self, url: str | None, token: str | None, out: TextIO, timeout: float = 60.0,
                 client: SIO | None = None) -> None:
        # 通信（接続先の決定、認証、要求の外枠、送受信）は sio_client に任せる。
        # sio_cli は応答をそのまま見せる道具なので、sio_client の低水準の送受信（exchange）を使い、
        # 待機・結果の問い合わせ・例外化は行わない。
        self.client = client if client is not None else SIO(url=url, token=token, timeout=timeout)
        self.url = self.client.url
        self.out = out
        self.echo = False
        self.max_lines = 500
        self.expect_project: str | None = None
        self.batch: list[dict[str, Any]] | None = None
        self.insert_counter = 0
        self.last_envelope: dict[str, Any] | None = None
        self.last_target_id: str | None = None  # get_request_status の既定の照会先
        self.last_response: str | None = None
        self.exit_code = 0
        self.done = False

    # ------------------------------------------------------------ 表示

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    @property
    def timeout(self) -> float:
        return self.client.timeout

    @timeout.setter
    def timeout(self, seconds: float) -> None:
        self.client.timeout = seconds

    @property
    def prompt(self) -> str:
        return "sio> " if self.batch is None else f"sio({len(self.batch)}件)> "

    # ------------------------------------------------------------ 1 行の実行

    def execute(self, line: str) -> None:
        try:
            parsed = parse_line(line)
            if parsed is None:
                return
            if parsed.name == "retry":
                Options(parsed).finish()
                if self.last_envelope is None:
                    raise CommandError("再送する要求がまだありません。")
                self.send(self.last_envelope, parsed.redirect)
                return
            request = self.interpret(parsed)
            if request is None:
                if parsed.redirect is not None:
                    raise CommandError(f"{parsed.name} の結果はファイルに保存できません。")
                return
        except CommandError as e:
            self.say(f"! {e}")
            self.exit_code = 2
            return
        method, params = request
        self.send(self.client.new_envelope(method, params), parsed.redirect)

    def interpret(self, parsed: ParsedLine) -> tuple[str, dict[str, Any]] | None:
        """コマンドを (method, params) に変換する。送信しないコマンドはここで実行して None を返す。"""
        handler: Callable[[Options], tuple[str, dict[str, Any]] | None] | None = getattr(
            self, "cmd_" + parsed.name, None)
        if handler is None or parsed.name not in HELP and parsed.name != "quit":
            raise CommandError(f"未知のコマンドです: {parsed.name}  （help で一覧を表示）")
        request = handler(Options(parsed))
        if request is None:
            return None
        method, params = request
        if self.expect_project is not None and method in PROJECT_SCOPED and parsed.name != "raw":
            params = {"expect_project_name": self.expect_project, **params}
        return method, params

    # ------------------------------------------------------------ 状態と選択

    def _plain(self, o: Options, method: str) -> tuple[str, dict[str, Any]]:
        o.finish()
        return method, {}

    def cmd_get_status(self, o: Options):
        return self._plain(o, "get_status")

    def cmd_list_projects(self, o: Options):
        return self._plain(o, "list_projects")

    def cmd_get_score_info(self, o: Options):
        return self._plain(o, "get_score_info")

    def cmd_stop(self, o: Options):
        return self._plain(o, "stop")

    def cmd_select_project(self, o: Options):
        name = o.one_arg("曲名").text
        o.finish()
        return "select_project", {"project_name": name}

    def cmd_select_part(self, o: Options):
        name = o.one_arg("パート名").text
        o.finish()
        return "select_part", {"part_name": name}

    def cmd_create_project(self, o: Options):
        return self._plain(o, "create_project")

    def cmd_add_track(self, o: Options):
        if not 1 <= len(o.args) <= 2:
            raise CommandError(f"add_track にはトラック名と、必要なら音源名を指定してください。  書式: {HELP['add_track'][0]}")
        params: dict[str, Any] = {"part_name": o.args[0].text}
        instrument = o.text("instrument")
        if len(o.args) == 2:
            if instrument is not None:
                raise CommandError("音源名は 2 つ目の引数か instrument= のどちらか一方で指定してください。")
            instrument = o.args[1].text
        o.args.clear()
        o.finish()
        if instrument is not None:
            params["instrument"] = instrument
        return "add_track", params

    def cmd_add_item(self, o: Options):
        usage = f"add_item には範囲と、必要ならパート名を指定してください。  書式: {HELP['add_item'][0]}"
        args = [tok.text for tok in o.args]
        o.args.clear()
        rng, part = o.text("range"), o.text("part")
        if rng is None:
            if not args:
                raise CommandError(usage)
            rng = args.pop()
        if args:
            if part is not None:
                raise CommandError("パート名は 1 つ目の引数か part= のどちらか一方で指定してください。")
            part = args.pop(0)
        if args:
            raise CommandError(usage)
        o.finish()
        params: dict[str, Any] = {}
        if part is not None:
            params["part_name"] = part
        params["range"] = parse_range(rng)
        return "add_item", params

    def cmd_get_request_status(self, o: Options):
        if o.args:
            target = o.one_arg("request_id").text
        elif self.last_target_id is not None:
            target = self.last_target_id
        else:
            raise CommandError("照会する要求がまだありません。request_id を指定してください。")
        o.finish()
        return "get_request_status", {"target_request_id": target}

    # ------------------------------------------------------------ 取得・選択・再生

    def cmd_get_notes(self, o: Options):
        params: dict[str, Any] = {}
        if o.args:
            params["part_names"] = [tok.text for tok in o.args]
            o.args.clear()
        selected = o.text("selected")
        params["selected_part"] = parse_bool(selected, "selected") if selected is not None else False
        rng, at = o.text("range"), o.text("at")
        if rng is not None:
            params["range"] = parse_range(rng)
        if at is not None:
            params["at"] = parse_position(at, "at")
        relation = o.text("relation")
        params["relation"] = relation if relation is not None else ("sounding_at" if at is not None else "overlaps")
        muted = o.text("muted", "include_muted")
        params["include_muted"] = parse_bool(muted, "muted") if muted is not None else False
        o.finish()
        return "get_notes", params

    def _target_entry(self, tok: Token, default_part: str | None) -> dict[str, Any]:
        part, target = parse_target(tok)
        entry: dict[str, Any] = {}
        if part is not None or default_part is not None:
            entry["part_name"] = part if part is not None else default_part
        entry["target"] = target
        return entry

    def cmd_select_notes(self, o: Options):
        default_part = o.text("part")
        targets = [self._target_entry(tok, default_part) for tok in o.args]
        o.args.clear()
        o.finish()
        return "select_notes", {"targets": targets}

    def cmd_play_range(self, o: Options):
        rng = parse_range(o.one_arg("範囲").text)
        o.finish()
        return "play_range", {"range": rng}

    # ------------------------------------------------------------ 編集

    def _patch(self, o: Options, operations: list[dict[str, Any]], kind: str) -> tuple[str, dict[str, Any]]:
        dry = o.text("dry", "dry_run")
        label = o.text("label")
        o.finish()
        return "apply_patch", {
            "operations": operations,
            "label": label if label is not None else f"sio_cli: {kind}",
            "dry_run": parse_bool(dry, "dry") if dry is not None else False,
        }

    def _queue_or_send(self, o: Options, op: dict[str, Any], kind: str):
        if self.batch is None:
            return self._patch(o, [op], kind)
        if "dry" in o.options or "dry_run" in o.options or "label" in o.options:
            raise CommandError("溜め込み中は dry= と label= を commit に付けてください。")
        o.finish()
        self.batch.append(op)
        self.say(f"-- {len(self.batch)} 件目として追加しました（commit で送信、abort で破棄）")
        return None

    def _build_insert(self, o: Options) -> dict[str, Any]:
        part, body = split_part_prefix(o.one_arg("追加する音符（音高@開始）"))
        default_part = o.text("part")
        coord = parse_coordinate(body, "追加する音符")
        dur, vel, ch, muted = (o.text("dur", "duration_qn"), o.text("vel", "velocity"),
                               o.text("ch", "channel"), o.text("muted"))
        if ch is not None and "channel" in coord:
            raise CommandError("チャンネルは /チャンネル か ch= のどちらか一方で指定してください。")
        note: dict[str, Any] = {"pitch": coord["pitch"]}
        note.update({k: coord[k] for k in ("start_qn", "start") if k in coord})
        note["duration_qn"] = parse_number(dur, "dur") if dur is not None else 1.0
        note["velocity"] = parse_int(vel, "vel") if vel is not None else 80
        note["channel"] = parse_int(ch, "ch") if ch is not None else coord.get("channel", 1)
        note["muted"] = parse_bool(muted, "muted") if muted is not None else False
        self.insert_counter += 1
        op: dict[str, Any] = {"op": "insert", "client_note_id": f"cli-{self.insert_counter}"}
        if part is not None or default_part is not None:
            op["part_name"] = part if part is not None else default_part
        op["note"] = note
        return op

    def _build_target_op(self, o: Options, kind: str) -> dict[str, Any]:
        entry = self._target_entry(o.one_arg("音符"), o.text("part"))
        expect = o.text("expect")
        if expect is not None:
            entry["target"]["expect"] = parse_expect(expect)
        op: dict[str, Any] = {"op": kind}
        op.update(entry)
        if kind == "update":
            changes: dict[str, Any] = {}
            pitch, start = o.text("pitch"), o.text("start", "start_qn")
            dur, vel = o.text("dur", "duration_qn"), o.text("vel", "velocity")
            ch, muted = o.text("ch", "channel"), o.text("muted")
            if pitch is not None:
                changes["pitch"] = parse_pitch(pitch)
            if start is not None:
                changes.update(start_fields(start, "start"))
            if dur is not None:
                changes["duration_qn"] = parse_number(dur, "dur")
            if vel is not None:
                changes["velocity"] = parse_int(vel, "vel")
            if ch is not None:
                changes["channel"] = parse_int(ch, "ch")
            if muted is not None:
                changes["muted"] = parse_bool(muted, "muted")
            if not changes:
                raise CommandError("update には変更する属性を 1 つ以上指定してください（pitch start dur vel ch muted）。")
            op["set"] = changes
        return op

    def cmd_insert(self, o: Options):
        return self._queue_or_send(o, self._build_insert(o), "insert")

    def cmd_update(self, o: Options):
        return self._queue_or_send(o, self._build_target_op(o, "update"), "update")

    def cmd_delete(self, o: Options):
        return self._queue_or_send(o, self._build_target_op(o, "delete"), "delete")

    def cmd_begin(self, o: Options):
        o.finish()
        if self.batch is not None:
            raise CommandError("すでに溜め込み中です（commit で送信、abort で破棄）。")
        self.batch = []
        self.say("-- 編集の溜め込みを始めました")
        return None

    def cmd_abort(self, o: Options):
        o.finish()
        if self.batch is None:
            raise CommandError("溜め込み中ではありません。")
        self.say(f"-- {len(self.batch)} 件の編集を破棄しました")
        self.batch = None
        return None

    def cmd_commit(self, o: Options):
        if self.batch is None:
            raise CommandError("溜め込み中ではありません（begin で開始）。")
        return self._patch(o, list(self.batch), "patch")

    # ------------------------------------------------------------ raw と補助コマンド

    def cmd_raw(self, o: Options):
        raise CommandError("raw は「raw method JSON」の形で指定してください。")  # execute_raw が処理する

    def cmd_last(self, o: Options):
        o.finish()
        if self.last_response is None:
            raise CommandError("表示する応答がまだありません。")
        self.say(self.last_response)
        return None

    def cmd_show(self, o: Options):
        o.finish()
        self.say(f"url            {self.url}")
        self.say(f"echo           {'on' if self.echo else 'off'}")
        self.say(f"max_lines      {self.max_lines}")
        self.say(f"expect_project {self.expect_project if self.expect_project is not None else 'off'}")
        self.say(f"timeout        {self.timeout:g}")
        self.say(f"直前の要求     {self.last_envelope['request_id'] if self.last_envelope else '-'}")
        if self.batch is not None:
            self.say(f"溜めた編集     {len(self.batch)} 件")
        return None

    def cmd_set(self, o: Options):
        if o.options or len(o.args) != 2:
            raise CommandError(f"書式: {HELP['set'][0]}")
        name, value = o.args[0].text, o.args[1].text
        if name == "echo":
            if value not in ("on", "off"):
                raise CommandError("echo は on か off で指定してください。")
            self.echo = value == "on"
        elif name == "max_lines":
            self.max_lines = parse_int(value, "max_lines")
            if self.max_lines < 0:
                raise CommandError("max_lines は 0 以上で指定してください（0 は無制限）。")
        elif name == "expect_project":
            self.expect_project = None if (value == "off" and not o.args[1].quoted) else value
        elif name == "timeout":
            timeout = parse_number(value, "timeout")
            if timeout <= 0:
                raise CommandError("timeout は 0 より大きい秒数で指定してください。")
            self.timeout = float(timeout)
        else:
            raise CommandError(f"未知の設定です: {name}  書式: {HELP['set'][0]}")
        self.say(f"-- {name} = {value}")
        return None

    def cmd_help(self, o: Options):
        if o.options or len(o.args) > 1:
            raise CommandError(f"書式: {HELP['help'][0]}")
        if o.args:
            name = o.args[0].text
            if name not in HELP:
                raise CommandError(f"未知のコマンドです: {name}")
            usage, description, examples = HELP[name]
            self.say(f"{usage}\n  {description}")
            for example in examples:
                self.say(f"  例: {example}")
            return None
        width = max(len(name) for name in HELP)
        for name, (_, description, _) in HELP.items():
            self.say(f"  {name.ljust(width)}  {description}")
        self.say()
        self.say(NOTATION_HELP)
        return None

    def cmd_exit(self, o: Options):
        o.finish()
        if self.batch:
            self.say(f"-- 送信していない編集 {len(self.batch)} 件を破棄しました")
        self.done = True
        return None

    cmd_quit = cmd_exit

    # ------------------------------------------------------------ 送信

    def send(self, envelope: dict[str, Any], redirect: str | None = None) -> None:
        if self.echo:
            self.say(">>>")
            self.say(json.dumps(envelope, ensure_ascii=False, indent=2))
            self.say("<<<")
        self.last_envelope = envelope
        if envelope.get("method") != "get_request_status":
            self.last_target_id = envelope.get("request_id")
        started = time.perf_counter()
        try:
            status, raw = self.client.exchange(envelope)
        except OSError as e:
            self.say(f"! SIO Service に接続できません（{self.url}）: {e}")
            self.exit_code = 2
            return
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.show_response(envelope, status, raw, elapsed_ms, redirect)

    def show_response(self, envelope: dict[str, Any], status: int, raw: bytes, elapsed_ms: float,
                      redirect: str | None) -> None:
        text = raw.decode("utf-8", errors="replace")
        doc: Any = None
        try:
            doc = json.loads(text)
            text = json.dumps(doc, ensure_ascii=False, indent=2)
        except ValueError:
            pass  # JSON でない本文はそのまま表示する
        self.last_response = text

        summary, pending = f"HTTP {status}", False
        self.exit_code = 2
        if isinstance(doc, dict) and isinstance(doc.get("ok"), bool):
            if doc["ok"]:
                summary, self.exit_code = "ok", 0
            else:
                error = doc.get("error")
                error_code = error.get("code") if isinstance(error, dict) else "?"
                summary, self.exit_code = f"error {error_code}", 1
                pending = error_code == "REQUEST_PENDING"
            summary += f" / HTTP {status}"
        status_line = f"-- {summary} / {elapsed_ms:.0f} ms"

        # 溜めた編集は、実適用が成功したら空にして溜め込みを終える。
        if (envelope.get("method") == "apply_patch" and self.batch is not None and self.exit_code == 0
                and envelope["params"].get("operations") == self.batch
                and envelope["params"].get("dry_run") is False):
            self.batch = None

        if redirect is not None:
            try:
                with open(redirect, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text + "\n")
            except OSError as e:
                self.say(f"! {redirect} に保存できません: {e}")
                self.exit_code = 2
                return
            notes = doc.get("result", {}).get("notes") if isinstance(doc, dict) and doc.get("ok") else None
            extra = f"（{len(notes)} 音符）" if isinstance(notes, list) else ""
            self.say(f"{status_line} / {redirect} に保存{extra}")
        else:
            lines = text.splitlines()
            if self.max_lines and len(lines) > self.max_lines:
                self.say("\n".join(lines[: self.max_lines]))
                self.say(f"-- 以下 {len(lines) - self.max_lines:,} 行を省略（last で全表示、> ファイル で保存）")
            else:
                self.say(text)
            self.say(status_line)
        if pending:
            self.say("-- 未確定。get_request_status で状態を確認できます")


def execute_line(session: Session, line: str) -> None:
    """raw（JSON をそのまま渡す）だけは字句解析の前に処理する。"""
    stripped = line.strip()
    if stripped == "raw" or stripped.startswith("raw ") or stripped.startswith("raw\t"):
        rest = stripped[3:].strip()
        method, _, payload = rest.partition(" ")
        payload = payload.strip()
        redirect: str | None = None
        try:
            if not method:
                raise CommandError(f"書式: {HELP['raw'][0]}")
            try:
                params = json.loads(payload) if payload else {}
            except ValueError as e:
                raise CommandError(f"params を JSON として解釈できません: {e}") from None
        except CommandError as e:
            session.say(f"! {e}")
            session.exit_code = 2
            return
        session.send(session.client.new_envelope(method, params), redirect)
        return
    session.execute(line)
