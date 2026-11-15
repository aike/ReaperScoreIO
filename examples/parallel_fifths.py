"""2 声の連続 5 度・連続 8 度を見つけ、REAPER 上で示して鳴らす。

対象は、いま REAPER で表に出ている曲の「Soprano」と「Bass」という 2 つのパートです。
--demo を付けると、連続 5 度を 1 か所含む例を新しい曲に作ってから調べます。

    python examples\\parallel_fifths.py --demo      （--no-play を付けると再生しません）
    python examples\\parallel_fifths.py              （自分で入力した曲を調べる）

使っている機能: notes / select_notes（問題の音を選択状態にして示す）/ play（その箇所を鳴らす）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # pip install していなくても動くように

from sio_client import SIO, PartNotFound, SIOConnectionError

UPPER, LOWER = "Soprano", "Bass"
PERFECT = {0: "8 度", 7: "5 度"}        # 音程（半音数を 12 で割った余り）→ 名前

# --demo で作る例（1 小節に 1 音、全音符）。第1〜2小節が C-G → D-A の連続 5 度になる。
DEMO = {UPPER: ["G4", "A4", "G4", "E4"], LOWER: ["C3", "D3", "E3", "C3"]}


def make_demo(sio: SIO) -> None:
    sio.create_project()
    for part, pitches in DEMO.items():
        sio.add_track(part, instrument="ReaSynth")
        sio.add_item(bars=(1, len(pitches)))
        with sio.patch(label=f"{part} の例") as p:
            for bar, pitch in enumerate(pitches, start=1):
                p.insert(part, pitch, (bar, 1), duration=4)


def sounding_at(notes, position):
    """position の時点で鳴っている音符（なければ None）。"""
    for note in notes:
        if note.start <= position < note.end:
            return note
    return None


def find_parallels(upper, lower):
    """連続 5 度・8 度の一覧。各要素は (音程の名前, 前の上声, 前の下声, 後の上声, 後の下声)。"""
    # どちらかの声部が動く時点ごとに、2 声の組を作る。
    pairs = []
    for position in sorted({n.start for n in upper} | {n.start for n in lower}):
        u, l = sounding_at(upper, position), sounding_at(lower, position)
        if u and l and (not pairs or (u, l) != pairs[-1]):
            pairs.append((u, l))
    found = []
    for (u1, l1), (u2, l2) in zip(pairs, pairs[1:]):
        interval = (u1.pitch - l1.pitch) % 12
        same_interval = interval in PERFECT and (u2.pitch - l2.pitch) % 12 == interval
        both_move = u1.pitch != u2.pitch and l1.pitch != l2.pitch
        same_direction = (u2.pitch - u1.pitch) * (l2.pitch - l1.pitch) > 0
        if same_interval and both_move and same_direction:
            found.append((PERFECT[interval], u1, l1, u2, l2))
    return found


def main() -> None:
    sio = SIO()
    try:
        if not sio.status().connected:
            raise SystemExit("REAPER とつながっていません。REAPER を起動してください。")
    except SIOConnectionError as e:
        raise SystemExit(str(e))

    if "--demo" in sys.argv:
        make_demo(sio)
    try:
        upper, lower = sio.notes(UPPER), sio.notes(LOWER)
    except PartNotFound as e:
        raise SystemExit(f"パート「{e.details.get('part_name')}」がありません。"
                         f"{UPPER} と {LOWER} のトラックを用意するか、--demo を付けてください。")

    found = find_parallels(upper, lower)
    if not found:
        print("連続 5 度・連続 8 度は見つかりませんでした。")
        sio.select_notes()                              # 選択を解除する
        return
    for name, u1, l1, u2, l2 in found:
        print(f"連続 {name}: 第{u1.bar}小節第{u1.beat}拍（{l1.pitch_name}-{u1.pitch_name}）"
              f" → 第{u2.bar}小節第{u2.beat}拍（{l2.pitch_name}-{u2.pitch_name}）")

    # 最初の 1 か所を、REAPER のピアノロール上で選択状態にして示し、その小節を鳴らす。
    name, u1, l1, u2, l2 = found[0]
    sio.select_notes([u1, l1, u2, l2])
    if "--no-play" not in sys.argv:
        print(f"第{u1.bar}〜{u2.bar}小節を再生します…")
        sio.play(bars=(u1.bar, u2.bar), wait=True)


if __name__ == "__main__":
    main()
