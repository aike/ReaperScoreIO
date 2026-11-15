"""sio_cli — SIO の動作を手で確認するための対話型ツール（REPL）。標準ライブラリだけで動く。"""

CLI_VERSION = "0.4.0"
from sio_client import PROTOCOL_VERSION  # noqa: E402,F401  （通信は sio_client に任せる）
