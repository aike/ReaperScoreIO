"""和声の課題を出し、ピアノロールに入力された解答を評価する（対話型の教材の最小の例）。

課題: 与えられたバスと和音（C - F - G - C）に、ソプラノを付ける。
プログラムがバスを書き込み、学習者が REAPER のピアノロールで Soprano に音符を入力します。
Enter を押すと解答を読み取り、和音に含まれない音を指摘して、その小節を鳴らします。

    python3 examples/harmony_exercise.py            （自分で解答を入力する）
    python3 examples/harmony_exercise.py --demo     （誤りを 1 つ含む解答を自動で入力し、直すところまで実演する）

使っている機能: 課題の用意（create_project / add_track / add_item / patch）、
                解答の読み取り（notes）、指摘（select_notes / play）、修正（update）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # pip install していなくても動くように

from sio_client import SIO, SIOConnectionError, pitch_number

# 課題: 小節ごとの (和音の名前, バスの音, 和音に含まれる音名)
EXERCISE = [
    ("C", "C3", ["C", "E", "G"]),
    ("F", "F3", ["F", "A", "C"]),
    ("G", "G3", ["G", "B", "D"]),
    ("C", "C3", ["C", "E", "G"]),
]
DEMO_ANSWER = ["E4", "A4", "C5", "C5"]      # 第3小節の C5 は、和音 G に含まれない
DEMO_FIX = {3: "B4"}                        # 実演で直す内容（小節 → 正しい音）
PLAY = "--no-play" not in sys.argv


def pitch_class(name: str) -> int:
    return pitch_number(name + "4") % 12


def prepare(sio: SIO) -> None:
    """課題を用意する: バスを書き込み、ソプラノには入力できる範囲だけを作る。"""
    sio.create_project()                                # 新しい曲（4/4 拍子を想定）が対象になる
    bars = (1, len(EXERCISE))
    sio.add_track("Bass", instrument="ReaSynth")
    sio.add_item(bars=bars)
    with sio.patch(label="課題のバス") as p:
        for bar, (_, bass, _) in enumerate(EXERCISE, start=1):
            p.insert("Bass", bass, (bar, 1), duration=4)
    sio.add_track("Soprano", instrument="ReaSynth")     # 最後に追加したトラックが REAPER 上で選択される
    sio.add_item(bars=bars)


def evaluate(sio: SIO):
    """解答を読み取り、(入力のない小節, 和音に含まれない音符) を返す。"""
    answer = sio.notes("Soprano", bars=(1, len(EXERCISE)))
    empty_bars, wrong_notes = [], []
    for bar, (_, _, tones) in enumerate(EXERCISE, start=1):
        in_bar = [n for n in answer if n.bar == bar]
        if not in_bar:
            empty_bars.append(bar)
        allowed = {pitch_class(t) for t in tones}
        wrong_notes += [n for n in in_bar if n.pitch % 12 not in allowed]
    return empty_bars, wrong_notes


def report(sio: SIO, empty_bars, wrong_notes) -> bool:
    """評価を伝える。全部正しければ True。"""
    for bar in empty_bars:
        print(f"  第{bar}小節: まだ音符がありません。")
    for note in wrong_notes:
        chord, _, tones = EXERCISE[note.bar - 1]
        print(f"  第{note.bar}小節第{note.beat}拍の {note.pitch_name}: "
              f"和音 {chord}（{'・'.join(tones)}）に含まれない音です。")
    sio.select_notes(wrong_notes)                       # 問題の音を選択状態にする（なければ選択を解除）
    if wrong_notes and PLAY:
        sio.play(bars=wrong_notes[0].bar, wait=True)    # 最初の問題の小節を鳴らす
    if not empty_bars and not wrong_notes:
        print("  すべて和音の音です。よくできました。")
        if PLAY:
            sio.play(bars=(1, len(EXERCISE)), wait=True)
        return True
    return False


def main() -> None:
    sio = SIO()
    try:
        if not sio.status().connected:
            raise SystemExit("REAPER とつながっていません。REAPER を起動してください。")
    except SIOConnectionError as e:
        raise SystemExit(str(e))

    prepare(sio)
    print("課題: バスに合わせて、Soprano のトラックに各小節 1 音以上のソプラノを入力してください。")
    print("和音: " + " - ".join(chord for chord, _, _ in EXERCISE))

    if "--demo" in sys.argv:
        # 学習者の代わりに、誤りを 1 つ含む解答を入力する。
        with sio.patch(label="解答（実演）") as p:
            for bar, pitch in enumerate(DEMO_ANSWER, start=1):
                p.insert("Soprano", pitch, (bar, 1), duration=4)
        print("提出 1 回目:")
        empty_bars, wrong_notes = evaluate(sio)
        report(sio, empty_bars, wrong_notes)
        # 指摘された音を直す。読み取った音符をそのまま渡すので、別の音を書き換える心配がない。
        for note in wrong_notes:
            sio.update(note, pitch=DEMO_FIX[note.bar], label="解答の修正（実演）")
        print("提出 2 回目:")
        report(sio, *evaluate(sio))
        return

    while True:
        if input("入力したら Enter（やめるときは q）: ").strip().lower() == "q":
            break
        if report(sio, *evaluate(sio)):
            break


if __name__ == "__main__":
    main()
