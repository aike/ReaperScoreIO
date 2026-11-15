"""3 和音を 4 小節分書き込み、それを読み取ってコードを解析する。

SIO が受け渡すのは音符（音高・位置・長さ）だけです。和音の名前や度数を求めるのは、
このプログラムの側の仕事です（analyze_triad と roman_numeral がその部分です）。

    python examples\\chord_analysis.py        （--no-play を付けると再生しません）

使っている機能: create_project / add_track / add_item / patch / notes(at=…)（ある時点で鳴っている音）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # pip install していなくても動くように

from sio_client import SIO, SIOConnectionError, pitch_name

# 書き込む和音（1 小節に 1 つ、全音符）。転回形を混ぜてある。
CHORDS = [
    ["C4", "E4", "G4"],     # 第1小節
    ["C4", "E4", "A4"],     # 第2小節
    ["F4", "A4", "D5"],     # 第3小節
    ["B3", "D4", "G4"],     # 第4小節
]

PITCH_CLASSES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# 根音から数えた第3音・第5音の半音数 → 和音の種類
QUALITIES = {(4, 7): ("", "長三和音"), (3, 7): ("m", "短三和音"), (3, 6): ("dim", "減三和音"),
             (4, 8): ("aug", "増三和音")}
INVERSIONS = ["基本形", "第1転回形", "第2転回形"]
# ハ長調の各音度（根音の音高クラス → 度数）
DEGREES = {0: "I", 2: "II", 4: "III", 5: "IV", 7: "V", 9: "VI", 11: "VII"}


def analyze_triad(pitches):
    """音高（MIDI ノート番号）の集まりを 3 和音として解析する。

    戻り値は (根音の音高クラス, 種類の記号, 種類の名前, 転回の番号)。3 和音でなければ None。
    """
    classes = sorted({p % 12 for p in pitches})
    if len(classes) != 3:
        return None
    bass = min(pitches) % 12
    for root in sorted(classes, key=lambda c: c != bass):         # 最低音を根音とする解釈を先に試す
        third, fifth = sorted((c - root) % 12 for c in classes if c != root)
        if (third, fifth) in QUALITIES:
            symbol, name = QUALITIES[(third, fifth)]
            inversion = [root, (root + third) % 12, (root + fifth) % 12].index(bass)
            return root, symbol, name, inversion
    return None


def roman_numeral(root, symbol):
    """ハ長調での度数（短三和音・減三和音は小文字で書く）。調の外の和音は "?"。"""
    degree = DEGREES.get(root)
    if degree is None:
        return "?"
    if symbol in ("m", "dim"):
        degree = degree.lower()
    return degree + {"dim": "o", "aug": "+"}.get(symbol, "")


def main() -> None:
    sio = SIO()
    try:
        if not sio.status().connected:
            raise SystemExit("REAPER とつながっていません。REAPER を起動してください。")
    except SIOConnectionError as e:
        raise SystemExit(str(e))

    # 1. 和音を書き込む。
    sio.create_project()                                # 新しい曲（4/4 拍子を想定）が対象になる
    sio.add_track("Chords", instrument="ReaSynth")
    sio.add_item(bars=(1, len(CHORDS)))
    with sio.patch(label="3 和音") as p:
        for bar, chord in enumerate(CHORDS, start=1):
            for pitch in chord:
                p.insert("Chords", pitch, (bar, 1), duration=4)

    # 2. 小節ごとに「その小節の頭で鳴っている音」を読み取り、解析する。
    print("小節  構成音        和音    度数  種類      転回")
    for bar in range(1, len(CHORDS) + 1):
        sounding = sio.notes("Chords", at=(bar, 1))
        pitches = sorted(n.pitch for n in sounding)
        names = " ".join(pitch_name(p) for p in pitches)
        result = analyze_triad(pitches)
        if result is None:
            print(f"{bar:>3}   {names:<12}  （3 和音ではありません）")
            continue
        root, symbol, kind, inversion = result
        chord_name = PITCH_CLASSES[root] + symbol
        if inversion:
            chord_name += "/" + PITCH_CLASSES[pitches[0] % 12]     # 転回形は最低音を添える
        print(f"{bar:>3}   {names:<12}  {chord_name:<6}  {roman_numeral(root, symbol):<4}  {kind}  "
              f"{INVERSIONS[inversion]}")

    if "--no-play" not in sys.argv:
        print("再生します…")
        sio.play(bars=(1, len(CHORDS)), wait=True)


if __name__ == "__main__":
    main()
