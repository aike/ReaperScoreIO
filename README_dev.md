# Score IO (SIO) — 開発者向け資料

利用者向けの説明は [README.md](README.md) にあります。この文書は、SIO 自体を開発・検証する人のための資料です。

REAPER のピアノロールの音符を、外部アプリ（SA/CT など）からローカル HTTP で取得・編集するための実装です。
仕様は `SIO_Specification_ja.md`（実装仕様）と `SIO_Protocol_Specification_en.md`（クライアント向けプロトコル 2.3）です。
Service／Bridge 0.6.0 がプロトコル 2.3 を実装しています。

```
SA/CT ──HTTP──▶ SIO Service (Python) ──ファイルIPC──▶ SIO Bridge (Lua, REAPER 内に常駐)
```

| 場所 | 内容 |
|---|---|
| `bridge/sio_bridge.lua` | SIO Bridge。REAPER の Action として常駐する |
| `bridge/sio_json.lua` | Bridge 同梱の JSON ライブラリ（本プロジェクト用の書き下ろし。第三者コードなし） |
| `sio_service/` | SIO Service（FastAPI / Uvicorn / Pydantic） |
| `sio_cli/` | 動作確認用の対話ツール（REPL）。通信は `sio_client` を使う。仕様は `SIO_CLI_Specification_ja.md` |
| `sio_client/` | Python 用のクライアントライブラリ（標準ライブラリのみ）。仕様は `SIO_Client_Library_Specification_ja.md`。`pyproject.toml` はこのライブラリだけを導入する |
| `docs/sio_rpc.schema.json` | 要求（外枠と各 method の params）の JSON Schema |
| `examples/` | sio_client のサンプルプログラム（説明は `examples/README.md`） |
| `tests/test_service.py` | Service の単体試験（偽 Bridge を使用。REAPER 不要） |
| `tests/test_cli.py` | sio_cli の試験（偽の HTTP サーバーを使用） |
| `tests/test_client.py` | sio_client の試験（偽の通信路を使用） |
| `tests/test_e2e_reaper.py` | 実機受入試験（本物の REAPER を起動して仕様 11.1 を確認） |

動作確認環境: Windows 11、REAPER 7.86、Python 3.11。

## 導入

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# Bridge を REAPER に導入（Scripts/SIO へコピーし、Scripts/__startup.lua に自動起動を追加）
.\.venv\Scripts\python -m sio_service.install
```

- 導入後に REAPER を再起動すると Bridge が自動起動します。SWS は不要です（REAPER 標準の `__startup.lua` を使用）。
  SWS のグローバル起動アクションを使いたい場合は `--no-autostart` で導入し、Action 一覧の
  `Script: sio_bridge.lua` を起動アクションに設定してください。
- Bridge は Action 一覧でトグル表示されます。もう一度実行すると停止し、さらに実行すると起動します（＝ Bridge の再起動）。
- ポータブル導入の REAPER では `--resource-path <reaper.ini のあるフォルダ>` を付けます（Service も同じ）。
- 取り除くには `python -m sio_service.install --uninstall`。

## Service の起動

```powershell
.\.venv\Scripts\python -m sio_service            # 127.0.0.1:18741
.\.venv\Scripts\python -m sio_service --port 18750 --resource-path D:\ReaperPortable
```

- 設定は `<REAPERリソース>/SIO/config.json`（Windows 既定: `%APPDATA%\REAPER\SIO\config.json`）。
  初回起動時に `token` を自動生成します。クライアントはこのファイルから `token` と `port` を読みます。
- `host` はループバックのみ指定できます。CORS は既定で無効で、`"allowed_origins": ["http://localhost:5173"]`
  のように明示したオリジンだけ許可します。
- 環境変数 `SIO_TOKEN` / `SIO_PORT` / `SIO_HOST` / `SIO_REAPER_RESOURCE_PATH` で上書きできます。
- 多重起動は `SIO/service.lock` の OS ロックで拒否します（終了コード 3）。

## 手で動作を確認する（sio_cli）

Service と REAPER（Bridge）が動いている状態で起動します。標準ライブラリだけで動きます。

```
> .\.venv\Scripts\python -m sio_cli
sio> get_status
sio> select_part "piano"
sio> get_notes selected=true range=b1..b3
sio> update C4@4:2 start=4:3 dry=true
sio> insert G3@6:1 dur=2
sio> help
sio> exit
```

コマンドは 1 行で書き、省略した引数は sio_cli が既定値を補って送ります。応答はインデント付き JSON で表示します。
`set echo on` で、実際に送った要求の JSON も確認できます。

## 呼び出し例

```python
import json, os, uuid, requests

cfg = json.load(open(os.path.expandvars(r"%APPDATA%\REAPER\SIO\config.json"), encoding="utf-8"))

def rpc(method, params=None):
    r = requests.post(
        f"http://127.0.0.1:{cfg['port']}/v2/rpc",
        headers={"Authorization": f"Bearer {cfg['token']}"},
        json={"protocol_version": "2.3", "request_id": str(uuid.uuid4()), "method": method, "params": params or {}},
        timeout=(3, 60),
    )
    r.raise_for_status()
    return r.json()

print(rpc("get_status"))
print(rpc("list_projects"))
print(rpc("get_notes"))                      # 既定は全パート
print(rpc("apply_patch", {
    "expect_project_name": "Harmony.rpp",    # 曲の取り違えを検出（任意）
    "operations": [{
        "op": "update", "part_name": "Cello",
        "target": {"note_id": 5, "expect": {"pitch": 60}},   # 古い番号の誤編集を検出（任意）
        "set": {"pitch": 64}}]}))
```

## 試験

```powershell
# Service 単体、sio_cli、sio_client（REAPER 不要）
.\.venv\Scripts\python -m pytest tests/test_service.py tests/test_cli.py tests/test_client.py

# 実機受入試験。普段の設定に触れないよう、試験専用のポータブル REAPER を使う:
#   reaper.exe・Plugins フォルダ・reaper-license.rk をコピーし、普段の REAPER.ini を reaper.ini という名前で
#   同じ場所に置いたフォルダ（reaper.ini があるとポータブル導入として起動する）
$env:SIO_E2E_REAPER_DIR = "C:\path\to\reaper_portable"
.\.venv\Scripts\python -m pytest tests/test_e2e_reaper.py -v -s
```

実機試験は REAPER を起動し、自動起動 → 選択 → 取得 → 選択・編集・Undo → 失敗注入と復元 → 対応外構造 →
曲の重複名とクローズ → 範囲再生 → 2,000 音符の取得時間 → Bridge / Service 再起動、の順に確認します。
REAPER 7.86 での実測は 8 パート・2,000 音符の全取得で約 0.24 秒でした。

## 仕様との関係

実機検証で分かった REAPER の挙動（非アクティブタブでの PPQ↔QN 換算、既定で有効なループ設定、ティック表現、
同チャンネル重複音符、Undo の挙動など）と、それに基づく決定は、すべて仕様書 2.2 に反映してあります。
実装固有の補足は次のとおりです。

- 保存済み応答はメモリ上に zlib 圧縮して保持します（合計上限 256 MiB）。
- 秒経由の換算誤差を減らすため、PPQ と QN が線形に対応するアイテムでは「ソース先頭の QN ＋ ティック数 ÷ 分解能」
  の値を返します（仕様 4.1 が許す範囲）。
- 受入試験用の失敗注入として、REAPER の ExtState `SIO/test_inject`（`write_fail` / `rollback_fail`）を
  Bridge が参照します。通常は未設定です。

## 未検証・未提供

- 未検証の項目は仕様書（日本語版 11.3、英語版 12）の一覧のとおりです。
- 教材テンプレート、OpenAPI 文書（JSON Schema のみ提供）、MCP 入口は含みません。
