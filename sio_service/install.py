"""SIO Bridge を REAPER のリソースフォルダへ導入する。

    python -m sio_service.install [--resource-path DIR] [--no-autostart]
    python -m sio_service.install --uninstall

Scripts/SIO/ に Bridge と JSON ライブラリを置き、Scripts/__startup.lua に
「Action 登録 → 起動」を行う自動起動ブロックを追加する（SWS は不要）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import default_resource_path

BRIDGE_FILES = ("sio_bridge.lua", "sio_json.lua")
BEGIN = "-- >>> SIO Bridge autostart >>>"
END = "-- <<< SIO Bridge autostart <<<"
STARTUP_BLOCK = f"""{BEGIN}
do
  local path = reaper.GetResourcePath() .. "/Scripts/SIO/sio_bridge.lua"
  local f = io.open(path, "r")
  if f then
    f:close()
    local cmd = reaper.AddRemoveReaScript(true, 0, path, true)
    if cmd and cmd ~= 0 then reaper.Main_OnCommand(cmd, 0) end
  end
end
{END}
"""


def _strip_block(text: str) -> str:
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        return text
    end += len(END)
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


def install(resource: Path, autostart: bool = True) -> None:
    if not resource.is_dir():
        raise SystemExit(f"REAPER のリソースフォルダが見つかりません: {resource}")
    source = Path(__file__).resolve().parent.parent / "bridge"
    target = resource / "Scripts" / "SIO"
    target.mkdir(parents=True, exist_ok=True)
    for name in BRIDGE_FILES:
        shutil.copyfile(source / name, target / name)
        print(f"copied  {target / name}")

    startup = resource / "Scripts" / "__startup.lua"
    text = startup.read_text(encoding="utf-8") if startup.exists() else ""
    text = _strip_block(text)
    if autostart:
        if text and not text.endswith("\n"):
            text += "\n"
        text += STARTUP_BLOCK
        startup.write_text(text, encoding="utf-8", newline="\n")
        print(f"updated {startup}  (REAPER 起動時に Bridge を自動起動)")
    elif startup.exists():
        startup.write_text(text, encoding="utf-8", newline="\n")
    print("REAPER を再起動してください。")


def uninstall(resource: Path) -> None:
    startup = resource / "Scripts" / "__startup.lua"
    if startup.exists():
        text = _strip_block(startup.read_text(encoding="utf-8"))
        if text.strip():
            startup.write_text(text, encoding="utf-8", newline="\n")
        else:
            startup.unlink()
        print(f"updated {startup}")
    target = resource / "Scripts" / "SIO"
    for name in BRIDGE_FILES:
        path = target / name
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
    print("Action 一覧の登録は REAPER の Actions ウィンドウから削除できます。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sio_service.install", description="SIO Bridge を REAPER に導入する")
    parser.add_argument("--resource-path", help="REAPER のリソースフォルダ（既定: OS 標準の場所）")
    parser.add_argument("--no-autostart", action="store_true", help="__startup.lua に自動起動を追加しない")
    parser.add_argument("--uninstall", action="store_true", help="導入したファイルと自動起動を取り除く")
    args = parser.parse_args(argv)
    resource = Path(args.resource_path) if args.resource_path else default_resource_path()
    if args.uninstall:
        uninstall(resource)
    else:
        install(resource, autostart=not args.no_autostart)
    return 0


if __name__ == "__main__":
    sys.exit(main())
