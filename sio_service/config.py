"""ローカル設定。<REAPERリソース>/SIO/config.json を読み、無ければ token を生成して作る。"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18741

MIB = 1024 * 1024


@dataclass
class Limits:
    request_bytes: int = 1 * MIB
    response_bytes: int = 32 * MIB
    max_operations: int = 10_000
    max_targets: int = 10_000
    max_waiting: int = 32
    retention_seconds: float = 24 * 3600
    retained_bytes: int = 256 * MIB  # 保存済み応答（圧縮後）の合計上限


@dataclass
class Timing:
    queue_wait: float = 10.0  # キュー待ちの目安
    dispatch_wait: float = 30.0  # 配送後の待機の目安
    start_timeout: float = 5.0  # Bridge の実行開始期限
    heartbeat_stale: float = 3.0  # これ以上 heartbeat が無ければ unresponsive
    abandon_after: float = 120.0  # 実行開始後に無応答のまま OUTCOME_UNKNOWN とするまで
    poll_interval: float = 0.02


@dataclass
class Config:
    resource_path: Path
    token: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed_origins: list[str] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    timing: Timing = field(default_factory=Timing)

    @property
    def sio_dir(self) -> Path:
        return self.resource_path / "SIO"

    @property
    def ipc_dir(self) -> Path:
        return self.sio_dir / "ipc"


def default_resource_path() -> Path:
    """REAPER の既定リソースフォルダ（ポータブル導入では --resource-path で指定する）。"""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "REAPER"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "REAPER"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(
    resource_path: str | os.PathLike | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> Config:
    """設定を読む。優先順位: 引数 > 環境変数 > config.json > 既定値。"""
    res = Path(resource_path or os.environ.get("SIO_REAPER_RESOURCE_PATH") or default_resource_path())
    sio_dir = res / "SIO"
    sio_dir.mkdir(parents=True, exist_ok=True)
    path = sio_dir / "config.json"

    data: dict = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: JSON オブジェクトではありません")

    changed = False
    if not isinstance(data.get("token"), str) or len(data["token"]) < 16:
        data["token"] = secrets.token_urlsafe(32)
        changed = True
    if "host" not in data:
        data["host"] = DEFAULT_HOST
        changed = True
    if "port" not in data:
        data["port"] = DEFAULT_PORT
        changed = True
    if changed:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    cfg = Config(
        resource_path=res,
        token=os.environ.get("SIO_TOKEN") or data["token"],
        host=host or os.environ.get("SIO_HOST") or data["host"],
        port=int(port or os.environ.get("SIO_PORT") or data["port"]),
        allowed_origins=[str(o) for o in data.get("allowed_origins", [])],
    )
    if not _is_loopback(cfg.host):
        raise ValueError(f"host はループバックアドレスでなければなりません: {cfg.host}")
    return cfg
