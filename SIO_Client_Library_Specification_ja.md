# sio_client 仕様書

- 版：0.1（実装済み。`sio_client/`）
- 更新日：2026年11月14日
- 対象：SIO Service（プロトコル2.3）、Python 3.11以降
- 関連文書：`SIO_Protocol_Specification_en.md`（プロトコルの正本）、`SIO_Specification_ja.md`

## 1. 目的

sio_clientは、自作のPythonプログラムからSIOを使うためのライブラリである。プロトコルの細部（要求の外枠、request_id、処理が長引いたときの待機、応答を失ったときの問い合わせ、取り違えの検出）をライブラリが引き受け、利用者は音楽の言葉（小節、拍、音名、パート名）でプログラムを書けるようにする。

- 依存は標準ライブラリだけとする。
- プロトコルの要求1つに、メソッド1つが対応する。ライブラリ独自の音楽的な判断（和声分析、声部の推定など）は行わない。
- プロトコルで表せることを制限しない。対応するメソッドがない使い方は、`call`で任意の要求を送れる。
- 同期処理だけを提供する。

```python
from sio_client import SIO

sio = SIO()
for note in sio.notes(bars=(1, 2)):
    print(note.part, note.pitch, note.bar, note.beat, note.duration)
```

## 2. 導入と接続

SIOのフォルダを指定して`pip install`する。導入されるのは`sio_client`だけで、他のパッケージは追加されない。

```
python3 -m pip install ~/ReaperScoreInterface
```

```python
SIO(resource_path=None, *, url=None, token=None, timeout=60.0, pending_timeout=120.0)
```

| 引数 | 既定 | 内容 |
|---|---|---|
| resource_path | OS標準のREAPERリソースフォルダ | `SIO/config.json`からportとtokenを読む場所。環境変数`SIO_REAPER_RESOURCE_PATH`でも指定可 |
| url | `http://127.0.0.1:<port>` | Serviceの基底URL |
| token | config.jsonのtoken | Bearer token。環境変数`SIO_TOKEN`でも指定可 |
| timeout | 60 | 1回のHTTP通信の待ち時間（秒） |
| pending_timeout | 120 | 処理が長引いたときに、結果を待ち続ける時間（秒）。6章 |

- urlとtokenの両方を指定した場合、config.jsonは読まない。
- config.jsonを読めなければ`SIOConfigError`。生成時には通信しないので、Serviceが起動していなくても`SIO()`は成功する。接続の確認には`status()`を使う。
- 属性：`url`、`timeout`、`pending_timeout`、`locked_project`（5.6）、`last_request_id`（直前に送った要求のID）。

## 3. 引数の書き方

### 3.1 位置

| 書き方 | 意味 | 送信するJSON |
|---|---|---|
| `13`、`13.5` | QN（四分音符を1とする、曲の先頭からの位置） | `{"qn": 13.5}` |
| `(4, 2)` | 第4小節第2拍 | `{"bar": 4, "beat": 2}` |
| `(4, 2, 0.5)` | 第4小節第2拍からQN 0.5後 | `{"bar": 4, "beat": 2, "offset_qn": 0.5}` |
| 辞書 | プロトコルのPositionをそのまま | そのまま |

小節と拍は整数。拍は拍子の分母の音符で数える（6/8拍子は1小節6拍）。真偽値、文字列、長さの合わないタプルは`TypeError`とし、何も送信しない。

### 3.2 範囲

範囲を取るメソッドは、`bars`と`range`のどちらか一方を受け取る。

| 書き方 | 意味 | 送信するrange |
|---|---|---|
| `bars=3` | 第3小節 | 第3小節の頭〜第4小節の頭 |
| `bars=(1, 4)` | **第1〜4小節（両端を含む）** | 第1小節の頭〜第5小節の頭 |
| `range=(0, 16)` | 開始〜終了。**終了は含まない** | `{"start": {"qn": 0}, "end": {"qn": 16}}` |
| `range=((1, 3), (2, 1))` | 第1小節第3拍〜第2小節の頭 | 開始と終了に3.1の書き方を使う |

プロトコルの範囲は終了を含まない。`bars`は音楽家の言い方に合わせて最後の小節を含むので、ライブラリが「最後の小節＋1の頭」に直して送る。`bars`と`range`の同時指定、1未満の小節番号、最初＞最後は`ValueError`。

### 3.3 音高

MIDIノート番号（0〜127の整数）か、音名の文字列で指定する。音名は`A`〜`G`、任意の`#`または`b`、オクターブ`-1`〜`9`で、中央CをC4＝60とする（`"C4"`、`"F#3"`、`"Bb2"`）。SIOは異名同音を区別しないので、音名は入力の便宜であり、読み取った音符の音高は常に整数で返る。`pitch_number(値)`と`pitch_name(番号)`を関数としても公開する。`pitch_name`は半音を`#`で表す。

### 3.4 音符の指定

既存の音符を指定する引数（`update`、`delete`、`select_notes`）は、次のいずれかを受け取る。

| 書き方 | 意味 | 送信するtarget |
|---|---|---|
| `Note` | 読み取った音符 | `{"note_id": 番号, "expect": {"pitch": …, "start_qn": …, "duration_qn": …}}`。パートは音符のパート |
| 整数 | note_id | `{"note_id": 番号}` |
| `(音高, 開始位置)` | 音高と開始位置による指定 | `{"pitch": …, "start_qn": …}`または`{"pitch": …, "start": …}` |
| `(音高, 開始位置, チャンネル)` | 上記にチャンネルを加える | `"channel"`を加える |
| 辞書 | プロトコルのtargetをそのまま | そのまま |

- **`Note`を渡す方法を推奨する。** ライブラリが、読み取ったときの音高・開始位置・長さを照合条件（expect）として付ける。読み取り後に人間が編集して番号がずれていた場合、別の音を書き換えずに`NoteMismatch`になる。
- 整数とタプルの指定は、`part=`でパートを指定できる。省略するとSIOの既定パートが対象になる。`Note`に`part=`を付けた場合は、`part=`を優先する。
- タプルは常に音符1つを表す。`select_notes((60, 16))`は「音高60、開始QN 16の音符」であり、note_id 60と16ではない。複数の音符はリストで渡す。

## 4. 戻り値の型

すべて変更できないオブジェクトで、元のJSONを`raw`属性に持つ。位置と長さは四分音符を1とする数値。

| 型 | 属性 |
|---|---|
| `Note` | `id`（note_id）、`part`、`pitch`、`start`、`duration`、`end`（start＋duration）、`bar`、`beat`、`offset`（拍の頭からのずれ）、`velocity`、`channel`、`muted`、`item_muted`、`track_muted`、`selected`、`pitch_name` |
| `NoteList` | `Note`のリスト（`list`の派生）。加えて`project`、`parts`、`meters`、`range`（(開始, 終了)またはNone）、`warnings` |
| `Part` | `name`、`order`、`addressable`、`selected` |
| `Project` | `name`、`active`、`addressable` |
| `Meter` | `start`、`bar`、`numerator`、`denominator` |
| `Diagnostic` | `code`、`message`、`details` |
| `Selection` | `project`、`part`（既定パート。なければNone） |
| `Status` | `connected`、`bridge_state`、`project`、`selected_part`、`playback_active`、`service_version`、`bridge_version` |
| `ScoreInfo` | `project`、`selected_part`、`parts`、`meters`、`note_range`（(開始, 終了)またはNone）、`note_range_complete`、`constraints`、`warnings` |
| `Track` | `project`、`name`、`order`、`instrument_loaded`、`instrument_name`、`warnings` |
| `PatchResult` | `project`、`added`、`updated`、`deleted`、`dry_run`、`resolved`（対象にした音符の変更前の内容。`Note`のリスト）、`inserted`（`Inserted`のリスト） |
| `Inserted` | `part`、`id`（編集後のnote_id）、`client_id`、`operation_index` |

`Note.id`は一時的な番号である。音符を追加・削除すると振り直されるので、編集のあとは`notes()`で読み取り直す。

## 5. メソッド

対象の曲は、REAPERで表に出ているタブの曲である（プロトコル仕様書4.1）。

### 5.1 状態と曲

| メソッド | 送信する要求 | 戻り値 |
|---|---|---|
| `status()` | get_status | `Status`。REAPERとつながっていなくても例外にせず、`connected`がFalseになる |
| `projects()` | list_projects | `Project`のリスト |
| `select_project(name)` | select_project | `Selection`。その曲のタブが表に出る |
| `create_project()` | create_project | `Selection`。新しい曲（未保存、名前は`""`）が対象になる |
| `score_info()` | get_score_info | `ScoreInfo` |

### 5.2 パート

| メソッド | 送信する要求 | 戻り値 |
|---|---|---|
| `select_part(name)` | select_part | `Selection`。REAPERでもそのトラックだけが選択される |
| `add_track(name, instrument=None)` | add_track | `Track`。追加したトラックが既定パートになる |
| `add_item(part=None, *, bars=None, range=None)` | add_item | 作った範囲の`(開始, 終了)` |

- `add_track`は、指定した音源が見つからなくても例外にしない（プロトコルどおり、空のトラックが追加される）。`instrument_loaded`と`warnings`で確認する。
- `add_item`は`bars`か`range`が必須。`part`を省略すると既定パートに作る。

### 5.3 読み取り

```python
notes(*parts, selected_part=False, bars=None, range=None, at=None, starts_in=False, include_muted=False)
```

| 引数 | 送信する値 |
|---|---|
| パート名なし | `part_names`を送らない（全パート） |
| パート名あり | `part_names`にその一覧 |
| `selected_part=True` | `"selected_part": true`（既定パートだけ） |
| `bars`／`range` | `range`と`"relation": "overlaps"`（範囲にかかる音符。前から伸びている音を含む） |
| `starts_in=True` | `"relation": "starts_in"`（範囲の中で始まる音符だけ） |
| `at=位置` | `at`と`"relation": "sounding_at"`（その時点で鳴っている音符） |
| 範囲の指定なし | 対象パートの全音符 |
| `include_muted` | `"include_muted"`。常に送る（既定False） |

`at`は`bars`、`range`、`starts_in`と同時に指定できない（`ValueError`）。戻り値は`NoteList`で、パートの順、note_idの順に並ぶ。

### 5.4 選択と再生

| メソッド | 送信する要求 | 戻り値 |
|---|---|---|
| `select_notes(notes=None, *, part=None)` | select_notes | 選択した音符の数 |
| `play(*, bars=None, range=None, wait=False, timeout=None)` | play_range | なし |
| `wait_playback(timeout=None)` | get_status（繰り返し） | なし |
| `stop()` | stop | SIOが始めた再生を止めたらTrue |

- `select_notes`の`notes`は、3.4の指定のリスト。指定1つをそのまま渡してもよい。省略または空のリストは選択の解除。
- `play`は`bars`か`range`が必須。`wait=True`にすると、再生開始後に`wait_playback`を呼ぶ。
- `wait_playback`は、`status().playback_active`がFalseになるまで0.1秒間隔で確認する。再生が終端まで進んだ場合も、人間や`stop()`が止めた場合も戻る。`timeout`秒を過ぎたら`TimeoutError`（再生は止めない）。

### 5.5 編集

```python
insert(part, pitch, start, duration=1.0, *, velocity=80, channel=1, muted=False, label=None, dry_run=False)
update(note, *, pitch=None, start=None, duration=None, velocity=None, channel=None, muted=None,
       part=None, label=None, dry_run=False)
delete(note, *, part=None, label=None, dry_run=False)
patch(label=None, dry_run=False)
```

- `insert`、`update`、`delete`は、それぞれ操作1件のapply_patchを送り、`PatchResult`を返す。
- `insert`の`part`は必須の引数だが、`None`を渡すと既定パートに追加する。省略された属性には上記の既定値を補って送る。
- `update`は、指定した属性だけを`set`に入れる。1つも指定しなければ`ValueError`。
- `label`はREAPERの「元に戻す」に表示される名前。省略すると送らない（SIOの既定名になる）。
- `dry_run=True`は、検証だけを行い、何も変更しない。

**複数の編集を1回にまとめる：**

```python
with sio.patch(label="模範解答") as p:
    p.update(wrong[0], pitch="A5")
    p.delete(wrong[1])
    p.insert("Soprano", 72, (3, 1), duration=2)
print(p.result.added)
```

- `p.insert`、`p.update`、`p.delete`は、`label`と`dry_run`を除いて上記と同じ引数を取り、操作を溜めるだけで送信しない。
- `with`ブロックを正常に抜けたときに、溜めた操作を1件のapply_patchとして送り、結果を`p.result`に入れる。REAPERの「元に戻す」1回分になり、1つでも失敗すれば全体が適用されない（プロトコルどおり）。
- ブロックの中で例外が起きた場合は、何も送信しない。操作が1件もなければ送信せず、`p.result`はNoneのままになる。
- 追加する音符のclient_note_idは、`n1`、`n2`…と操作の番号で自動的に付ける。`p.result.inserted`の`client_id`と`operation_index`で対応を取れる。
- `with`を使わずに、`p = sio.patch()`の後で`p.send()`を呼んでもよい。

### 5.6 曲の取り違えの防止

対象の曲は表のタブで決まるので、読み取りと編集の間に人間がタブを切り替えると、別の曲を編集してしまう。これを防ぐために、対象の曲を名前で固定できる。

| メソッド | 動作 |
|---|---|
| `lock_project(name=None)` | 以後、曲に依存する全要求に`"expect_project_name": name`を付ける。対象の曲がその名前でなければ`ProjectMismatch`になり、何も実行されない。nameを省略すると、list_projectsを呼び、いま表に出ている曲の名前を使う。固定した名前を返す |
| `unlock_project()` | 固定を解除する |

- 名前のない（未保存の）曲は固定できない（`ValueError`）。プロトコルが、名前のない曲の照合を持たないためである。
- 固定中に`select_project(name)`が成功すると、固定する曲もその曲に替える。プログラム自身が意図して切り替えたためである。
- `create_project()`が成功すると、固定を解除する（新しい曲に名前がないため）。
- 照合を付けるのは、select_part、add_track、add_item、get_score_info、get_notes、select_notes、apply_patch、play_rangeである。`status`、`projects`、`stop`、`call`には付けない。

### 5.7 任意の要求

`call(method, params=None)`は、methodとparamsをそのまま送り、resultの辞書を返す。外枠の組み立て、6章の待機と問い合わせ、エラーの例外化は他のメソッドと同じに行う。`lock_project`の照合は付けない。

### 5.8 低水準の送受信

応答そのものを確認する道具（sio_cliなど）のために、6章の処理を行わない送受信を公開する。通常のプログラムは使わない。

| メソッド | 動作 |
|---|---|
| `new_envelope(method, params=None)` | 要求の外枠（protocol_version、新しいrequest_id、method、params）を辞書で返す。送信はしない |
| `exchange(envelope)` | 外枠を1回送り、`(HTTPステータス, 応答の本文のbytes)`をそのまま返す |

- `exchange`は、待機、結果の問い合わせ、エラーの例外化、`lock_project`の照合の付加を行わない。エラー応答やJSONでない本文も、そのまま返す。
- 同じ外枠をもう一度渡すと、同じrequest_idで再送できる（プロトコル仕様書7章の再送）。
- 通信の失敗は`OSError`のまま送出する。送信前に接続を拒否された場合は`ConnectionRefusedError`（`OSError`の派生）。
- 接続先、token、`timeout`は通常のメソッドと共通である。

## 6. 待機と、応答を失ったときの扱い

編集を二重に実行しないことを最優先とする。ライブラリは、結果が分からない要求を新しいrequest_idで送り直すことをしない。

| 状況 | ライブラリの動作 |
|---|---|
| 送信前に接続を拒否された（Serviceが起動していない） | `SIOConnectionError`。要求は実行されていない（`outcome_unknown`はFalse） |
| SIOがREQUEST_PENDINGを返した（処理が長引いている） | get_request_statusで、結果が出るまで照会する。間隔は0.25秒から1秒まで延ばす。要求は送り直さない |
| 照会を`pending_timeout`秒続けても結果が出ない | `RequestPending`。要求は取り消されておらず、後で完了しうる。`request_id`属性のIDで、`call("get_request_status", …)`により照会を続けられる |
| 送信後に通信が切れた、またはタイムアウトした | 同じrequest_idの状態をget_request_statusで問い合わせる |
| 　→ 結果がある（succeeded／failed） | その結果を使う |
| 　→ 実行中（queued／running） | 上記と同じく、結果が出るまで照会する |
| 　→ Serviceが要求を知らない（unknown）、読み取りの要求 | 同じrequest_idで1回だけ送り直す |
| 　→ Serviceが要求を知らない（unknown）、REAPERの状態を変える要求 | `SIOConnectionError`（`outcome_unknown`がTrue）。実行されたかどうか分からないので、送り直さない |
| 　→ 問い合わせもできない | `SIOConnectionError`（状態を変える要求では`outcome_unknown`がTrue） |
| 照会の結果が保持期限切れ（expired）など | `OutcomeUnknown` |

「REAPERの状態を変える要求」は、select_project、select_part、create_project、add_track、add_item、select_notes、apply_patch、play_range、stopである。

`outcome_unknown`がTrueの例外と`OutcomeUnknown`を受け取ったプログラムは、同じ操作を繰り返す前に、`notes()`や`score_info()`で現在の状態を読み取り直して確認する。

## 7. 例外

| 例外 | 意味 |
|---|---|
| `SIOException` | すべての基底 |
| 　`SIOConfigError` | config.jsonを読めない |
| 　`SIOConnectionError` | Serviceと通信できない。属性`outcome_unknown`、`request_id` |
| 　`SIOError` | SIOがエラーを返した。属性`code`、`message`、`details`、`retryable`、`request_id` |
| 　　`BridgeUnavailable` | BRIDGE_UNAVAILABLE。REAPERとつながっていない。要求は実行されていない |
| 　　`RequestPending` | REQUEST_PENDING。`pending_timeout`内に結果が出なかった |
| 　　`OutcomeUnknown` | OUTCOME_UNKNOWN。実行されたかどうか分からない |
| 　　`ProjectMismatch` | PROJECT_MISMATCH。対象の曲が、固定した曲と違う |
| 　　`NoteMismatch` | NOTE_MISMATCH。音符が、読み取ったときと変わっている |
| 　　`NoteNotFound` | NOTE_NOT_FOUND |
| 　　`PartNotFound` | PART_NOT_FOUND |
| 　　`PartNotSelected` | PART_NOT_SELECTED。パートを省略したが、既定パートがない |

- 上記以外のエラーコード（RANGE_OCCUPIED、NO_TARGET_ITEM、TRANSPORT_BUSYなど）は`SIOError`として送出する。`code`で判別する。コードの一覧はプロトコル仕様書8章にある。
- 引数の書き方の誤り（3章）は、Python標準の`TypeError`／`ValueError`とし、何も送信しない。値の意味の検証（音高の範囲を除く）はSIOに任せる。
- 警告（`warnings`）は例外にしない。各結果の`warnings`属性で確認する。

## 8. 例

実行できるサンプルプログラムが`examples/`にある（`examples/README.md`）。frog_round.py（輪唱の書き込みと再生）、chord_analysis.py（和音の解析）、parallel_fifths.py（連続5度の検出と指摘）、harmony_exercise.py（課題の出題・評価・修正）の4つで、実際のREAPERで動作を確認している。

次は、和声課題を用意し、解答を評価する流れの抜粋である。

```python
from sio_client import SIO, SIOError

sio = SIO()
if not sio.status().connected:
    raise SystemExit("REAPER を起動してください。")

# 課題を用意する
sio.create_project()
sio.add_track("Soprano", instrument="ReaSynth")
sio.add_item(bars=(1, 4))                       # 追加したトラックが既定パートになっている
sio.add_track("Bass", instrument="ReaSynth")
sio.add_item(bars=(1, 4))
with sio.patch(label="課題のバス") as p:
    for bar, pitch in enumerate(["C3", "F3", "G3", "C3"], start=1):
        p.insert("Bass", pitch, (bar, 1), duration=4)

input("ソプラノを入力したら Enter を押してください。")

# 解答を読んで評価する（ここでは、音域を超えた音を探すだけ）
answer = sio.notes("Soprano", bars=(1, 4))
too_high = [n for n in answer if n.pitch > 81]
if too_high:
    sio.select_notes(too_high)                  # 問題の音を REAPER 上で示す
    first = too_high[0]
    sio.play(bars=first.bar, wait=True)         # その小節を鳴らす
    try:
        sio.update(first, pitch=first.pitch - 12)
    except SIOError as e:
        print("直せませんでした:", e.code)
```

## 9. 試験

- `tests/test_client.py`：偽の通信路を使う単体試験。引数の変換、戻り値、曲の固定、エラー、6章の各状況、低水準の送受信、実際のHTTP通信を確認する。
- `tests/test_cli.py`：sio_cliはこのライブラリの上で動くので、接続先の決定と送受信はsio_cliの試験でも確認される。
- `tests/test_e2e_reaper.py`の`test_client_library`：実際のServiceとREAPERに対して、トラックとアイテムの追加、編集、読み取り、`Note`による照合、選択、再生の待機、曲の固定を確認する。
