# sio_cli 仕様書

- 版：0.4（実装済み。`sio_cli/`）
- 更新日：2026年11月14日
- 対象：SIO Service（プロトコル2.3）、Python 3.11以降
- 関連文書：`SIO_Protocol_Specification_en.md`、`SIO_Specification_ja.md`、`SIO_Client_Library_Specification_ja.md`

## 1. 目的

sio_cliは、SIOの動作を人間が手で確認するための対話型ツール（REPL）である。1行のコマンドを入力すると、sio_cliがプロトコル2.3のRPC要求に組み立ててSIO Serviceへ送り、応答をインデント付きJSONで表示する。

- コマンドはJSONではなく、`select_part "piano"`のような「コマンド名＋引数」の1行で書く。
- 必須の引数は最小限にする。省略された引数は、sio_cliが既定値を補って明示的に送信する。
- request_idとprotocol_versionは自動で付ける。
- `exit`で終了する。

sio_cliは確認用の道具であり、SA/CTの代わりではない。分析や表形式の表示は行わない。引数の意味の検証はServiceに任せ、sio_cliは構文だけを検査する（エラー応答そのものを確認できるようにするため）。

## 2. 起動と終了

```
python -m sio_cli [--resource-path DIR] [--url URL] [--token TOKEN] [--timeout 秒] [-c "コマンド"]
```

| 引数 | 既定 | 内容 |
|---|---|---|
| --resource-path | OS標準のREAPERリソースフォルダ | `SIO/config.json`からportとtokenを読む場所 |
| --url | `http://127.0.0.1:<port>` | Serviceの基底URL |
| --token | config.jsonのtoken | Bearer token。環境変数`SIO_TOKEN`でも指定可 |
| --timeout | 60 | HTTPの読取タイムアウト（秒） |
| -c | なし | コマンドを1つ実行して終了する（複数指定可） |

- 依存は標準ライブラリと、同じフォルダにあるsio_clientだけとし、Serviceの仮想環境がなくても動く。
- 通信はsio_clientに任せる。接続先とtokenの決定（config.json、環境変数）、認証、要求の外枠の組み立て、送受信、音名の解釈がsio_clientと共通になる。ただしsio_cliは応答をそのまま確認する道具なので、sio_clientの低水準の送受信（`exchange`）を使う。sio_clientの通常のメソッドが行う、処理が長引いたときの待機、応答を失ったときの問い合わせ、エラーの例外化は行わない（REQUEST_PENDINGもエラー応答も、そのまま表示する）。
- 対話で起動したときは、`get_status`を1回送って接続状態を1行で表示する。接続できなくてもREPLは開始する。`-c`とスクリプト実行では行わない。
- プロンプトは`sio> `。
- `exit`で終了する。`quit`、入力の終端（Ctrl+Z／Ctrl+D）でも終了する。
- 標準入力が端末でなければ、プロンプトを出さずに1行ずつ実行する（スクリプト実行）。
- tokenは画面にもログにも出さない。

終了コード（`-c`とスクリプト実行のとき）：最後のコマンドが`ok:true`なら0、`ok:false`なら1、送信できなかった・構文エラーなら2。

## 3. コマンド行の書き方

```
コマンド名 [位置引数 ...] [名前=値 ...] [> ファイル]
```

- 空白で区切る。空白を含む値は二重引用符で囲む。引用符の中では`\"`と`\\`が使える。
- 空白を含まない文字列は引用符なしで書ける（`select_part piano`）。日本語もそのまま書ける。
- `名前=値`の形はオプション引数。順序は自由。
- 値の型は引数ごとに決まっている。名前を受け取る引数は常に文字列として扱うので、`select_part 1`は名前`"1"`を送る。真偽値は`true`／`false`で書く。
- `#`で始まる行と空行は無視する。
- 行末の`> ファイル`は、応答のJSONをそのファイルに保存し、画面には要約1行だけを出す（`raw`には使えない）。
- コマンド名とオプション名は小文字。未知のコマンド、未知のオプション、引数の過不足は送信せずにエラーを表示する。

### 3.1 位置（Position）と範囲（Range）

| 書き方 | 意味 | 送信するJSON |
|---|---|---|
| `13` `13.5` | QN（`:`のない数値は常にQNで、小節番号ではない） | `{"qn": 13.5}` |
| `4:2` | 第4小節第2拍 | `{"bar": 4, "beat": 2}` |
| `4:2+0.5` | 第4小節第2拍からQN 0.5後 | `{"bar": 4, "beat": 2, "offset_qn": 0.5}` |
| `b4` | 第4小節の冒頭（`4:1`と同じ） | `{"bar": 4, "beat": 1}` |

範囲は開始と終了を`..`でつなぐ。`1:1..3:1`、`0..8`、`b1..b3`（第1〜2小節）。開始と終了で書き方を混ぜてよい。

### 3.2 音符の指定（target）

| 書き方 | 意味 | 送信するtarget |
|---|---|---|
| `5` | note_id 5 | `{"note_id": 5}` |
| `60@16` | 音高60、開始QN 16 | `{"pitch": 60, "start_qn": 16}` |
| `60@16/2` | 上記でチャンネル2 | `{"pitch": 60, "start_qn": 16, "channel": 2}` |
| `60@4:2` `60@b5` | 開始位置を小節・拍で指定 | `{"pitch": 60, "start": {"bar": 4, "beat": 2}}` |
| `C4@16` `F#3@4:2` | 音名による音高（C4＝60） | `{"pitch": 60, "start_qn": 16}` |
| `Cello:5` `"Violin I":65@16` | パートを指定 | 外側の`part_name`に入る |

- 音名はA〜G、任意の`#`または`b`、オクターブ-1〜9。中央CをC4＝60とする。入力の便宜であり、応答の音高は数値のまま表示する。
- `@`の後ろの開始位置には3.1の位置の書き方がすべて使える。QNなら`start_qn`、小節・拍なら`start`として送る。
- パートの接頭辞は、`@`より前にある最後の`:`より前の部分（`@`がなければ全体で最後の`:`）。空白、`:`、`@`を含むパート名は引用符で囲む。
- パートを省略すると`part_name`を送らない（SIOが選択中パートを使う）。コマンドに`part=名前`を付けると、接頭辞のない全targetにそのパートを付ける。
- `expect=60@16`を付けると、targetに`"expect": {"pitch": 60, "start_qn": 16}`を加える（古い番号の取り違え検出）。`expect=60`は音高だけ、`expect=60@4:2`は小節・拍、`expect=60@16~1`のように`~長さ`を付けると`duration_qn`も照合する。

## 4. コマンド一覧

SIOのmethodと同じ名前のコマンドを用意する。apply_patchだけは、操作ごとの`insert`／`update`／`delete`に分ける。

### 4.1 状態と選択

曲に依存するコマンドは、常にREAPERで表に出ているタブの曲を対象にする。人間がタブを切り替えると、対象も変わる。

| コマンド | 送信する要求 |
|---|---|
| `get_status` | get_status `{}` |
| `list_projects` | list_projects `{}` |
| `select_project 名前` | select_project `{"project_name": 名前}`。その曲のタブがREAPERで表に出る |
| `select_part 名前` | select_part `{"part_name": 名前}`。REAPERでもそのトラックだけが選択状態になる |
| `create_project` | create_project `{}`。新しい曲（未保存のタブ）を作る。新しい曲が表のタブになる |
| `add_track 名前 [音源名]` | add_track `{"part_name": 名前}`。追加したトラックは`select_part`したのと同じ状態（既定のパート）になる。音源名を付けると`"instrument": 音源名`を加える。`instrument=音源名`とも書ける |
| `add_item [パート名] 範囲` | add_item `{"range": 範囲}`。パート名を付けると`"part_name": パート名`を加える。`part=`、`range=`とも書ける |
| `get_score_info` | get_score_info `{}` |
| `get_request_status [ID]` | get_request_status。IDを省略すると、直前に送った要求（get_request_status自身を除く）のrequest_idを使う |

例：`add_track "Violin I" ReaSynth` → `{"part_name": "Violin I", "instrument": "ReaSynth"}`。音源名を省略すると`instrument`を送らない（空のトラック）。指定した音源が見つからない場合も空のトラックが追加され、応答の`instrument_loaded`が`false`、`warnings`に`INSTRUMENT_NOT_FOUND`が入る。

例：`add_item "Violin I" b1..b9` → `{"part_name": "Violin I", "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 9, "beat": 1}}}`（第1〜8小節に空のMIDIアイテムを作る）。引数が1つならそれを範囲とみなし、`part_name`を送らない（SIOが選択中パートを使う）。

### 4.2 get_notes

```
get_notes [パート名 ...] [range=範囲 | at=位置] [relation=starts_in|overlaps|sounding_at]
          [muted=true|false] [selected=true|false]
```

| 入力 | 補って送る値 |
|---|---|
| パート名なし | `part_names`を送らない（全パート） |
| パート名あり | `part_names`にその一覧 |
| `selected=true` | `"selected_part": true`。省略時は`false`を送る |
| `relation`省略 | `at`があれば`"sounding_at"`、なければ`"overlaps"` |
| `muted`省略 | `"include_muted": false` |
| `range`／`at`省略 | 送らない（対象パートの全音符） |

例：

| 入力 | 送信するparams |
|---|---|
| `get_notes` | `{"selected_part": false, "relation": "overlaps", "include_muted": false}` |
| `get_notes Cello range=b1..b3` | `{"part_names": ["Cello"], "selected_part": false, "range": {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}}, "relation": "overlaps", "include_muted": false}` |
| `get_notes at=4:2` | `{"selected_part": false, "at": {"bar": 4, "beat": 2}, "relation": "sounding_at", "include_muted": false}` |
| `get_notes selected=true muted=true` | `{"selected_part": true, "relation": "overlaps", "include_muted": true}` |

### 4.3 select_notes

```
select_notes [target ...] [part=パート名]
```

targetを0個以上並べる。引数なしは選択解除（`{"targets": []}`）。

例：`select_notes 5 "Violin I":65@16` → `{"targets": [{"target": {"note_id": 5}}, {"part_name": "Violin I", "target": {"pitch": 65, "start_qn": 16}}]}`

### 4.4 編集（insert、update、delete）

```
insert [パート:]音高@開始[/チャンネル] [dur=1] [vel=80] [ch=1] [muted=false]
update target 属性=値 [属性=値 ...] [expect=...]
delete target [expect=...]
```

共通のオプション：`part=パート名`、`dry=true|false`（既定false）、`label="表示名"`。

それぞれ、操作1件のapply_patchを送る。

| 入力 | 補って送る値 |
|---|---|
| insertの`dur` `vel` `ch` `muted`省略 | `duration_qn: 1.0`、`velocity: 80`、`channel: 1`、`muted: false` |
| insertの`client_note_id` | `cli-1`、`cli-2`…と自動で付ける |
| `dry`省略 | `"dry_run": false` |
| `label`省略 | `"sio_cli: insert"`等、コマンド名を入れた表示名 |

updateの属性名は`pitch`、`start`、`dur`、`vel`、`ch`、`muted`。プロトコルの名前（`start_qn`、`duration_qn`、`velocity`、`channel`）でも書ける。`pitch`には音名、`start`には小節・拍も使える（`start=4:3`）。指定した属性だけを`set`に入れる（既定値は補わない）。

例：

| 入力 | 送信するoperations |
|---|---|
| `insert C4@20` | `[{"op": "insert", "client_note_id": "cli-1", "note": {"pitch": 60, "start_qn": 20, "duration_qn": 1.0, "velocity": 80, "channel": 1, "muted": false}}]` |
| `insert Cello:60@20 dur=0.5 vel=100` | 上記に`"part_name": "Cello"`、`duration_qn: 0.5`、`velocity: 100` |
| `update 5 pitch=64 expect=60@16` | `[{"op": "update", "target": {"note_id": 5, "expect": {"pitch": 60, "start_qn": 16}}, "set": {"pitch": 64}}]` |
| `update Cello:60@16 dur=2 dry=true` | `[{"op": "update", "part_name": "Cello", "target": {"pitch": 60, "start_qn": 16}, "set": {"duration_qn": 2}}]`、`dry_run: true` |
| `delete 60@16/2` | `[{"op": "delete", "target": {"pitch": 60, "start_qn": 16, "channel": 2}}]` |
| `insert G3@6:1 dur=2` | `[{"op": "insert", "client_note_id": "cli-1", "note": {"pitch": 55, "start": {"bar": 6, "beat": 1}, "duration_qn": 2, "velocity": 80, "channel": 1, "muted": false}}]` |
| `update C4@4:2 start=4:3` | `[{"op": "update", "target": {"pitch": 60, "start": {"bar": 4, "beat": 2}}, "set": {"start": {"bar": 4, "beat": 3}}}]` |

**複数の操作を1回で送る：** `begin`で編集の溜め込みを始める。以後の`insert`／`update`／`delete`は送信せずに溜め、溜めた件数を表示する。`commit [dry=true] [label="..."]`で1件のapply_patchとして送る。`abort`で破棄する。溜め込み中のプロンプトは`sio(3件)> `のように件数を示す。溜め込み中も他のコマンド（get_notes等）はそのまま実行できる。`commit dry=true`は溜めた内容を保持したまま送るので、確認後に`commit`で実適用できる。実適用が成功すると溜め込みを終える。失敗した場合は内容を保持する。

### 4.5 再生

| コマンド | 送信する要求 |
|---|---|
| `play_range 範囲` | play_range `{"range": ...}`。例：`play_range b1..b3` |
| `stop` | stop `{}` |

### 4.6 曲の取り違え検出

`set expect_project 名前`を実行すると、以後、曲に依存する全コマンド（select_part、add_track、add_item、get_score_info、get_notes、select_notes、編集、play_range）に`"expect_project_name": 名前`を付ける。`set expect_project off`で解除。既定はoff。

### 4.7 任意の要求（raw）

```
raw method [paramsのJSON]
```

paramsをJSONのまま送る。補完は行わない。不正な要求に対するエラー応答を確認するために使う。JSONを省略すると`{}`。

例：`raw get_notes {"all_parts": true}` → INVALID_REQUESTの応答を確認できる。

### 4.8 REPLの補助コマンド

| コマンド | 内容 |
|---|---|
| `help [コマンド]` | コマンド一覧、または指定コマンドの書式と例 |
| `exit` | 終了（`quit`も同じ） |
| `retry` | 直前の要求を、同じrequest_id・同じ内容で再送する（再送規則の確認用） |
| `last` | 直前の応答をもう一度、行数制限なしで表示する |
| `set 名前 値` | 設定を変更する（下表） |
| `show` | 現在の設定、接続先URL、直前のrequest_idを表示する |

| 設定 | 既定 | 内容 |
|---|---|---|
| echo | off | onにすると、送信する要求のJSONを応答の前に表示する |
| max_lines | 500 | 応答の表示行数の上限。0は無制限 |
| expect_project | off | 4.6 |
| timeout | 60 | HTTPの読取タイムアウト（秒） |

## 5. 表示

- 応答は外枠（`request_id`、`ok`、`result`または`error`）ごと、インデント2のJSONで表示する。非ASCII文字はエスケープしない。キーの順序は応答のまま。
- JSONの後に、状態を1行で表示する。例：`-- ok / HTTP 200 / 42 ms`、`-- error PART_NOT_FOUND / HTTP 200 / 12 ms`。
- 応答がmax_linesを超える場合は、先頭max_lines行を表示し、`-- 以下 39,480 行を省略（last で全表示、> ファイル で保存）`と表示する。省略はJSONの外に書き、表示したJSON自体は書き換えない。
- `echo on`のときは、要求のJSONを`>>>`、応答を`<<<`の見出しで区別する。Authorizationヘッダーは表示しない。
- JSONでない応答本文は、そのまま表示する。
- 送信しなかった場合（構文エラー等）や接続できなかった場合は、`! `で始まる1行のメッセージを表示する。JSONは表示しない。

### 5.1 REQUEST_PENDINGの扱い

REQUEST_PENDINGが返ったら、応答をそのまま表示し、`-- 未確定。get_request_status で状態を確認できます`と案内する。自動では再送も照会もしない。`get_request_status`（ID省略）は直前の要求を照会するので、そのまま続けて入力できる。`retry`は同じrequest_idで待機に合流する。

## 6. 対話の例

```
sio> get_status
{
  "request_id": "5a0f...",
  "ok": true,
  "result": {
    "protocol_version": "2.3",
    "bridge_state": "connected",
    ...
  }
}
-- ok / HTTP 200 / 3 ms
sio> select_part "piano"
{
  "request_id": "91c2...",
  "ok": true,
  "result": {
    "project_name": "Harmony.rpp",
    "selected_part_name": "piano"
  }
}
-- ok / HTTP 200 / 61 ms
sio> get_notes selected=true range=b1..b3 > notes.json
-- ok / HTTP 200 / 74 ms / notes.json に保存（18 音符）
sio> update 5 pitch=E4 expect=C4@16 dry=true
...
sio> begin
sio(0件)> delete 3
sio(1件)> insert G3@6:1 dur=2
sio(2件)> commit label="和声の修正"
...
sio> exit
```

## 7. 範囲外

- 応答の表・楽譜表示、音符の分析。
- 複数行にわたるコマンド、変数、条件分岐、繰り返し。
- Service／Bridgeの起動と停止。

## 8. 実装方針

- 配置：`sio_cli/`パッケージ、`python -m sio_cli`で起動。
- HTTPは`urllib.request`。行編集と履歴は`readline`が使える環境で有効にし、なければ標準の入力を使う。
- 構成：コマンド行の字句解析、コマンドごとの「引数→params」変換表、送信と表示、の3部に分ける。変換表は`help`の内容と同じ定義から生成し、書式と実際の動作がずれないようにする。
- 試験：`tests/test_cli.py`。コマンド行からparamsへの変換（4章の例をそのまま試験表にしている）と、偽のHTTPサーバーに対する送信・表示を確認する。
