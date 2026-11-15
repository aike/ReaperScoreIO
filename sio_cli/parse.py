"""コマンド行の字句解析と、位置・範囲・音符指定の短縮記法の解釈。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sio_client import pitch_number


class CommandError(Exception):
    """送信せずに表示する入力エラー。"""


@dataclass
class Token:
    """空白で区切った 1 語。chars は (文字, 引用符の中か) の列。"""

    chars: list[tuple[str, bool]]

    @property
    def text(self) -> str:
        return "".join(c for c, _ in self.chars)

    @property
    def quoted(self) -> bool:
        return any(q for _, q in self.chars)

    def find(self, ch: str, last: bool = False, before: int | None = None) -> int:
        """引用符の外にある ch の位置。なければ -1。"""
        end = len(self.chars) if before is None else before
        indexes = [i for i in range(end) if self.chars[i] == (ch, False)]
        if not indexes:
            return -1
        return indexes[-1] if last else indexes[0]

    def split_at(self, index: int, width: int = 1) -> tuple["Token", "Token"]:
        return Token(self.chars[:index]), Token(self.chars[index + width:])


def tokenize(line: str) -> list[Token]:
    tokens: list[Token] = []
    current: list[tuple[str, bool]] | None = None
    in_quote = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == "\\" and i + 1 < len(line) and line[i + 1] in '"\\':
                current.append((line[i + 1], True))  # type: ignore[union-attr]
                i += 2
                continue
            if ch == '"':
                in_quote = False
            else:
                current.append((ch, True))  # type: ignore[union-attr]
        elif ch == '"':
            in_quote = True
            if current is None:
                current = []
            # 空文字列 "" も 1 語として残すための印
            current.append(("", True))
        elif ch.isspace():
            if current is not None:
                tokens.append(Token(current))
                current = None
        else:
            if current is None:
                current = []
            current.append((ch, False))
        i += 1
    if in_quote:
        raise CommandError('引用符 " が閉じていません。')
    if current is not None:
        tokens.append(Token(current))
    return tokens


@dataclass
class ParsedLine:
    name: str
    args: list[Token]
    options: dict[str, Token]
    redirect: str | None


OPTION_NAME = re.compile(r"^[a-z_]+$")


def parse_line(line: str) -> ParsedLine | None:
    """1 行を「コマンド名、位置引数、名前=値、> ファイル」に分ける。空行・コメントは None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    tokens = tokenize(stripped)
    redirect: str | None = None
    for i, tok in enumerate(tokens):
        if not tok.chars or tok.chars[0] != (">", False):
            continue
        rest = Token(tok.chars[1:])
        tail = tokens[i + 1:]
        if rest.chars:
            if tail:
                raise CommandError("> ファイル の後ろに引数は書けません。")
            redirect = rest.text
        else:
            if len(tail) != 1:
                raise CommandError("> の後ろには保存先のファイル名を 1 つ書いてください。")
            redirect = tail[0].text
        tokens = tokens[:i]
        break
    if not tokens:
        raise CommandError("コマンド名がありません。")
    if tokens[0].quoted:
        raise CommandError("コマンド名は引用符で囲めません。")
    name = tokens[0].text
    args: list[Token] = []
    options: dict[str, Token] = {}
    for tok in tokens[1:]:
        eq = tok.find("=")
        if eq > 0:
            key, value = tok.split_at(eq)
            if not key.quoted and OPTION_NAME.match(key.text):
                if key.text in options:
                    raise CommandError(f"{key.text}= が重複しています。")
                options[key.text] = value
                continue
        args.append(tok)
    return ParsedLine(name, args, options, redirect)


# ---------------------------------------------------------------- 値の解釈


def parse_bool(text: str, what: str) -> bool:
    if text == "true":
        return True
    if text == "false":
        return False
    raise CommandError(f"{what} は true か false で指定してください: {text}")


def parse_int(text: str, what: str) -> int:
    if not re.fullmatch(r"-?\d+", text):
        raise CommandError(f"{what} は整数で指定してください: {text}")
    return int(text)


def parse_number(text: str, what: str) -> int | float:
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?(\d+\.\d*|\.\d+)", text):
        return float(text)
    raise CommandError(f"{what} は数値で指定してください: {text}")


def parse_pitch(text: str, what: str = "音高") -> int:
    """整数、または音名（C4 = 60）。音名の解釈は sio_client と共通。

    整数は範囲を検査せずに送る（範囲外に対する SIO のエラー応答を確認できるようにするため）。
    """
    if re.fullmatch(r"\d+", text):
        return int(text)
    try:
        return pitch_number(text)
    except ValueError:
        pass
    raise CommandError(f"{what} は 60 のような整数か C4 のような音名で指定してください: {text}")


def parse_position(text: str, what: str = "位置") -> dict[str, Any]:
    """13.5 → {qn}、4:2 / 4:2+0.5 → {bar, beat, offset_qn}、b4 → {bar: 4, beat: 1}。"""
    m = re.fullmatch(r"b(\d+)", text)
    if m:
        return {"bar": int(m.group(1)), "beat": 1}
    m = re.fullmatch(r"(\d+):(\d+)(?:\+(\d+(?:\.\d*)?|\.\d+))?", text)
    if m:
        pos: dict[str, Any] = {"bar": int(m.group(1)), "beat": int(m.group(2))}
        if m.group(3) is not None:
            pos["offset_qn"] = parse_number(m.group(3), what)
        return pos
    try:
        return {"qn": parse_number(text, what)}
    except CommandError:
        raise CommandError(
            f"{what} は 13.5（QN）、4:2（小節:拍）、4:2+0.5、b4（小節の冒頭）の形で指定してください: {text}"
        ) from None


def parse_range(text: str, what: str = "範囲") -> dict[str, Any]:
    start, sep, end = text.partition("..")
    if not sep or not start or not end:
        raise CommandError(f"{what} は 開始..終了 の形で指定してください（例: b1..b3、0..8）: {text}")
    return {"start": parse_position(start, what + "の開始"), "end": parse_position(end, what + "の終了")}


def start_fields(text: str, what: str) -> dict[str, Any]:
    """開始位置を、QN なら start_qn、小節・拍なら start（Position）として返す。"""
    pos = parse_position(text, what)
    if "qn" in pos:
        return {"start_qn": pos["qn"]}
    return {"start": pos}


def split_part_prefix(tok: Token) -> tuple[str | None, str]:
    """[パート:]本体 を分ける。パートの区切りは @ より前（@ がなければ全体）で最後の : 。"""
    at = tok.find("@", last=True)
    colon = tok.find(":", last=True, before=at if at >= 0 else None)
    if colon < 0:
        if tok.quoted:
            raise CommandError(f"音符の指定を引用符で囲めるのはパート名の部分だけです: {tok.text}")
        return None, tok.text
    part, body = tok.split_at(colon)
    if body.quoted:
        raise CommandError(f"音符の指定を引用符で囲めるのはパート名の部分だけです: {tok.text}")
    if part.text == "":
        raise CommandError(f"パート名が空です: {tok.text}")
    return part.text, body.text


def parse_coordinate(body: str, what: str) -> dict[str, Any]:
    """音高@開始[/チャンネル] → {pitch, start_qn|start[, channel]}。"""
    pitch_text, sep, rest = body.partition("@")
    if not sep or not pitch_text or not rest:
        raise CommandError(f"{what} は 音高@開始位置 の形で指定してください（例: 60@16、C4@4:2）: {body}")
    result: dict[str, Any] = {"pitch": parse_pitch(pitch_text)}
    start_text, slash, channel = rest.rpartition("/")
    if slash:
        result.update(start_fields(start_text, "開始位置"))
        result["channel"] = parse_int(channel, "チャンネル")
    else:
        result.update(start_fields(rest, "開始位置"))
    return result


def parse_target(tok: Token) -> tuple[str | None, dict[str, Any]]:
    """[パート:]note_id または [パート:]音高@開始[/チャンネル]。戻り値は (part_name, target)。"""
    part, body = split_part_prefix(tok)
    if re.fullmatch(r"\d+", body):
        return part, {"note_id": int(body)}
    if "@" not in body:
        raise CommandError(f"音符は 5（note_id）か 60@16（音高@開始位置）の形で指定してください: {body}")
    return part, parse_coordinate(body, "音符")


def parse_expect(text: str) -> dict[str, Any]:
    """expect=音高[@開始][~長さ] → target.expect。"""
    head, tilde, duration = text.partition("~")
    pitch_text, at, start = head.partition("@")
    if not pitch_text or (at and not start) or (tilde and not duration):
        raise CommandError(f"expect は 60、60@16、60@16~1 の形で指定してください: {text}")
    expect: dict[str, Any] = {"pitch": parse_pitch(pitch_text, "expect の音高")}
    if at:
        expect.update(start_fields(start, "expect の開始位置"))
    if tilde:
        expect["duration_qn"] = parse_number(duration, "expect の長さ")
    return expect
