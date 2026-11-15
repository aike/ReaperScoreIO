"""python -m sio_service で SIO Service を起動する。"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from . import PROTOCOL_VERSION, SERVICE_VERSION
from .app import create_app
from .config import load_config
from .ipc import ServiceLock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sio_service", description="Score IO (SIO) Service")
    parser.add_argument("--resource-path", help="REAPER のリソースフォルダ（既定: OS 標準の場所）")
    parser.add_argument("--host", help="bind するループバックアドレス（既定: 127.0.0.1）")
    parser.add_argument("--port", type=int, help="ポート番号（既定: 18741）")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("sio.service")

    try:
        config = load_config(args.resource_path, host=args.host, port=args.port)
    except (OSError, ValueError) as e:
        log.error("設定を読み込めません: %s", e)
        return 2

    lock = ServiceLock(config.sio_dir / "service.lock")
    if not lock.acquire():
        log.error("SIO Service はすでに起動しています（%s）。", config.sio_dir / "service.lock")
        return 3

    log.info("SIO Service %s (protocol %s)", SERVICE_VERSION, PROTOCOL_VERSION)
    log.info("IPC: %s", config.ipc_dir)
    log.info("設定と token: %s", config.sio_dir / "config.json")
    try:
        uvicorn.run(create_app(config), host=config.host, port=config.port,
                    log_level=args.log_level, access_log=False)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
