"""python -m sio_cli で起動する。"""

from __future__ import annotations

import argparse
import json
import sys

from sio_client import SIO, SIOConfigError, SIOConnectionError, SIOError

from . import CLI_VERSION, PROTOCOL_VERSION
from .app import Session, execute_line


def connection_summary(session: Session) -> str:
    """起動時の接続確認（応答の JSON は表示しない）。"""
    client = session.client
    saved, client.timeout = client.timeout, min(client.timeout, 5.0)
    try:
        st = client.status()
    except SIOConnectionError as e:
        return f"! {e}"
    except SIOError as e:
        hint = "（token を確認してください）" if e.code == "UNAUTHORIZED" else ""
        return f"! get_status が失敗しました: {e.code}{hint}"
    finally:
        client.timeout = saved
    return (f"{session.url} / Service {st.service_version} / "
            f"Bridge {st.bridge_state} / 曲 {json.dumps(st.project, ensure_ascii=False)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sio_cli", description="SIO の動作を確認する対話型ツール")
    parser.add_argument("--resource-path", help="REAPER のリソースフォルダ（SIO/config.json を読む場所）")
    parser.add_argument("--url", help="SIO Service の基底 URL（既定: http://127.0.0.1:<config.json の port>）")
    parser.add_argument("--token", help="Bearer token（既定: config.json の token、または環境変数 SIO_TOKEN）")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP の読取タイムアウト秒（既定 60）")
    parser.add_argument("-c", dest="commands", action="append", metavar="コマンド",
                        help="コマンドを実行して終了する（複数指定可）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure") and not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

    # 接続先と token の決定（config.json、環境変数）は sio_client と共通。
    try:
        client = SIO(args.resource_path, url=args.url, token=args.token, timeout=args.timeout)
    except SIOConfigError as e:
        print(f"! {e}", file=sys.stderr)
        print("  SIO Service を一度起動するか、--url と --token を指定してください。", file=sys.stderr)
        return 2
    session = Session(None, None, sys.stdout, client=client)

    if args.commands:
        for line in args.commands:
            execute_line(session, line)
            if session.done:
                break
        return session.exit_code

    interactive = sys.stdin.isatty()
    if interactive:
        try:
            import readline  # noqa: F401  （行編集と履歴。無い環境では標準の入力を使う）
        except ImportError:
            pass
        print(f"sio_cli {CLI_VERSION}（プロトコル {PROTOCOL_VERSION}）  help で一覧、exit で終了")
        print(connection_summary(session))
    while not session.done:
        try:
            line = input(session.prompt) if interactive else sys.stdin.readline()
        except EOFError:
            break
        except KeyboardInterrupt:
            print()
            continue
        if not interactive and line == "":
            break
        execute_line(session, line)
    return session.exit_code if not interactive else 0


if __name__ == "__main__":
    sys.exit(main())
