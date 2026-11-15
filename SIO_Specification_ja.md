# Score IO（SIO）仕様書

- 版：2.3
- 更新日：2026年11月14日
- 対象：REAPER 7.x、Lua ReaScript、Python 3.11以降
- 状態：プロトコル2.3を実装し（Service／Bridge 0.6.0）、REAPER 7.86で実機検証済み。未検証の項目は11.3を参照。
- 外部プロトコルの対応文書：`SIO_Protocol_Specification_en.md`、プロトコル2.3。

**文書の分担：** クライアントから見える契約（HTTP、要求・応答の形式、エラー、上限値）は英語プロトコル仕様書を正本とする。本書は実装方法と内部設計を定め、外部契約は要約だけを載せる。両者が食い違う場合、外部契約は英語版、内部実装は本書に従う。

## 1. 目的と責務

SIOは、REAPERのピアノロールに入力された複数パートの音符を、外部アプリから取得・編集するローカルソフトウェアである。音符取得のたびに人間がREAPERでトラックを選択したり、Actionを起動したりする必要はない。

| 構成要素 | 責務 |
|---|---|
| REAPER | 音符入力、手動編集、音源、再生、プロジェクト保存 |
| SIO Bridge（Lua） | 常駐し、REAPER APIで要求を実行する |
| SIO Service（Python） | HTTP API、要求配送、入力検証、応答の返却 |
| SA（Score Analyzer） | 分析条件、楽曲分析、問題箇所の説明 |
| CT（Composition Teacher）等 | 課題、評価、指導、対話履歴 |

SIOはREAPER専用。他DAW用の抽象化層は作らない。MCPは任意の追加入口であり、本体の実装に必要としない。

### 1.1 対象外

自然言語解釈、和声分析、作曲、教育的評価、記譜復元、異名同音・声部・調性の推定、オーディオ処理、リアルタイム演奏イベント中継、自動保存、REAPER起動・プロジェクト自動オープン、プラグインのプリセット切り替え・パラメータ操作は行わない。

音符の開始・終了と音高はピアノロールの値を使用する。「鳴っている」とは音符区間内であることを指し、サステイン、音源のリリース、MIDI FX、音量・ソロ等から実際の発音を推定しない。

### 1.2 設計の基本方針

- スナップショット、リビジョン番号、取得データの期限・保存表を持たない。
- 対象の曲は、常にREAPERのアクティブなタブ（表に出ているタブ）の曲とする。SIOは曲の選択状態を持たない。select_project(project_name)はそのタブを表に出す。各要求にproject_nameを繰り返さない。
- select_part(part_name)で、曲ごとの既定パートを選択する。
- GUIDやテイク識別子を外部に公開しない。複数テイクは未対応とする。
- update/deleteは`note_id`、または`pitch + start_qn`で対象を指定する。
- note_idはパート内の一時的な音符番号。取得後に編集されない状況で使用する。
- 1トラック内に複数のMIDIアイテムを置くことを許可する。
- サーバーに状態を持たない範囲で、取り違えを検出する任意の照合（expect_project_name、targetのexpect）を提供する。

### 1.3 版の履歴

| 版 | 内容 |
|---|---|
| 1.0 | スナップショットとリビジョンを用いる初版。廃止。 |
| 2.0 | スナップショット、リビジョン、GUID参照、複数テイク対応を廃止。`/v2/rpc`。 |
| 2.1 | 選択方式（select_project、select_part）を追加。2.0の要求形式とは非互換。 |
| 2.2 | 実機検証の結果を反映し、下記を変更。2.1の要求形式とは非互換。 |
| 2.3 | 対象の曲をアクティブなタブの曲に変更（本節の末尾）。要求形式は同じだが、意味が2.2と異なる。 |

2.2の変更点：

- get_notesの既定対象を常に全パートとし、all_partsを廃止、selected_partを追加（6.3）。
- 名前のないトラックをパートとして扱わない（3.1）。EMPTY_PART_NAMEを廃止しUNNAMED_TRACKS_IGNOREDを追加。
- アイテム境界の完全に外にある音符は除外して警告する（3.2）。
- 「ループ有効」の拒否条件を、実際にソースが繰り返される場合に限定（3.2）。
- 座標targetに任意のchannel、両targetに任意のexpectを追加（4.3）。NOTE_MISMATCHを追加。
- 曲に依存する要求に任意のexpect_project_nameを追加（6.2）。PROJECT_MISMATCHを追加。
- 編集・選択・再生の結果にproject_nameを追加。apply_patchの結果にresolvedとinsertedを追加し、refresh_requiredを廃止（7.2）。
- stopを選択状態と無関係にした（8）。get_statusにplayback_activeを追加。
- ティックで表現できない位置の扱い、同チャンネルの重複音符の扱いを定義（4.1、7.1.5）。
- エラーコードの割り当て、HTTP 400と200の境界、details、retryableを定義（9.3）。UNAUTHORIZEDを追加、AMBIGUOUS_TARGET_ITEMを廃止。
- IPCのclaimと取消、応答保持の合計上限、設定ファイルの場所、自動起動方法を定義（2、9、10）。

2.2（改訂1）の追加：要求中の開始位置を、start_qnの代わりにstart（Position。小節・拍で書ける）で指定できる（4.3）。追加のみの変更で、既存の2.2の要求はそのまま有効なため、protocol_versionは`2.2`のままとする。

2.2（改訂2）の追加：曲を作るcreate_project、トラックを追加するadd_track、空のMIDIアイテムを追加するadd_itemを追加（6.5）。エラーPART_ALREADY_EXISTS、RANGE_OCCUPIEDと診断INSTRUMENT_NOT_FOUNDを追加。追加のみの変更で、protocol_versionは`2.2`のままとする。0.5.0より前のServiceは、これらのmethodにUNKNOWN_METHODを返す。

2.3の変更点：

- SIO独自の曲の選択状態を廃止し、曲に依存する要求は常にREAPERのアクティブなタブの曲を対象とする（3.1）。人間がタブを切り替えれば対象も変わる。単一曲の自動選択規則は廃止。
- select_projectは、指定した曲のタブを表に出す操作とする。管理中の再生は先に停止する。録音中はTRANSPORT_BUSY。
- 既定パートは曲ごとに覚える。曲を切り替えても解除しない（3.1）。
- select_partは、REAPER上でもそのトラックだけを選択状態にする。add_trackは、追加したトラックを同じ方法で既定パートにし、selected_part_nameを返す。人間によるREAPERのトラック選択は、引き続き既定パートを変えない（3.1）。
- get_statusのselected_project_nameをactive_project_nameに変更。list_projectsのselectedを廃止。
- PROJECT_NOT_SELECTEDとPROJECT_NOT_ACTIVEを廃止。play_rangeに別途のタブ切替は不要。
- 非アクティブなタブの曲は、表に出さない限り取得・編集できない。

HTTPは引き続き`/v2/rpc`を使用する。protocol_versionは`2.3`とし、完全一致で照合する。2.2の要求はVERSION_MISMATCHになる。

## 2. システム構成

SA/CT → ローカルHTTP → SIO Service → ファイルIPC → REAPER内のBridge、の順に接続し、応答は逆経路で返す。REAPERのデータを正本とする。自然言語の依頼はSA/CTが構造化要求に変換する。

| 項目 | 方針 |
|---|---|
| Service | Python、FastAPI、Uvicorn、Pydantic。版は`requirements.txt`で固定する |
| Bridge | Lua ReaScriptと同梱JSONライブラリ |
| HTTP | `127.0.0.1:18741`、ポート変更可 |
| IPC | UTF-8 JSONファイル |
| 自動起動 | REAPER標準の`Scripts/__startup.lua`（10.2） |
| 対応範囲 | 1個のREAPERプロセス、複数のプロジェクトタブ |

Serviceの多重起動はOSのファイルロック（`SIO/service.lock`）、Bridgeの多重起動はREAPER内の状態（ExtState）で防止する。

### 2.1 設定ファイル

設定は`<REAPERリソースフォルダ>/SIO/config.json`に置く。Serviceは初回起動時にtokenを生成して作成する。クライアントはこのファイルからportとtokenを読む。

| 項目 | 内容 |
|---|---|
| host | bindするアドレス。ループバック以外は起動を拒否する。既定`127.0.0.1` |
| port | 既定18741 |
| token | Bearer token。16文字以上。無ければ自動生成 |
| allowed_origins | CORSを許可するオリジンの配列。既定は空（CORS無効） |

REAPERリソースフォルダの既定は、Windowsが`%APPDATA%\REAPER`、macOSが`~/Library/Application Support/REAPER`、Linuxが`~/.config/REAPER`。ポータブル導入ではServiceの起動引数`--resource-path`または環境変数`SIO_REAPER_RESOURCE_PATH`で指定する。`SIO_TOKEN`、`SIO_PORT`、`SIO_HOST`でも上書きできる。

### 2.2 脅威モデル

Bearer tokenとループバックbindが防ぐのは、ブラウザ上のページ、他のユーザー、他のホストからの要求である。同じユーザーで動くローカルプロセスは、config.jsonを読め、IPCディレクトリにも直接書けるため、信頼する前提とする。tokenはIPCを保護しない。

## 3. プロジェクト、パート、アイテム

### 3.1 対象の曲と既定パート

**曲に依存する要求は、常にREAPERのアクティブなタブ（表に出ているタブ）の曲を対象とする。** SIOは曲の選択状態を持たない。対象は、Bridgeが要求の実行を始める時点で`EnumProjects(-1)`により求める。

| 要求 | 引数 | 動作 |
|---|---|---|
| select_project | project_name | その名前の開いている曲のタブを表に出す。以後の要求の対象になる |
| select_part | part_name | 対象の曲のトラックを、その曲の既定パートとして選択。REAPER上でもそのトラックだけを選択状態にする |

select_projectのproject_nameはREAPERが返すプロジェクト表示名そのもの（通常は拡張子込み）。select_partのpart_nameは対象トラック名そのもの。大文字小文字・空白・Unicode表現を含む完全一致で検索し、パス化・拡張子除去・正規化をしない。

**対象の曲：**

- タブを表に出す操作はすべて対象を変える。select_project、create_project、人間のタブ切替、REAPERでの曲のオープン・クローズが該当する。
- したがって、人間や他のクライアントが、あるクライアントの要求と要求の間に対象を変えることがある。取り違えを検出したいクライアントはexpect_project_name（6.2）を使う。
- 名前のない未保存の曲も、アクティブであれば対象になる。返却するproject_nameは空文字列`""`。
- 名前変更・Save Asでは対象は変わらない。返却名は現在名。
- 対象の曲が閉じられたら、REAPERが次に表に出したタブの曲が対象になる。
- 開いている曲がなければNO_PROJECT_OPEN。REAPERは通常1個以上のタブを持つため、通常は発生しない。

**select_project：**

- 対象がすでにアクティブなら何もしない。そうでなければ、管理中の再生を停止し（8章のstopと同じ処理）、`SelectProjectInstance`でタブを切り替え、アクティブになったことを確認する。確認できなければINTERNAL_ERROR。
- アクティブな曲が録音中なら、切り替えずにTRANSPORT_BUSY。
- 失敗した場合はタブを切り替えない。以前の対象のままになる。
- 名前のない曲と、同名の曲が複数あるものは、SIOからは表に出せない。人間がタブを表に出す。曲のファイルを開くことはしない。

**既定パート：**

- 既定パートは、開いている曲ごとに1つ覚える（Bridge内の`{曲の参照, トラックの参照}`の一覧）。別のタブが表に出ると、その曲の既定パート（なければ未選択）が使われ、元のタブに戻ると元の既定パートに戻る。
- 既定パートを設定するのは、select_partと、add_track（追加したトラックを既定パートにする。6.5）だけである。
- **REAPERのトラック選択との連動は一方向とする。** select_partまたはadd_trackが既定パートを設定するとき、`SetOnlyTrackSelected`でREAPER上でもそのトラックだけを選択状態にする（他のトラックの選択は解除する）。SIOが操作するパートを人間が画面で確認できるようにするためである。逆方向には連動しない。人間がREAPERで別のトラックを選択しても（複数選択しても）、既定パートは最後に成功したselect_partまたはadd_trackのもののまま変わらない。この点は、アクティブなタブだけで対象が決まる曲の扱いと異なる。
- REAPERはトラックを複数選択できるが、既定パートは曲ごとに常に1つ以下である。
- REAPERのトラック選択を変えるのは、この2つの要求の実行時だけである。タブを切り替えて戻ったとき等に、選択状態を復元しない。
- トラックの内部参照で保持し、名前変更後も同じトラックを維持する。返却名は現在名。GUIDを外部に公開しない。
- トラックの削除、改名による重複名・空名、曲のクローズで、その曲の既定パートを解除する。
- Bridge/Service再起動時は全曲の既定パートを解除する。Service世代の変更はservice.jsonでBridgeへ伝える。
- select_partに失敗した場合、以前の有効な既定パートもREAPERのトラック選択も変更しない。
- アクティブな曲と既定パートの有効性は、要求の実行開始時とheartbeat周期ごとに求め直す。

アクティブなタブと既定パートはREAPER全体で1組の共有状態であり、クライアント別セッションは設けない。

空の要求名はINVALID_REQUEST、0件一致はPROJECT_NOT_FOUND/PART_NOT_FOUND、複数一致はAMBIGUOUS_PROJECT/AMBIGUOUS_PART。list_projectsは空名・重複名の曲も列挙し、get_score_infoは重複名のパートも列挙して、名前で指定可能か（addressable）を示す。

要求の対象はBridgeで実行を始める時点の状態から解決する。select_project、select_partも他のBridge要求と直列実行する。タブの切替が全体に影響するため、複数クライアントで異なる曲を同時操作する用途は本版の対象外とする。起動世代は通信管理用であり、音符のリビジョンではない。

### 3.2 MIDIデータ

- パート内の複数MIDIアイテムを統合し、曲全体のQN座標で扱う。1個には制限しない。
- MIDIを含むアイテムはテイク数が1であること。複数テイクなら、アクティブテイクを選ばずエラーにする。
- 単一テイクの内部構造やアイテム境界はBridgeだけが扱う。
- MIDI以外のアイテム（空のアイテムを含む）は無視し、件数を警告する。
- 同時刻・同音高の別音符は統合しない。休符は送信しない。チャンネルから声部を推定しない。

| 状態 | 診断コード | 動作 |
|---|---|---|
| MIDIを含むアイテムが複数テイクを持つ | MULTIPLE_TAKES | 対象パートの取得・編集を拒否（UNSUPPORTED_TAKES） |
| ソースがループ再生されている | LOOPED_MIDI_ITEM | 拒否（UNSUPPORTED_ITEM） |
| 共有・プールされたMIDIソース | POOLED_MIDI_SOURCE | 拒否（UNSUPPORTED_ITEM） |
| 固定レーン、フリー配置のトラック | UNSUPPORTED_LANES | 拒否（UNSUPPORTED_ITEM） |
| 同一トラックで重なったMIDIアイテム | OVERLAPPING_ITEMS | 拒否（UNSUPPORTED_ITEM） |
| テイク再生速度が1以外 | UNSUPPORTED_PLAYRATE | 拒否（UNSUPPORTED_ITEM） |
| アイテム境界をまたぐ音符 | NOTE_OUTSIDE_ITEM | 拒否（UNSUPPORTED_ITEM） |
| アイテム境界の完全に外にある音符 | NOTES_OUTSIDE_ITEM_IGNORED | その音符を除外して警告。付番もしない |
| 非ゼロのソース開始オフセット | SOURCE_OFFSET_READ_ONLY | 読取可（警告）、編集は拒否（UNSUPPORTED_ITEM） |
| MIDI以外のアイテム | AUDIO_ITEMS_IGNORED | 無視して件数を警告 |
| 名前のないトラック | UNNAMED_TRACKS_IGNORED | パートとして扱わず件数を警告 |
| 重複したパート名 | DUPLICATE_PART_NAME | 名前で指定不可。全パート取得はAMBIGUOUS_PART |
| ROLLBACK_FAILEDによる書込禁止中 | WRITE_BLOCKED | get_score_infoのconstraintsに記載 |

判定の詳細：

- **ループ：** REAPERの既定では新規MIDIアイテムのループ設定（`B_LOOPSRC`）が有効である。設定値だけでは拒否しない。設定が有効で、かつアイテム終端がソース終端（ソース先頭のQN＋ソース長）を超え、実際に繰り返される場合だけ拒否する。
- **境界外の音符：** アイテムの端を縮めると、ソース内に境界外の音符が残る。音符全体がアイテム範囲の外（終了がアイテム開始以前、または開始がアイテム終了以後。epsilon_qnで判定）にあるものは鳴らないので除外し、件数を警告する。除外した音符はnote_idを持たず、target解決・編集・選択の対象にならず、編集時も内容を変更しない。境界をまたぐ音符は、再生時に切り詰められてピアノロールの値と一致しないため、パートごと拒否する。
- **プール：** `GetMediaSourceType`が`MIDIPOOL`を返すソースをプールと判定する。
- **ソース開始オフセット：** `D_STARTOFFS`が0でない、またはソース先頭（PPQ 0）の位置がアイテム開始と一致しない場合。
- **テンポ無視・時間タイムベース：** PPQとQNが線形に対応しないテイクも、4.1の秒経由の換算で位置を求める。個別の診断は設けない。

未対応の対象を黙って省略しない。除外・無視したものは必ず警告する。確実に扱えない状態では編集を拒否する。

**教材の条件：** 通常レーン、非ループ、非共有、単一テイクのMIDIアイテムを使用する。パートのトラックには一意な名前を付ける。弱起（アウフタクト）は未対応なので、弱起の課題は第1小節を休符で埋めて完全小節から始める。同一チャンネル・同音高の音符を重ねない。

## 4. 時間と音符の指定

### 4.1 時間座標

正規座標はプロジェクト時刻0秒を原点とする四分音符単位のQN。四分音符1個＝1.0。JSONの有限な倍精度数値を使う。

- start_qnは0以上、duration_qnは0より大きい。
- 終了はstart_qn + duration_qn。区間は開始を含み、終了を含まない。
- グリッド（拍や音価）への丸めは行わない。
- epsilon_qn＝1e-9。この差以内の境界は等しいとみなす。

**PPQとQNの換算：** PPQ分解能を固定値と仮定せず、REAPERの変換APIを使う。ただし`MIDI_GetProjQNFromPPQPos`と`MIDI_GetPPQPosFromProjQN`は使用しない。この2つは、非アクティブなタブのテイクに対してもアクティブな曲のテンポ地図で換算する（REAPER 7.86で確認）。必ず秒を経由し、曲を明示して換算する。2.3では対象が常にアクティブな曲なので差は現れないが、要求の実行中に前提が崩れても誤らないよう、この換算方法を維持する。

- PPQ→QN：`TimeMap2_timeToQN(proj, MIDI_GetProjTimeFromPPQPos(take, ppq))`
- QN→PPQ：`MIDI_GetPPQPosFromProjTime(take, TimeMap2_QNToTime(proj, qn))`

秒経由の換算には1e-15程度の浮動小数誤差が乗る。返却値はこの誤差を含み得るので、クライアントは等値比較ではなくepsilon_qnで比較する。実装は、PPQとQNが線形に対応する通常のアイテムについて「ソース先頭のQN＋ティック数÷分解能」で誤差を減らしてよいが、秒経由の値とepsilon_qn以内で一致する場合に限る。

**ティック表現：** MIDIはテイクの分解能（通常960 PPQ）の整数ティックでしか保存できない。書き込むstart_qnと終了位置は最近傍のティックに換算し、QNへ戻した値との差がepsilon_qnを超える場合は、丸めずに書込前にINVALID_POSITIONで拒否する。書込後の再読取が期待値とepsilon_qnを超えて異なる場合は失敗として復元する（7.2）。

人間向けのPositionは次のどちらか一方とする。

```json
{
  "qn": 13.0
}
```

```json
{
  "bar": 4,
  "beat": 2,
  "offset_qn": 0.0
}
```

bar/beatは1始まりの正整数。offset_qnは省略時0、非負かつその拍の長さ未満。1拍は拍子分母の音価であり、6/8の第1小節第2拍はQN＝0.5。4/4の第4小節第2拍はQN＝13。beatがその小節の拍数を超える場合、offset_qnが1拍以上の場合はINVALID_POSITION。

**対応する時間構造：** 表示小節番号オフセット0、時刻0が第1小節冒頭、拍子変更は小節境界のみ。弱起・不完全小節・負の位置は未対応でUNSUPPORTED_PROJECTとする。拍子・テンポ変更は読み取るが編集しない。

- 拍子表は、テンポ・拍子マーカーを列挙し、各拍子変更の実際のQNが「直前の拍子で小節を数えた位置」と一致することを確認して作る。一致しなければ不完全小節があるとみなす。
- 表示小節番号オフセットは、アクティブな曲についてだけ読める設定値で検査する。対象は常にアクティブな曲なので、常に検査できる。

要求のrangeは`{"start": Position, "end": Position}`で、end > start必須。返却する範囲は`{"start_qn": 数値, "end_qn": 数値}`。

| relation | 取得条件 |
|---|---|
| starts_in | 要求区間内で開始する音符 |
| overlaps | 要求区間と正の長さで交差する音符。範囲前からの持続音も含む |
| sounding_at | 指定時点で区間内にある音符 |

終了境界の音符は含めず、開始境界の音符は含める。音符を要求範囲に切り詰めない。

### 4.2 note_idの定義

note_idはパートごとの1始まり整数。パート内の対応可能な全MIDI音符（3.2で除外した音符を除く）を次の順に並べて付番する。

1. start_qn昇順。
2. pitch昇順。
3. 同値なら、REAPERのトラック内アイテム列挙番号、アイテム内ノート番号の順。

start_qnの比較は、返却する値そのものの厳密な大小比較とし、epsilon_qnを使わない（並べ替えの一貫性のため）。同じテイク内で同じティックの音符は必ず同値になる。

付番は範囲・ミュートの絞込み前に行う。したがって部分取得のnote_idは飛び番になり得る。同じ状態の同じパートなら取得範囲によらず同じ番号となる。アイテム境界やGUIDは返さない。

update/delete/select_notes時は現在の全音符を同じ規則で再列挙して番号を解決する。取得後の追加・削除・移動等で番号が変わり得る。SIOは古い番号の使用を自動では検出しない。取得後に人間や他アプリが編集しない状況で使用するか、4.3のexpectで照合する。SIO自身の編集後も必要なら再取得する。

サーバー側に音符IDの保存表、取得期限、変更番号は設けない。列挙時の一時的な内部参照だけを使用する。

### 4.3 target

```json
{
  "note_id": 5
}
```

```json
{
  "pitch": 60,
  "start_qn": 16.0
}
```

targetは上記のどちらか一方の形式とする。混在、片方だけのpitch/start_qn、未知の項目を拒否する。対象パートは外側のpart_nameで指定できる。省略時は選択中パートを使い、未選択ならPART_NOT_SELECTEDとする。

座標形式は任意でchannel（1〜16）を追加できる。指定するとチャンネルも一致する音符だけを対象にする。別チャンネルにある同時刻・同音高の音符を区別するために使う。

```json
{
  "pitch": 60,
  "start_qn": 16.0,
  "channel": 2
}
```

**開始位置の書き方：** 要求の中で開始位置を表すstart_qnは、代わりにstart（4.1のPosition）で書ける。小節・拍で指定したいときに使う。

```json
{
  "pitch": 60,
  "start": {
    "bar": 5,
    "beat": 1
  }
}
```

- 対象は、座標形式のtarget、targetのexpect、insertのnote、updateのsetの4か所。
- start_qnとstartは同時に指定できない。座標形式のtargetとinsertのnoteではどちらか一方が必須。
- startは実行時にその曲の拍子表でQNに解決し、以後はstart_qnを指定した場合と同じに扱う。解決できない位置（拍数を超えるbeat等）はINVALID_POSITION、対応外の時間構造はUNSUPPORTED_PROJECT。
- 長さ（duration_qn）はQNだけで指定する。応答は従来どおりstart_qnとpositionの両方を返す。

pitchとchannelは整数完全一致、start_qnはepsilon_qn以内で比較する。ミュート状態によらず現在の音符を検索する。0件ならNOTE_NOT_FOUND、複数ならAMBIGUOUS_NOTE。近い音符への自動置換や複数音符への一括適用をしない。

pitch＋start_qnは元の音符の同一性を保証せず、実行時の条件一致検索である。人間が音高・開始を変更すれば見つからず、長さだけを変更すれば一致して変更・削除の対象になる。同じ位置に作り直した音符も一致する。

**expect（任意の照合）：** どちらの形式にも、解決した音符が期待どおりかを確かめるexpectを付けられる。

```json
{
  "note_id": 5,
  "expect": {
    "pitch": 60,
    "start_qn": 16.0
  }
}
```

expectはpitch、start_qn（またはstart）、duration_qn、velocity、channel、mutedのうち1つ以上を持つオブジェクト。targetを解決した後、指定された属性だけを現在の音符と比較する（整数と真偽値は完全一致、QNはepsilon_qn以内）。1つでも異なればNOTE_MISMATCHとし、その要求全体で何も変更しない。古いnote_idが別の音符を指した場合や、座標指定で長さが変わっていた場合を検出できる。サーバーは状態を保持しない。

## 5. 返却データ

### 5.1 音符

```json
{
  "note_id": 5,
  "part_name": "チェロ",
  "pitch": 60,
  "start_qn": 16.0,
  "duration_qn": 1.0,
  "position": {
    "bar": 5,
    "beat": 1,
    "offset_qn": 0.0
  },
  "velocity": 80,
  "channel": 1,
  "muted": false,
  "item_muted": false,
  "track_muted": false,
  "selected": false
}
```

全項目必須。pitchは0〜127整数、velocityは1〜127整数、channelは1〜16整数。REAPER内部のチャンネル0〜15とはBridgeで変換する。中央Cは60。track_mutedは親フォルダのミュートも含む。positionは表示用派生情報。「ミュートされていない音符」とは、muted、item_muted、track_mutedがすべてfalseの音符を指す。

### 5.2 get_notesの結果

```json
{
  "project_name": "Harmony.rpp",
  "captured_at": "2026-11-14T00:00:00Z",
  "time_unit": "quarter_note",
  "range": {
    "start_qn": 0.0,
    "end_qn": 8.0
  },
  "parts": [
    {
      "part_name": "チェロ",
      "order": 2
    }
  ],
  "meters": [
    {
      "start_qn": 0.0,
      "bar": 1,
      "numerator": 4,
      "denominator": 4
    }
  ],
  "notes": [],
  "warnings": []
}
```

これは単なる取得結果であり、サーバー側で有効期間付きの状態として保持しない。captured_atはUTC RFC 3339の取得時刻で、競合検出用ではない。

明示rangeならその解決済み範囲を返す。全体取得・sounding_atなら返却音符を覆う最小範囲、空ならnull。partsは空パートも含めてトラック順、orderは元の1始まりトラック番号。notesはパート順・note_id順。metersは曲頭から対象範囲終端（空なら必要な指定位置）までの拍子情報を返す。

Diagnosticは`{code:文字列, message:文字列, details:オブジェクト}`とする。内部GUIDは出さず、part_name、track_order、item_order、count等で説明する。診断コードは3.2の表のとおり。

### 5.3 constraintsとwarningsの区別

get_score_infoは診断を次のように振り分ける。

| 配列 | 入れるもの |
|---|---|
| constraints | 取得・編集・名前指定を制限するもの。3.2の表で「拒否」となる全コード、SOURCE_OFFSET_READ_ONLY、DUPLICATE_PART_NAME、WRITE_BLOCKED |
| warnings | 無視・除外したことの通知。AUDIO_ITEMS_IGNORED、UNNAMED_TRACKS_IGNORED、NOTES_OUTSIDE_ITEM_IGNORED |

INSTRUMENT_NOT_FOUNDはadd_trackの結果のwarningsにだけ現れる（6.5）。

get_notesのwarningsには、取得は成功したが呼出側が知るべきもの（AUDIO_ITEMS_IGNORED、UNNAMED_TRACKS_IGNORED、NOTES_OUTSIDE_ITEM_IGNORED、SOURCE_OFFSET_READ_ONLY）を入れる。

## 6. 外部HTTP API

### 6.1 共通仕様

- `POST /v2/rpc`、UTF-8 JSON。JSON-RPC 2.0ではなくSIO独自形式。
- `GET /health`は認証不要で`{"status":"ok"}`を返し、Serviceの生存のみを示す。
- RPC認証はBearer token。config.json（2.1）から供給し、URLに入れない。ループバックbind、CORS既定無効。
- 上限値は英語プロトコル仕様書2.1の表に従う。超過時は切り捨てずエラー。
- 未知の引数、重複JSONキー、NaN/Infinity、不正な型、省略可能な項目へのnullを拒否する。
- 整数項目は、小数部・指数部のないJSON数値だけを受け付ける。`60.0`や`6e1`は拒否する。真偽値は整数ではない。

```json
{
  "protocol_version": "2.3",
  "request_id": "23e4a562-38be-4f4a-b89d-729dd2cfa327",
  "method": "get_notes",
  "params": {
    "part_names": [
      "第1バイオリン",
      "チェロ"
    ],
    "at": {
      "bar": 4,
      "beat": 2
    },
    "relation": "sounding_at"
  }
}
```

request_idはクライアントが発行する小文字の標準UUID文字列。成功はHTTP 200で`{request_id, ok:true, result}`。業務エラーも200で`{request_id, ok:false, error:{code,message,details,retryable}}`。

| HTTP | 条件 | error.code |
|---|---|---|
| 400 | 本文がJSONとして不正（重複キー、NaN、非UTF-8を含む）、または外枠が不正（4項目の欠落・型違い・未知の項目、request_idの形式違い） | INVALID_REQUEST |
| 401 | 認証失敗 | UNAUTHORIZED |
| 413 | 本文が上限超過 | LIMIT_EXCEEDED |
| 503 | 待機要求が上限。受け付けず、実行もしない | QUEUE_FULL |
| 200 | 外枠が正しい要求のすべて。protocol_version不一致、未知のmethod、params不正もここに含む | 各コード |

request_idを回復できなければHTTPエラー応答のrequest_idはnull。

### 6.2 メソッドと引数

| method | 必須引数 | 任意引数 | 内容 |
|---|---|---|---|
| get_status | なし | なし | 接続状態、起動世代、最後に確認した対象の曲・既定パート・再生状態 |
| list_projects | なし | なし | 開いている曲名、名前で指定可能か、アクティブか |
| select_project | project_name | なし | その曲のタブを表に出す（対象の曲にする） |
| select_part | part_name | expect_project_name | 対象の曲の既定パートを選択 |
| create_project | なし | なし | 新しい曲（未保存のタブ）を作る。新しい曲が対象になる（6.5） |
| add_track | part_name | instrument, expect_project_name | 対象の曲の末尾にトラック（パート）を追加（6.5） |
| add_item | range | part_name, expect_project_name | パートに空のMIDIアイテムを追加（6.5） |
| get_score_info | なし | expect_project_name | 対象の曲の全パート、拍子、範囲、制約 |
| get_notes | なし | part_names, selected_part, range, at, relation, include_muted, expect_project_name | 対象の曲の音符取得 |
| get_request_status | target_request_id | なし | 要求状態と保存済み応答 |
| select_notes | targets | expect_project_name | 対象の曲の音符を選択 |
| apply_patch | operations | label, dry_run, expect_project_name | 対象の曲の音符を編集 |
| play_range | range | expect_project_name | 対象の曲の範囲を1回再生 |
| stop | なし | なし | SIO管理の再生を停止（曲の解決を行わない） |

全paramsはオブジェクト、引数なしは`{}`。project_nameを引数に取るのはselect_projectだけで、他に付けるとINVALID_REQUEST。返却データのproject_nameは処理した曲の現在名を表し、要求引数とは区別する。応答の詳細は英語プロトコル仕様書に従う。

**expect_project_name：** 空でない文字列。実行時に解決した曲の現在名と完全一致で比較し、異なればPROJECT_MISMATCH（detailsにexpectedとactual）として何も実行しない。曲を切り替える指定ではない。アクティブなタブは人間や他のクライアントがいつでも切り替えられるため、取り違えを検出する目的で使う。名前のない曲は指定できない。

select_project、select_part、create_projectの成功結果は`{project_name, selected_part_name}`。selected_part_nameは未選択ならnull。get_statusのactive_project_name（アクティブな曲の名前）、selected_part_name（その曲の既定パート）、playback_activeは最後にBridgeが確認した状態（heartbeat周期で更新）であり、接続不良時は古い可能性がある。list_projects/get_score_infoはBridgeで現在状態を確認する。

選択と操作の例（各行は別のRPC要求のmethodとparamsを表す。配列一括送信の機能ではない）：

```json
[
  {"method":"select_project","params":{"project_name":"Harmony.rpp"}},
  {"method":"select_part","params":{"part_name":"チェロ"}},
  {"method":"get_notes","params":{"selected_part":true}},
  {"method":"apply_patch","params":{"expect_project_name":"Harmony.rpp","operations":[{"op":"update","target":{"note_id":5,"expect":{"pitch":60}},"set":{"pitch":64}}]}}
]
```

対象の曲がすでに表に出ていれば最初のselect_projectは不要。選択コマンドの成功を待ってから次へ進む。

### 6.3 get_notes

対象パートは次のとおり決める。結果が共有の選択状態によって黙って変わらないよう、既定は常に全パートとする。

| 指定 | 対象 |
|---|---|
| part_namesもselected_partもなし | 全パート（名前のある全トラック） |
| part_names | その一覧。選択状態は使わず、変更もしない。空配列・重複名は拒否 |
| selected_part: true | 選択中パート1つ。未選択ならPART_NOT_SELECTED |

part_namesとselected_part:trueの同時指定は拒否する。selected_partの既定はfalse。全パート取得で重複名のパートがあればAMBIGUOUS_PART（details.diagnosticsにDUPLICATE_PART_NAME）。名前のないトラックは対象外とし、UNNAMED_TRACKS_IGNOREDを警告する。part_namesで一意な名前を明示すれば、他に重複名があっても取得できる。

relationはstarts_in/overlaps/sounding_at、既定overlaps。include_mutedは既定falseで、音符・アイテム・親を含むトラックのどれかがミュートなら除外する。

rangeとatは同時指定不可。sounding_atはat必須・range不可、それ以外はat不可。range省略のstarts_in/overlapsは対象パートの全音符を返す。取得はREAPERのトラックやアイテムの選択状態と無関係である。対象パートのどれかに対応外のMIDI構造があれば、要求全体を失敗とする。

### 6.4 select_notes

```json
{
  "targets": [
    {
      "part_name": "チェロ",
      "target": {
        "note_id": 5
      }
    },
    {
      "part_name": "第1バイオリン",
      "target": {
        "pitch": 65,
        "start_qn": 16.0
      }
    }
  ]
}
```

各targets要素のpart_nameは省略可能で、その場合は選択中パートを使う。未選択ならPART_NOT_SELECTED。targetsは0〜10,000件。全対象を先に解決し、同じ実音符への重複指定は拒否する。

成功時は指定音符だけを選択し、曲内の他のMIDI音符選択を解除する。解除の対象は、対応外のアイテムや複数テイクを含む、曲内の全MIDIテイクとする。空配列は選択解除。音符以外（CC等）の選択は変更しない。前面化・タブ切替・エディタ表示は行わない。Undo点は作らない。録音中はTRANSPORT_BUSY。結果は`{"project_name":"Harmony.rpp","selected_count":2}`。選択だけなら付番は変わらない。

### 6.5 create_project、add_track、add_item

曲、トラック、音符を入力できる範囲を用意するための要求。音符の編集（7章）とは別の単純な書込であり、復元処理を持たない。

**create_project**（引数なし）：新しい曲を新しいタブに作る。新しいタブがアクティブになるので、以後の要求は新しい曲を対象とする。既定パートは未選択。結果はselect_projectと同じ`{project_name, selected_part_name}`で、未保存のためproject_nameは`""`になる。

- Bridgeは管理中の再生を停止してから（8章のstopと同じ処理）、REAPERのAction「New project tab」（コマンドID 40859）を実行する。実行後、開いている曲が1つ増え、アクティブな曲が実行前の一覧にないことを確認する。確認できなければINTERNAL_ERROR。
- ファイルには保存しない。名前がないので、人間が保存するまではselect_projectで選べず、expect_project_nameも指定できない。タブが表に出ている間は対象であり続ける。別のタブが表に出た後は、人間がタブを表に出せば再び対象になる（SIOからは表に出せない）。
- 曲の内容はREAPERの新規プロジェクト設定に従う。既定のプロジェクトテンプレートが設定されていれば、最初からトラックを含むことがある。
- アクティブな曲が録音中ならTRANSPORT_BUSYとし、何も作らない。
- 曲を開く、保存する、閉じる要求は持たない。

**add_track**（part_name必須、instrument任意、expect_project_name任意）：対象の曲の末尾に、part_nameを名前とするトラックを1本追加する。名前があるので、新しいパートになる。

```json
{
  "part_name": "第1バイオリン",
  "instrument": "ReaSynth"
}
```

- 同じ名前のトラックがすでにあればPART_ALREADY_EXISTS（detailsにpart_name）とし、何も変更しない。重複名のパートを作らないための規則である。
- トラックは`InsertTrackInProject`のflags 0で追加する。既定のFXやエンベロープを付けない空のトラックになる。
- instrumentは音源（インストゥルメント）プラグインの名前。`TrackFX_AddByName`にそのまま渡す。REAPERのFXブラウザに表示される完全な名前（`VSTi: ReaSynth (Cockos)`）のほか、その一部（`ReaSynth`）でも指定できる。部分的な名前がどのプラグインに一致するかはREAPERの検索に従う。
- **プラグインが見つからない場合、または見つかったプラグインが音源でない場合は、プラグインのない空のトラックを追加する。** 要求は成功とし、instrument_loadedをfalse、warningsにINSTRUMENT_NOT_FOUND（detailsにinstrument＝要求された名前）を入れる。音源かどうかは、挿入後に`TrackFX_GetInstrument`がそのFXを指すかで判定し、音源でなければ`TrackFX_Delete`で取り除く。
- プラグインは既定の状態で挿入する。プリセットの切り替え、パラメータの操作、ルーティングの設定、トラックの削除は行わない。
- 追加したトラックにMIDIアイテムはない。音符を追加するには、add_itemかREAPERでアイテムを作る（なければNO_TARGET_ITEM）。
- **追加したトラックを、select_partを実行したのと同じ状態にする。** その曲の既定パートを追加したトラックに置き換え、REAPER上でもそのトラックだけを選択状態にする（3.1）。音源を挿入できなかった場合も同じ。結果のselected_part_nameで返す。トラック選択の変更はUndoブロックを閉じた後に行う。
- 追加は`SIO: Add track "<part_name>"`というUndo点1個にまとめる。
- 録音中はTRANSPORT_BUSY。書込禁止中の曲（WRITE_BLOCKED）にはROLLBACK_FAILED（blocked: true）。
- 大きな音源の読み込みは待機時間を超えることがあり、その場合はREQUEST_PENDINGになる（9.2）。

結果：

```json
{
  "project_name": "Harmony.rpp",
  "part_name": "第1バイオリン",
  "order": 3,
  "selected_part_name": "第1バイオリン",
  "instrument_loaded": true,
  "instrument_name": "VSTi: ReaSynth (Cockos)",
  "warnings": []
}
```

orderは3.1のトラック番号。selected_part_nameは実行後の既定パートで、常にpart_nameと等しい。instrument_nameは挿入したプラグインについてREAPERが返す名前で、挿入しなかった場合はnull。

**add_item**（range必須、part_name任意、expect_project_name任意）：パートのトラックに、rangeをちょうど覆う空のMIDIアイテムを1個作る。音符を入力できる範囲を用意するための要求で、apply_patchによる追加にも、人間のピアノロール入力にも使える。part_nameを省略すると選択中パートを使い、未選択ならPART_NOT_SELECTED。

```json
{
  "part_name": "第1バイオリン",
  "range": {
    "start": {"bar": 1, "beat": 1},
    "end": {"bar": 9, "beat": 1}
  }
}
```

結果は`{"project_name":"Harmony.rpp","part_name":"第1バイオリン","range":{"start_qn":0.0,"end_qn":32.0}}`。アイテムの識別子は返さない。アイテムは引き続き外部に公開せず、作成後は人間が作ったアイテムと区別しない。

- rangeがトラック上の既存アイテム（種類を問わない）と重なる場合はRANGE_OCCUPIEDとし、何も変更しない。detailsはpart_name、range、item_range（最初に重なったアイテム。どちらも`{start_qn, end_qn}`）。端が接するだけの範囲は許可する。重なりの判定はepsilon_qnを用いる。アイテムの延長・移動・結合・削除は行わない。
- rangeは小節や拍に揃っていなくても受け付ける。ただしアイテムの先頭がティック格子の原点になるため、半端な位置から始めると、通常の音符位置がティックで表現できなくなる（追加時にINVALID_POSITION）。小節の頭から始めることを推奨する。
- 音符は1つのアイテムに完全に収まる必要がある（7.1.2）。隣り合う2つのアイテムを作っても、境界をまたぐ音符は追加できない。連続した区間は1つのアイテムで用意する。
- `CreateNewMIDIItemInProj`には秒で位置を渡す（`TimeMap2_QNToTime`で曲を明示して換算）。QNで渡す指定は、4.1と同じ理由で使わない。
- 作成後に、アイテムの範囲が要求と一致すること、MIDIテイクが1つであること、ソースが先頭から範囲全体を覆うことを確認する。確認できなければ作ったアイテムを削除し、WRITE_FAILEDを返す。
- 作ったアイテムはループ無効（B_LOOPSRC＝0）にする。人間が後から端を伸ばしても、ソースが繰り返されず空白になる（3.2のLOOPED_MIDI_ITEMを避ける）。
- 追加は`SIO: Add item to "<part_name>"`というUndo点1個にまとめる。既定パートとREAPERのUI選択は変えない。
- 録音中はTRANSPORT_BUSY。書込禁止中の曲にはROLLBACK_FAILED（blocked: true）。
- 同じ範囲への再実行はRANGE_OCCUPIEDになり、アイテムは二重に作られない。

create_projectとadd_trackは、成功するたびに曲やトラックが増える。応答を失った場合は9.2に従って同じrequest_idで照会し、新しいIDで送り直さない。

## 7. 音符編集

### 7.1 apply_patchと操作形式

```json
{
  "protocol_version": "2.3",
  "request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27",
  "method": "apply_patch",
  "params": {
    "label": "CT: チェロの修正",
    "dry_run": false,
    "expect_project_name": "Harmony.rpp",
    "operations": [
      {
        "op": "update",
        "part_name": "チェロ",
        "target": {
          "note_id": 5,
          "expect": {
            "pitch": 60,
            "start_qn": 16.0
          }
        },
        "set": {
          "pitch": 64
        }
      },
      {
        "op": "delete",
        "part_name": "第1バイオリン",
        "target": {
          "pitch": 65,
          "start_qn": 16.0
        }
      },
      {
        "op": "insert",
        "client_note_id": "new-1",
        "part_name": "チェロ",
        "note": {
          "pitch": 60,
          "start_qn": 20.0,
          "duration_qn": 1.0,
          "velocity": 80,
          "channel": 1,
          "muted": false
        }
      }
    ]
  }
}
```

operationsは必須。プロジェクトは対象の曲を使う。labelはUndo表示名、省略時または空文字列のとき`SIO: Edit notes`。dry_runは既定false。

### 7.1.1 操作一覧

| op | 処理 | op以外の必須引数 |
|---|---|---|
| insert | 音符追加 | client_note_id、note（part_nameは任意） |
| update | 属性変更 | target、set（part_nameは任意） |
| delete | 音符削除 | target（part_nameは任意） |

### 7.1.2 insert

| 引数 | 型・必須条件 | 意味 |
|---|---|---|
| op | 文字列、必須 | 固定値insert |
| client_note_id | 空でない文字列、必須 | 同要求内で一意なラベル。結果のinsertedで追加後のnote_idと対応付ける |
| part_name | 空でない文字列、任意 | 追加先パート名。省略時は選択中パート |
| note | オブジェクト、必須 | 下表の6項目すべてを指定（開始位置はstart_qnかstartのどちらか） |

| noteの項目 | 型・範囲 | 意味 |
|---|---|---|
| pitch | 整数0〜127 | MIDI音高 |
| start_qn または start | 有限数0以上、またはPosition | 開始位置。どちらか一方が必須（4.3） |
| duration_qn | 有限数0より大きい | 長さ |
| velocity | 整数1〜127 | ベロシティ |
| channel | 整数1〜16 | チャンネル |
| muted | 真偽値 | 音符ミュート |

追加音符全体を含む既存の対応可能なMIDIアイテムが必要で、なければNO_TARGET_ITEM。重なったアイテムを拒否するため、候補が複数になることはない。SIOはトラックやアイテムを作成・延長しない。新規音符は未選択。

### 7.1.3 update

| 引数 | 型・必須条件 | 意味 |
|---|---|---|
| op | 文字列、必須 | 固定値update |
| part_name | 空でない文字列、任意 | 対象パート名。省略時は選択中パート |
| target | オブジェクト、必須 | 4.3のいずれかの形式 |
| set | オブジェクト、必須・空不可 | 変更属性と変更後の絶対値 |

setで指定できる項目はpitch/start_qn（またはstart）/duration_qn/velocity/channel/muted。型と範囲はinsertのnoteと同じ。未指定属性は保持し、値を増減量とは解釈しない。start_qnだけを指定した場合は長さを保って移動する（終了位置が動く）。duration_qnだけを指定した場合は開始を保つ。移動・延長後も元アイテム内に収まる必要があり、収まらなければINVALID_POSITION（details.reasonは`outside_item`）。パート変更はdelete＋insert、選択はselect_notesで表す。

```json
{
  "op": "update",
  "part_name": "チェロ",
  "target": {
    "pitch": 60,
    "start_qn": 16.0
  },
  "set": {
    "pitch": 64,
    "duration_qn": 2.0
  }
}
```

### 7.1.4 delete

| 引数 | 型・必須条件 | 意味 |
|---|---|---|
| op | 文字列、必須 | 固定値delete |
| part_name | 空でない文字列、任意 | 対象パート名。省略時は選択中パート |
| target | オブジェクト、必須 | 4.3のいずれかの形式 |

```json
{
  "op": "delete",
  "part_name": "チェロ",
  "target": {
    "note_id": 5
  }
}
```

一致した1音だけを削除し、同音高・同時刻の他音をまとめて消さない。座標指定で複数一致した場合はエラー。

### 7.1.5 操作列全体の規則

- part_name省略時に選択中パートがなければPART_NOT_SELECTED。明示part_nameはその操作だけの対象で、選択状態を変更しない。1要求で複数パートを扱える。
- operationsは1〜10,000件。空配列、未知引数、不正型・値、nullによる属性削除はINVALID_REQUEST。10,000件超はLIMIT_EXCEEDED。
- note_id方式と座標方式を混在させてよいが、全対象は編集開始前の現在状態から先に解決する。
- 異なる指定方式でも同じ実音符に解決した複数操作はINVALID_REQUEST。複数属性は1つのupdateにまとめる。
- client_note_idの重複を拒否する。insertした音符を同要求内の他操作から参照できない。
- 配列は逐次実行言語ではなく現在状態に対する変更集合。内部で削除順等を調整できる。
- 全件を検証してから書き込み、一部成功を返さない。
- expectを付けない操作については、取得後の人間の変更を検出しない。対象の現状を変更する仕様である。
- dry_runは検証と、解決結果・予定件数の返却だけ。対象を予約せず、その後の編集の成功も保証しない。
- 録音中はdry_runを含めTRANSPORT_BUSY。
- **同一チャンネル・同音高で時間が重なる音符は書き込めない。** MIDIではnote-onとnote-offの対応が一意に決まらず、REAPERが対応を組み替える。追加・移動・延長の結果そうなる場合は、書込後の検証で検出してWRITE_FAILEDとし、復元する。同時刻・同音高の別音符は別チャンネルに置く。

### 7.2 適用手順

1. 実行時の対象の曲（アクティブなタブの曲）を確定し、expect_project_nameがあれば照合する。書込禁止中（ROLLBACK_FAILED後）ならROLLBACK_FAILED（details.blockedがtrue）を返す。各操作の明示または選択中パートを解決して対応条件を確認する。
2. 対象パート全体を列挙し、すべてのtargetを現在状態に対して解決し、expectを照合する。
3. 重複・範囲・追加先・ティック表現（4.1）を全件検証し、メモリ上で変更計画を作る。
4. dry_runなら解決結果と予定件数を返して終了する。
5. 影響する全テイクの生MIDI（`MIDI_GetAllEvts`）を退避する。これは当該書込の復元用一時データであり、外部参照・期限管理付きの取得状態ではない。
6. Undoブロックを開始し、deferを挟まずに適用・再読取検証する。検証は、影響テイクの全音符（ティック位置、音高、チャンネル、ベロシティ、ミュート、選択）が期待状態と一致すること、QNがepsilon_qn以内であること、音符以外のイベント数が変わらないことを確認する。
7. 成功時はUndoブロックを終了し表示更新。失敗時は全影響データを復元（`MIDI_SetAllEvts`）し、退避内容と一致することを確認する。

非音符イベント（CC、SysEx、テキスト等）は保持する。既存音符の選択フラグも保持する。更新、内部インデックス降順の削除、追加の順にソートなしで書き、最後にテイクごとに1回ソートする。

結果：

```json
{
  "project_name": "Harmony.rpp",
  "added": 1,
  "updated": 1,
  "deleted": 1,
  "dry_run": false,
  "resolved": [
    {
      "operation_index": 0,
      "op": "update",
      "note": {
        "note_id": 5,
        "part_name": "チェロ",
        "pitch": 60,
        "start_qn": 16.0,
        "duration_qn": 1.0,
        "position": {
          "bar": 5,
          "beat": 1,
          "offset_qn": 0.0
        },
        "velocity": 80,
        "channel": 1,
        "muted": false,
        "item_muted": false,
        "track_muted": false,
        "selected": false
      }
    }
  ],
  "inserted": [
    {
      "operation_index": 2,
      "client_note_id": "new-1",
      "part_name": "チェロ",
      "note_id": 7
    }
  ]
}
```

- 件数は受け付けた操作数で、同じ値へのupdateも1件。
- resolvedは、updateとdeleteの各操作が解決した音符の**変更前**の内容（5.1の形式）を操作順に並べる。operation_indexは0始まり。dry_runでも実適用でも返す。これによりdry_runで対象を確認できる。
- insertedは、追加した各音符の**編集後**の付番によるnote_idを返す。同じパートを再列挙して求める。dry_runでは空配列。このnote_idも4.2のとおり、その後の編集で変わり得る。
- 実適用後は既存音符のnote_idが変わり得る。続けて番号で操作するなら再取得する。
- 通常のUndo1回で元に戻せること。失敗して復元した場合のUndo点は、表示名に` (failed, restored)`を付ける。

**Undoに関するREAPERの挙動：** Undoブロックは対象の曲に1件のUndo点を作る。Undoは直前のUndo点の状態へ戻すので、Undo点を作らずに行われた変更は一緒に巻き戻る。

クラッシュをまたぐ完全原子性は保証しない。復元確認失敗はROLLBACK_FAILEDとし、当該曲の後続書込（dry_runを含むapply_patch）を禁止する。名前変更で禁止を回避しないよう、Bridge内では開いている曲の内部参照に結び付ける。禁止中はget_score_infoのconstraintsにWRITE_BLOCKEDを出す。読取は可能。人間の確認とBridge再起動（10.2）で解除する。

受入試験のため、BridgeはExtState `SIO/test_inject`が`write_fail`または`rollback_fail`のとき、適用後に失敗を注入する。通常は未設定である。

## 8. 再生

play_rangeはrangeを受け取り、対象の曲を対象とする。再生中・録音中・一時停止中、またはSIO管理の再生が進行中ならTRANSPORT_BUSY。録音中は編集・音符の選択・select_projectによるタブ切替を拒否する（TRANSPORT_BUSY）。

再生前のカーソルとリピート状態を保存し、リピートを一時無効にして冒頭から再生する。終端で停止して状態を復元する。応答`{"project_name":"Harmony.rpp","started":true}`は開始の確認であって終了待ちではない。GUI処理周期による停止で、サンプル精度は保証しない。

管理中の再生は、deferの毎周期に次を確認する。

| 検出 | 動作 |
|---|---|
| 再生位置が終端に到達 | 停止し、カーソルとリピートを復元 |
| 終端付近（0.3秒以内）でREAPER自身が停止 | 正常終了として、カーソルとリピートを復元 |
| 人間の停止・一時停止、編集カーソルの移動、再生位置のジャンプ、タブ切替、録音、曲のクローズ | 管理を解除。カーソルは復元しない |

管理を解除したとき、人間の変更を古い状態で上書きしない。ただしリピートは、SIOが無効にしたまま（人間が変更していない）であれば元に戻す。SIOが無効にした設定を残さないためである。

stopはSIO管理の再生だけを止め、状態を復元して`{"stopped":true}`を返す。SIO管理の再生がなければ`{"stopped":false}`。人間が始めた再生は止めない。stopは曲の解決を行わず、既定パートも使わない。select_projectとcreate_projectは、タブを切り替える前に同じ停止処理を行う。

再生の完了通知は設けない。get_statusのplayback_activeで、SIO管理の再生が進行中かを確認できる（最後にBridgeが確認した状態）。

## 9. IPC、要求の再送、エラー

### 9.1 ファイル通信

REAPERリソース配下`SIO/ipc/`を使い、Serviceも同じ絶対パスを使う。

| ファイル | 書込担当 | 内容 |
|---|---|---|
| service.json | Service | 起動世代と開始時刻 |
| bridge.json | Bridge | 起動世代、状態（running/stopped）、実行中要求、アクティブな曲名（active_project_name）、その曲の既定パート名、再生状態 |
| requests/<request_id>.json | Service | 要求、Service/Bridge世代、発行時刻、実行開始期限 |
| claimed/<request_id>.json | Bridge | Bridgeが所有した要求（requests/からrename） |
| cancelled/<request_id>.*.json | Service | Serviceが取り消した要求（requests/からrename） |
| responses/<request_id>.json | Bridge | 応答と起動世代 |

- 一時ファイルをclose後、同ディレクトリ内renameで公開する。要求・応答の最終ファイルは上書きしない。
- **claimと取消：** Bridgeは要求を読む前に`requests/`から`claimed/`へrenameする。Serviceは実行開始期限を過ぎても応答がなければ、`requests/`から`cancelled/`へのrenameを試みる。同じファイルのrenameは一方しか成功しないので、取消に成功した要求は決して実行されず、取消に失敗した要求はBridgeが所有している。これにより「実行されていない」と「実行されたかもしれない」を確実に区別する。
- Bridgeは応答を書いた後、claimed/のファイルを削除する。
- Serviceは同時に1件のみBridgeに配送し、応答未確定中は次を送らない。
- bridge.jsonの置換は、Windows上のLuaでは原子的に行えない（削除してからrename）。読む側は一時的に読めない場合に短時間再試行し、直近に読めた内容を使う。置換に失敗した側は次周期で再試行する。
- heartbeatの時刻は、bridge.jsonの最終更新時刻（mtime）で判定する。
- Bridgeは実行前にbridge.jsonへ実行中のrequest_idを書く。

Bridgeは次の要求を実行しない。

| 条件 | 動作 |
|---|---|
| 要求のService世代がservice.jsonと一致しない（旧世代のService） | 応答せずに破棄 |
| 要求のBridge世代が自分と一致しない | BRIDGE_UNAVAILABLE（retryable）を応答 |
| 実行開始期限切れ | BRIDGE_UNAVAILABLE（retryable）を応答 |
| 実行済みのrequest_id、または応答ファイルが既にある | 応答せずに破棄 |

Service/Bridgeの起動世代は通信の旧要求排除用であり、音符の変更検出には使わない。Bridgeは未知のService世代の要求を受けたとき、およびheartbeat周期ごとにservice.jsonを読み、世代が変わっていれば全曲の既定パートを解除する。

### 9.2 待機と再送

| 値 | 既定 | 意味 |
|---|---|---|
| キュー待ち | 10秒 | 超えたらREQUEST_PENDING |
| 配送後の待機 | 30秒 | 超えたらREQUEST_PENDING |
| 実行開始期限 | 配送から5秒 | Bridgeはこれを過ぎた要求を実行しない。Serviceは期限の1秒後に取消を試みる |
| unresponsive判定 | 3秒 | bridge.jsonの更新がこの時間なければunresponsive |
| 結果不明の確定 | claim後120秒 | Bridgeがunresponsiveのまま経過したらOUTCOME_UNKNOWN |

配送時にBridgeがconnectedでなければ、要求ファイルを書かずにBRIDGE_UNAVAILABLEとする。claim済みの要求は、Bridgeの起動世代が変わるか停止を確認した時点、または上表の時間が過ぎた時点でOUTCOME_UNKNOWNとする。Bridgeは1回のコールバックで要求を実行するため、長い処理中はheartbeatが止まる。unresponsiveは停止を意味しない。

待機超過はREQUEST_PENDINGとtarget_request_idを返し、実行を取り消したことにはしない。get_request_statusでqueued/running/succeeded/failed/expired/unknownと保存済み応答を確認できる。この照会とget_statusはBridge待ち行列に入れない。

**要求の記録：** 外枠が正しく受け付けた要求は、get_statusとget_request_status、検証エラーで即時に確定したものも含めて、すべて記録する。503（QUEUE_FULL）で拒否した要求は記録せず、同じrequest_idで後から実行できる。REQUEST_PENDING、REQUEST_ID_REUSE、REQUEST_EXPIREDの応答自体は記録しない。

同じrequest_id＋同じ解析済みJSON内容の再送は、待機に合流して状態または応答を返す。異なる内容はREQUEST_ID_REUSE。終端結果（エラーを含む）も保存し、同じIDで新しい操作を始めない。

**保持：** 終端応答はService稼働中、最長24時間保持する。メモリを際限なく使わないよう、保存済み応答の合計（圧縮後）が上限（既定256 MiB）を超えたら、古いものから24時間を待たずに期限切れにする。最新の1件は常に保持する。期限切れになった既知のIDはREQUEST_EXPIREDとし、照会ではexpiredを返す。

通信断は取消ではない。特に番号がずれる編集を新request_idで自動再送してはならない。再起動や異常終了で結果不明ならOUTCOME_UNKNOWNとし、現在データを取得して確認する。再起動をまたぐexactly-onceは保証しない。Serviceは起動時に`requests/`の未実行要求を削除し、`responses/`、`claimed/`、`cancelled/`の24時間超のファイルだけを掃除する。

### 9.3 エラー一覧

| code | 意味 | retryable |
|---|---|---|
| INVALID_REQUEST | 引数・型の不正、同じ実音符への重複指定、空の名前 | false |
| UNKNOWN_METHOD / VERSION_MISMATCH | method・版の不正 | false |
| UNAUTHORIZED | 認証失敗（HTTP 401） | false |
| BRIDGE_UNAVAILABLE | 接続なし・応答不可。要求は実行されていない | true |
| PROJECT_NOT_FOUND / AMBIGUOUS_PROJECT | select_projectの曲名が0件／複数件 | false |
| NO_PROJECT_OPEN | 開いた曲なし | false |
| PROJECT_MISMATCH | expect_project_nameが実行時の曲名と不一致 | false |
| PART_NOT_SELECTED | 既定パートが必要だが未選択 | false |
| PART_NOT_FOUND / AMBIGUOUS_PART | パート名が0件／複数件。全パート取得で重複名がある | false |
| PART_ALREADY_EXISTS | add_trackの名前を持つトラックがすでにある。何も追加していない | false |
| UNSUPPORTED_PROJECT | 対応外の時間構造（弱起、不完全小節、小節途中の拍子変更、小節番号オフセット） | false |
| UNSUPPORTED_ITEM / UNSUPPORTED_TAKES | 対応外のMIDI構造／複数テイク | false |
| INVALID_POSITION | 時間座標が不正、アイテムに収まらない、ティックで表現できない | false |
| NOTE_NOT_FOUND / AMBIGUOUS_NOTE | 対象音符が0件／複数件 | false |
| NOTE_MISMATCH | 解決した音符がexpectと不一致 | false |
| NO_TARGET_ITEM | 追加する音符全体を含むアイテムがない | false |
| RANGE_OCCUPIED | add_itemの範囲が既存アイテムと重なる。何も追加していない | false |
| TRANSPORT_BUSY | 再生・録音・一時停止中。録音中の編集・音符の選択・タブ切替・曲やトラックやアイテムの作成 | true |
| REQUEST_PENDING | まだ結果なし。状態を照会する | false |
| OUTCOME_UNKNOWN | 異常終了等で結果不明 | false |
| REQUEST_EXPIRED / REQUEST_ID_REUSE | 再送規則違反 | false |
| LIMIT_EXCEEDED | 本文、応答、音符数、操作数、対象数の上限超過 | false |
| QUEUE_FULL | 待機要求の上限（HTTP 503）。実行されていない | true |
| WRITE_FAILED | 書込失敗。復元を確認済み（add_itemでは作りかけのアイテムを削除済み） | false |
| ROLLBACK_FAILED | 復元を確認できない。以後その曲への書込を禁止中の要求にも返す | false |
| INTERNAL_ERROR | 内部失敗。編集の結果は保守的に扱う | false |

messageは日本語表示可、codeで分岐する。retryableは助言であり、不明な書込の新IDによる自動再送を許可しない。

detailsはオブジェクトで、次の項目を必ず含む。これ以外の項目を追加してよい。

| code | detailsの必須項目 |
|---|---|
| REQUEST_PENDING | target_request_id |
| PROJECT_NOT_FOUND | project_name |
| AMBIGUOUS_PROJECT | project_name、match_count |
| PROJECT_MISMATCH | expected、actual |
| PART_NOT_FOUND | part_name |
| PART_ALREADY_EXISTS | part_name |
| AMBIGUOUS_PART | part_nameとmatch_count（名前指定時）、またはdiagnostics（全パート取得時） |
| UNSUPPORTED_ITEM / UNSUPPORTED_TAKES | part_name、diagnostics（Diagnostic配列） |
| UNSUPPORTED_PROJECT | reason |
| RANGE_OCCUPIED | part_name、range、item_range |
| NOTE_NOT_FOUND | part_name |
| AMBIGUOUS_NOTE | part_name、match_count |
| NOTE_MISMATCH | part_name、mismatched_fields（属性名の配列）、actual（5.1の音符） |
| LIMIT_EXCEEDED | limit |
| BRIDGE_UNAVAILABLE | bridge_state、reason |
| OUTCOME_UNKNOWN | reason |
| ROLLBACK_FAILED | project_name。書込禁止による拒否ではblocked: true |
| INVALID_REQUEST（paramsの検証） | errors（`{path, reason}`の配列） |

apply_patchの個別操作に起因するエラーはoperation_index（0始まり）を、select_notesの個別対象に起因するエラーはindex（0始まり）を含む。

## 10. Bridgeの実行と起動

### 10.1 常駐ループ

```lua
local next_poll, next_heartbeat = 0, 0
local function loop()
    local now = reaper.time_precise()
    if now >= next_heartbeat then
        next_heartbeat = now + 1.0
        -- service.json の確認、アクティブな曲と既定パートの確認、bridge.json の更新
    end
    if now >= next_poll then
        next_poll = now + 0.1
        -- 要求の列挙、claim、対象の列挙・解決、実行、応答
    end
    -- 管理中の範囲再生を確認
    reaper.defer(loop)
end
reaper.atexit(function()
    -- 管理中の再生の停止と復元、bridge.json を stopped に更新
end)
loop()
```

while/sleep待機、AI呼び出し、ネットワーク待ちは行わない。REAPER APIはLua側から呼ぶ。取得は1回のコールバック内で列挙し、途中でdeferしない。取得後の変更監視は行わない。待機中は音符を走査しない。各処理は保護呼び出しで包み、エラーでループを止めない。

`reaper.EnumerateFiles`はディレクトリ一覧をキャッシュするので、列挙の前にインデックス-1で呼んでキャッシュを破棄する。

### 10.2 導入、自動起動、再起動

導入コマンド（`python -m sio_service.install`）が、BridgeとJSONライブラリを`Scripts/SIO/`へ配置し、`Scripts/__startup.lua`に自動起動ブロックを追加する。`__startup.lua`はREAPERが起動時に実行する標準機能であり、SWSは不要。ブロックは`AddRemoveReaScript`でBridgeをActionとして登録し、`Main_OnCommand`で起動する。SWSのグローバル起動アクションを使う場合は、自動起動ブロックなしで導入し、登録済みのActionを指定する。通常の分析ごとの操作は不要。

BridgeはトグルActionとして動く（`set_action_options`）。実行中にもう一度Actionを実行すると終了し、さらに実行すると起動する。**Bridgeの再起動**とは、この終了と起動を指す。再起動により起動世代が変わり、全曲の既定パートとROLLBACK_FAILEDの書込禁止が解除される。

多重起動は、ExtStateに記録した起動世代とheartbeat時刻で防ぐ。生存中のBridgeがあれば新しいインスタンスは起動しない。

heartbeatは約1秒、3秒以上更新がなければunresponsive。停止と断定せず処理中要求を併記する。Bridgeは終了時にbridge.jsonの状態をstoppedにする（unavailable）。Service不在でもBridgeは待機できる。

### 10.3 使用するAPI

EnumProjects、GetProjectName、ValidatePtr2、CountTracks、GetTrack、GetSetMediaTrackInfo_String（P_NAME）、GetMediaTrackInfo_Value、GetParentTrack、CountTrackMediaItems、GetTrackMediaItem、GetMediaItemInfo_Value、CountTakes、GetTake、TakeIsMIDI、GetMediaItemTake_Source、GetMediaSourceType、GetMediaSourceLength、GetMediaItemTakeInfo_Value、MIDI_CountEvts、MIDI_GetNote、MIDI_InsertNote、MIDI_SetNote、MIDI_DeleteNote、MIDI_Sort、MIDI_GetAllEvts、MIDI_SetAllEvts、MIDI_GetProjTimeFromPPQPos、MIDI_GetPPQPosFromProjTime、TimeMap_GetMeasureInfo、CountTempoTimeSigMarkers、GetTempoTimeSigMarker、TimeMap2_QNToTime、TimeMap2_timeToQN、Undo_BeginBlock2、Undo_EndBlock2、MarkTrackItemsDirty、UpdateItemInProject、UpdateArrange、GetPlayStateEx、GetPlayPositionEx、GetCursorPositionEx、SetEditCurPos2、GetSetRepeatEx、OnPlayButtonEx、OnStopButtonEx、SelectProjectInstance、SetOnlyTrackSelected、Main_OnCommand（40859）、InsertTrackInProject、TrackFX_AddByName、TrackFX_GetInstrument、TrackFX_GetFXName、TrackFX_Delete、TrackList_AdjustWindows、CreateNewMIDIItemInProj、SetMediaItemInfo_Value（B_LOOPSRC）、GetActiveTake、DeleteTrackMediaItem、EnumerateFiles、RecursiveCreateDirectory、GetExtState、SetExtState、set_action_options、defer、atexit。

使用しないAPI：MIDI_GetProjQNFromPPQPos、MIDI_GetPPQPosFromProjQN（4.1）。GetTrackName（名前のないトラックに代替名を返すため）。

必要な内部アイテム・テイク参照は外部に公開しない。

## 11. 検証と実装順序

### 11.1 受入条件

| 項目 | 期待結果 |
|---|---|
| UI非依存 | MIDIエディタを閉じ別トラックを選んでも同じ音符を取得 |
| タブ連動 | select_projectがタブを表に出す。人間のタブ切替、曲のオープン・クローズに対象が追従する。失敗したselect_projectはタブを変えない |
| 名前・既定パート | 完全一致、重複、名前のない曲、改名、削除、曲ごとの既定パートの保持、再起動を確認。select_partとadd_trackがREAPER上で1本だけを選択し、人間のトラック選択（複数選択を含む）では既定パートが変わらないことを確認 |
| 対象パート | 既定の全パート、part_names、selected_part、操作ごとの上書き、名前のないトラックの除外を確認 |
| 取り違え検出 | expect_project_nameの不一致、targetのexpectの不一致で何も変更しない |
| 複数アイテム | 同パートの複数アイテムを統合し、全体付番する |
| 部分取得 | ミュート・範囲フィルタでnote_idを付け直さない |
| 番号参照 | 現在の全体列挙で解決する |
| 座標参照 | 微小差、0件、重複音符の複数件、channelによる区別、小節・拍による開始位置を規則どおり扱う |
| 編集集合 | 異方式で同じ音への重複を拒否し、削除で後続参照をずらさない |
| 編集結果 | resolvedが変更前の音符、insertedが編集後のnote_idを返す。dry_runは何も変更しない |
| ティック | ティックで表現できない位置をINVALID_POSITIONで拒否する |
| 同チャンネル重複 | 同チャンネル・同音高で重なる追加をWRITE_FAILEDとし復元する |
| テイク・構造 | 複数テイク、重なり、実際のループ、再生速度、境界をまたぐ音符、固定レーンを拒否。既定設定のアイテムと境界外の音符は読める |
| 時間 | 4/4、3/4、6/8、拍子変更、テンポ変化でQNを正しく変換 |
| 保持・復元 | CC等を保持し、Undo1回と注入失敗時の全復元、復元失敗後の書込禁止を確認 |
| 再送 | 同ID再送で二重編集せず、再起動後の旧要求を実行しない |
| 再生 | 終端での停止と復元、stop、人間の停止、人間の再生を止めないこと、select_projectによる切替前の停止と復元を確認 |
| 応答性 | 待機中に全音符走査せず、編集・再生を妨げない |
| 曲とトラックの作成 | create_projectが新しいタブを作り、その曲が対象になる。add_trackが末尾に追加し、音源あり・名前なし・音源でないプラグイン・同名の拒否・Undo1回を確認 |
| アイテムの作成 | add_itemがテンポ・拍子変更をまたぐ範囲を正しく作り、作った範囲の両端まで音符を追加できる。重なりの拒否、接する範囲の許可、選択中パートの使用、Undo1回を確認 |

8パート×32小節・2,000音符の取得を受付から1秒以内とする目標を実機計測する（REAPER 7.86で約0.23秒）。保証値ではない。最大規模でUI停止時間も測定する。

### 11.2 成果物

Service、Bridge、JSONライブラリ、導入コマンド、README、要求のJSON Schema、Service単体試験、実機受入試験。確認用の対話ツールsio_cli（`SIO_CLI_Specification_ja.md`）と、Python用のクライアントライブラリsio_client（`SIO_Client_Library_Specification_ja.md`）を含む。教材テンプレート、OpenAPI文書、MCP入口は別途とする。分析・指導UIはSA/CT側で作る。

### 11.3 実装状況

Service／Bridge 0.6.0がプロトコル2.3を実装している。確認用の対話ツールsio_cli（`SIO_CLI_Specification_ja.md`）とクライアントライブラリsio_client（`SIO_Client_Library_Specification_ja.md`）も同じ環境で動作を確認した。REAPER 7.86、Windows 11、Python 3.11で、Service単体試験（偽Bridge使用）と実機受入試験（11.1の各項目）を通過した。

**未検証：**

- プールされたMIDIソースの検出（実際のプール済みアイテムでは未確認）。
- テンポ無視・時間タイムベースのテイク。
- 再生中の位置ジャンプ、タブ切替、録音介入による管理解除（人間の停止による解除は確認済み）。
- 録音中の編集・選択・select_projectの拒否。
- 弱起・不完全小節・小節途中の拍子変更に対するUNSUPPORTED_PROJECT。
- 100,000音符規模でのUI停止時間。
- 録音中のcreate_project・add_track・add_itemの拒否。add_itemのWRITE_FAILED（作成後の確認の失敗）。
- 他社製の音源プラグインでのadd_track（同梱のReaSynthとReaEQでのみ確認）。
- 既定のプロジェクトテンプレートを設定した環境でのcreate_project。
- macOS、Linux。

## 12. 参照資料

- [REAPER ReaScript](https://www.reaper.fm/sdk/reascript/reascript.php)
- [REAPER API functions](https://www.reaper.fm/sdk/reascript/reascripthelp.html)

実装対象REAPERが生成するヘルプでAPI引数と単位を確認する。SIOの通信形式、名前解決、付番、制約、期限は本設計の決定でありREAPER標準プロトコルではない。
