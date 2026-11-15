# sio_client のサンプルプログラム

Python のライブラリ sio_client を使った、短いプログラムの例です。それぞれ 1 つのファイルで完結していて、上から順に読めます。

## 実行する前に

1. REAPER を起動しておきます。
2. SIO Service を起動しておきます（`.\.venv\Scripts\python -m sio_service`）。
3. SIO のフォルダで、次のように実行します。

```powershell
python examples\frog_round.py
```

sio_client を `pip install` していなくても、このフォルダの中からなら動きます。

どの例も、**新しい曲のタブを REAPER に作ってから** 音符を書き込みます。開いている曲には手を加えません。作った曲は保存しないので、終わったら REAPER でタブを閉じてください（残したい場合は保存します）。音源には、REAPER に付属の ReaSynth を使います。

`--no-play` を付けると、再生をせずに終わります。

## 例の一覧

| ファイル | 内容 | 主に使う機能 |
|---|---|---|
| [frog_round.py](frog_round.py) | 「かえるの歌」を 2 つのトラックで輪唱する。2 小節ずれた同じ旋律を書き込み、再生する | トラックと入力範囲の用意、複数の音符の書き込み、再生 |
| [chord_analysis.py](chord_analysis.py) | 3 和音を 4 小節分書き込み、読み取って、和音の名前・度数・転回形を表示する | ある時点で鳴っている音の読み取り |
| [parallel_fifths.py](parallel_fifths.py) | 2 声（Soprano と Bass）の連続 5 度・連続 8 度を見つけ、その音を REAPER 上で選択状態にして、その小節を鳴らす | 読み取り、音符の選択、範囲の再生 |
| [harmony_exercise.py](harmony_exercise.py) | 和声の課題を出し、学習者がピアノロールに入力した解答を評価する。対話型の教材の最小の例 | 課題の用意、解答の読み取り、指摘、修正 |

### frog_round.py

```
58 個の音符を書き込みました。
Frog 1: 29 音、第1小節から第8小節まで
Frog 2: 29 音、第3小節から第10小節まで
再生します…
```

旋律は「(音名, 長さ)」の並びとして書いてあります。`MELODY` を書き換えれば別の曲に、`VOICES` に名前を足せば 3 声以上の輪唱になります。

### chord_analysis.py

```
小節  構成音        和音    度数  種類      転回
  1   C4 E4 G4      C       I     長三和音  基本形
  2   C4 E4 A4      Am/C    vi    短三和音  第1転回形
  3   F4 A4 D5      Dm/F    ii    短三和音  第1転回形
  4   B3 D4 G4      G/B     V     長三和音  第1転回形
```

SIO が受け渡すのは音符だけです。和音の名前や度数を求める部分（`analyze_triad`、`roman_numeral`）は、このプログラムに書いてあります。分析の規則を変えたいときは、ここを書き換えます。度数はハ長調として求めています。

### parallel_fifths.py

```powershell
python examples\parallel_fifths.py --demo     # 連続 5 度を含む例を作ってから調べる
python examples\parallel_fifths.py            # いま表に出ている曲の Soprano と Bass を調べる
```

```
連続 5 度: 第1小節第1拍（C3-G4） → 第2小節第1拍（D3-A4）
第1〜2小節を再生します…
```

`--demo` を付けない場合は、REAPER で表に出ている曲に「Soprano」と「Bass」という名前のトラックが必要です。自分で入力した 2 声を調べられます。

### harmony_exercise.py

```powershell
python examples\harmony_exercise.py           # 自分で解答を入力する
python examples\harmony_exercise.py --demo    # 誤りを含む解答を自動で入力し、直すところまで実演する
```

```
課題: バスに合わせて、Soprano のトラックに各小節 1 音以上のソプラノを入力してください。
和音: C - F - G - C
提出 1 回目:
  第3小節第1拍の C5: 和音 G（G・B・D）に含まれない音です。
提出 2 回目:
  すべて和音の音です。よくできました。
```

`--demo` を付けない場合は、プログラムがバスを書き込んだあと、REAPER の Soprano トラックのアイテムをダブルクリックしてピアノロールを開き、音符を入力します。プログラムの画面で Enter を押すと、解答が評価されます。

評価の規則（`evaluate`）は「和音に含まれる音かどうか」だけの簡単なものです。実際の教材では、ここに声部進行や音域などの規則を足していきます。

## 自分のプログラムを書くときは

- メソッドと引数の一覧は [SIO_Client_Library_Specification_ja.md](../SIO_Client_Library_Specification_ja.md) にあります。
- どの例も、新しい曲は 4/4 拍子であることを前提にしています。REAPER の新規プロジェクトの設定を変えている場合は、`sio.score_info().meters` で拍子を確認してください。
