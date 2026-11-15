"""「かえるの歌」を 2 つのトラックで輪唱する。

新しい曲を作り、音源を載せたトラックを 2 本用意して、2 小節ずれた同じ旋律を書き込み、再生します。

    python examples\\frog_round.py            （--no-play を付けると再生しません）

使っている機能: create_project / add_track / add_item / patch（複数の音符を 1 回で書く）/ play
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # pip install していなくても動くように

from sio_client import SIO, SIOConnectionError

# 旋律: (音名, 長さ)。長さは四分音符を 1 とする。None は休符。1 行が 1 小節（4/4 拍子）。
MELODY = [
    ("C4", 1), ("D4", 1), ("E4", 1), ("F4", 1),                     # かえるの
    ("E4", 1), ("D4", 1), ("C4", 1), (None, 1),                     # うたが
    ("E4", 1), ("F4", 1), ("G4", 1), ("A4", 1),                     # きこえて
    ("G4", 1), ("F4", 1), ("E4", 1), (None, 1),                     # くるよ
    ("C4", 1), (None, 1), ("C4", 1), (None, 1),                     # クヮ クヮ
    ("C4", 1), (None, 1), ("C4", 1), (None, 1),                     # クヮ クヮ
    ("C4", 0.5), ("C4", 0.5), ("D4", 0.5), ("D4", 0.5),             # ケケケケ
    ("E4", 0.5), ("E4", 0.5), ("F4", 0.5), ("F4", 0.5),             # ケケケケ
    ("E4", 1), ("D4", 1), ("C4", 1), (None, 1),                     # クヮクヮクヮ
]
BARS = 8            # 旋律の長さ（小節）
DELAY_BARS = 2      # 2 つ目の声部が遅れて入る小節数
VOICES = ["Frog 1", "Frog 2"]


def main() -> None:
    sio = SIO()
    try:
        if not sio.status().connected:
            raise SystemExit("REAPER とつながっていません。REAPER を起動してください。")
    except SIOConnectionError as e:
        raise SystemExit(str(e))

    sio.create_project()                                # 新しい曲（4/4 拍子を想定）が対象になる
    total_bars = BARS + DELAY_BARS * (len(VOICES) - 1)
    for name in VOICES:
        track = sio.add_track(name, instrument="ReaSynth")
        if not track.instrument_loaded:
            print(f"注意: {name} に音源を載せられませんでした（音は鳴りません）。")
        sio.add_item(bars=(1, total_bars))              # 追加したトラックに、音符を入力できる範囲を作る

    # 2 声部の音符をまとめて書き込む（REAPER の「元に戻す」1 回分になる）。
    with sio.patch(label="かえるの歌（輪唱）") as p:
        for voice, name in enumerate(VOICES):
            position = voice * DELAY_BARS * 4.0         # この声部が歌い始める位置（四分音符単位）
            for pitch, length in MELODY:
                if pitch is not None:
                    p.insert(name, pitch, position, duration=length)
                position += length
    print(f"{p.result.added} 個の音符を書き込みました。")

    # 書き込んだ結果を読み取って確かめる。
    for name in VOICES:
        notes = sio.notes(name)
        print(f"{name}: {len(notes)} 音、第{notes[0].bar}小節から第{notes[-1].bar}小節まで")

    if "--no-play" not in sys.argv:
        print("再生します…")
        sio.play(bars=(1, total_bars), wait=True)
    print("終わりました。曲は保存していません。残す場合は REAPER で保存してください。")


if __name__ == "__main__":
    main()
