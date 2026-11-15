"""実機受入試験: 本物の REAPER + SIO Bridge + SIO Service を通して仕様 11.1 の項目を確認する。

環境変数 SIO_E2E_REAPER_DIR に「ポータブル導入の REAPER フォルダ」（reaper.exe と reaper.ini がある
試験専用のコピー）を指定したときだけ実行する。普段使いの REAPER 設定には触れない。

    $env:SIO_E2E_REAPER_DIR = "C:\\path\\to\\reaper_portable"
    python -m pytest tests/test_e2e_reaper.py -v

試験は上から順に状態を引き継ぐ（1 つの REAPER セッションを使い回す）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sio_service.app import create_app
from sio_service.config import Config
from sio_service.install import install

REAPER_DIR = os.environ.get("SIO_E2E_REAPER_DIR")
pytestmark = pytest.mark.skipif(not REAPER_DIR, reason="SIO_E2E_REAPER_DIR が未設定")

TOKEN = "e2e-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

AGENT_LUA = r"""
local DIR = [[%s]]
local json = dofile(reaper.GetResourcePath() .. "/Scripts/SIO/sio_json.lua")
sio_test_json = json
local n = 0
local function loop()
  local f = io.open(DIR .. "/cmd_" .. (n + 1) .. ".lua", "rb")
  if f then
    local src = f:read("a"); f:close()
    n = n + 1
    local fn, err = load(src, "cmd", "t")
    local res
    if not fn then res = { ok = false, error = tostring(err) }
    else
      local ok, r = xpcall(fn, debug.traceback)
      if ok then res = { ok = true, value = (r == nil) and json.null or r }
      else res = { ok = false, error = tostring(r) } end
    end
    local tmp = DIR .. "/res_" .. n .. ".tmp"
    local out = io.open(tmp, "wb"); out:write(json.encode(res)); out:close()
    os.rename(tmp, DIR .. "/res_" .. n .. ".json")
  end
  reaper.defer(loop)
end
local r = io.open(DIR .. "/ready", "w"); r:write("ok"); r:close()
loop()
"""

PRELUDE = r"""
local json = sio_test_json
local function proj_named(name)
  local i = 0
  while true do
    local p = reaper.EnumProjects(i)
    if not p then error("no project " .. name) end
    if reaper.GetProjectName(p) == name then return p end
    i = i + 1
  end
end
local function track_named(proj, name)
  for i = 0, reaper.CountTracks(proj) - 1 do
    local tr = reaper.GetTrack(proj, i)
    local _, n = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
    if n == name then return tr end
  end
  error("no track " .. name)
end
local function add_track(proj, name)
  local idx = reaper.CountTracks(proj)
  reaper.InsertTrackInProject(proj, idx, 0)
  local tr = reaper.GetTrack(proj, idx)
  reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", name, true)
  return tr
end
local function add_item(tr, s_qn, e_qn)
  local item = reaper.CreateNewMIDIItemInProj(tr, s_qn, e_qn, true)
  return item, reaper.GetActiveTake(item)
end
-- MIDI_GetPPQPosFromProjQN 等は非アクティブな曲で誤るため、秒を経由して曲を明示する。
local function take_proj(take)
  return reaper.GetItemProjectContext(reaper.GetMediaItemTake_Item(take))
end
local function ppq_of(take, qn)
  return math.floor(reaper.MIDI_GetPPQPosFromProjTime(take, reaper.TimeMap2_QNToTime(take_proj(take), qn)) + 0.5)
end
local function qn_of(take, ppq)
  local qn = reaper.TimeMap2_timeToQN(take_proj(take), reaper.MIDI_GetProjTimeFromPPQPos(take, ppq))
  return math.floor(qn * 1e6 + 0.5) / 1e6
end
local function add_note(take, pitch, s_qn, dur, opts)
  opts = opts or {}
  local s = ppq_of(take, s_qn)
  local e = ppq_of(take, s_qn + dur)
  reaper.MIDI_InsertNote(take, false, opts.muted or false, s, e, (opts.chan or 1) - 1, pitch, opts.vel or 80, true)
end
local function take_of(proj, track, item_index)
  return reaper.GetActiveTake(reaper.GetTrackMediaItem(track_named(proj, track), item_index))
end
"""


def _dismiss_dialogs(pid: int) -> None:
    """ポータブル初回起動の「VST を走査しますか」ダイアログに「いいえ」で答える。"""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def on_child(hwnd, _):
        if text(hwnd).startswith(("いいえ", "&No", "No")):
            user32.SendMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
            return False
        return True

    def on_top(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and text(hwnd) == "Portable installation":
            user32.EnumChildWindows(hwnd, proto(on_child), 0)
        return True

    user32.EnumWindows(proto(on_top), 0)


class Reaper:
    def __init__(self, reaper_dir: Path, work: Path) -> None:
        self.dir = reaper_dir
        self.work = work
        self.agent_dir = work / "agent"
        self.agent_dir.mkdir(parents=True)
        self.n = 0
        (self.agent_dir / "agent.lua").write_text(AGENT_LUA % self.agent_dir, encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(reaper_dir / "reaper.exe"), "-nosplash", "-newinst", "-ignoreerrors",
             str(self.agent_dir / "agent.lua")])
        deadline = time.time() + 90
        while not (self.agent_dir / "ready").exists():
            if time.time() > deadline:
                self.close()
                raise RuntimeError("REAPER の試験エージェントが起動しません")
            _dismiss_dialogs(self.proc.pid)
            time.sleep(0.5)

    def lua(self, code: str, timeout: float = 30.0):
        """REAPER 内で Lua を実行し、戻り値（JSON 化可能な値）を返す。"""
        self.n += 1
        tmp = self.agent_dir / f"cmd_{self.n}.tmp"
        tmp.write_text(PRELUDE + code, encoding="utf-8")
        os.rename(tmp, self.agent_dir / f"cmd_{self.n}.lua")
        res = self.agent_dir / f"res_{self.n}.json"
        deadline = time.time() + timeout
        while not res.exists():
            if time.time() > deadline:
                raise TimeoutError(code[:200])
            time.sleep(0.02)
        doc = json.loads(res.read_text(encoding="utf-8"))
        if not doc["ok"]:
            raise RuntimeError(doc["error"])
        return doc["value"]

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def reaper(tmp_path_factory):
    reaper_dir = Path(REAPER_DIR)
    assert (reaper_dir / "reaper.ini").exists(), "ポータブル導入（reaper.ini を持つフォルダ）を指定してください"
    install(reaper_dir)  # Scripts/SIO と __startup.lua による自動起動を導入
    work = tmp_path_factory.mktemp("sio_e2e")
    r = Reaper(reaper_dir, work)
    yield r
    if not os.environ.get("SIO_E2E_KEEP"):  # 調査用: REAPER を終了せずに残す
        r.close()


@pytest.fixture(scope="module")
def config():
    return Config(resource_path=Path(REAPER_DIR), token=TOKEN)


@pytest.fixture(scope="module")
def client(reaper, config):
    with TestClient(create_app(config)) as c:
        deadline = time.time() + 20
        while time.time() < deadline:
            if call(c, "get_status")["bridge_state"] == "connected":
                break
            time.sleep(0.2)
        yield c


def rpc(client, method, params=None, request_id=None):
    body = {"protocol_version": "2.3", "request_id": request_id or str(uuid.uuid4()),
            "method": method, "params": params or {}}
    r = client.post("/v2/rpc", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def call(client, method, params=None):
    body = rpc(client, method, params)
    assert body["ok"] is True, body
    return body["result"]


def code(client, method, params=None):
    body = rpc(client, method, params)
    assert body["ok"] is False, body
    return body["error"]["code"]


def brief(notes):
    return [(n["part_name"], n["note_id"], n["pitch"], n["start_qn"], n["duration_qn"]) for n in notes]


VIOLIN = [("Violin I", 1, 60, 0.0, 1.0), ("Violin I", 2, 64, 0.0, 1.0), ("Violin I", 3, 62, 1.0, 1.0),
          ("Violin I", 4, 67, 4.0, 4.0), ("Violin I", 5, 72, 8.0, 6.0), ("Violin I", 6, 60, 16.0, 1.0),
          ("Violin I", 7, 65, 22.5, 0.5)]
CELLO_ALL = [("Cello", 1, 36, 0.0, 4.0), ("Cello", 2, 43, 4.0, 4.0), ("Cello", 3, 48, 13.0, 1.0),
             ("Cello", 4, 36, 16.0, 3.0)]
CELLO = [n for n in CELLO_ALL if n[1] != 2]  # note 2 は音符ミュート


# ------------------------------------------------------------------ 1. 常駐と選択


def test_autostart_and_status(client):
    st = call(client, "get_status")
    assert st["bridge_state"] == "connected"
    assert st["bridge_version"] == "0.6.0" and st["protocol_version"] == "2.3"
    assert st["running_request_id"] is None and st["playback_active"] is False


def test_unnamed_active_project_is_the_target(client):
    projects = call(client, "list_projects")["projects"]
    assert projects == [{"project_name": "", "active": True, "addressable": False}]
    info = call(client, "get_score_info")
    assert info["project_name"] == "" and info["parts"] == [] and info["note_range"] is None
    assert info["note_range_complete"] is True
    assert info["meters"] == [{"start_qn": 0.0, "bar": 1, "numerator": 4, "denominator": 4}]


def test_build_scenario(reaper):
    d = reaper.work
    for sub, name in (("a", "Harmony.rpp"), ("a", "Study.rpp"), ("b", "Study.rpp"), ("a", "Temp.rpp")):
        (d / sub).mkdir(exist_ok=True)
        (d / sub / name).write_text('<REAPER_PROJECT 0.1 "7.0/win64" 1700000000\n>\n', encoding="utf-8")
    reaper.lua(r"""reaper.Main_openProject("noprompt:" .. [[%s]])""" % (d / "a" / "Harmony.rpp"))
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.SetTempoTimeSigMarker(proj, -1, 0, -1, -1, 120, 4, 4, false)
      reaper.SetTempoTimeSigMarker(proj, -1, -1, 2, 0, 90, 0, 0, false)   -- 第3小節からテンポ変更
      reaper.SetTempoTimeSigMarker(proj, -1, -1, 4, 0, 90, 3, 4, false)   -- 第5小節から 3/4
      reaper.SetTempoTimeSigMarker(proj, -1, -1, 6, 0, 90, 6, 8, false)   -- 第7小節から 6/8
      reaper.UpdateTimeline()
    """)
    # 試験データは Undo 履歴に記録しておく（記録のない変更は、後の Undo で巻き戻されてしまう）。
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.Undo_BeginBlock2(proj)
      local vn = add_track(proj, "Violin I")
      local _, a = add_item(vn, 0, 8)
      add_note(a, 64, 0, 1); add_note(a, 60, 0, 1); add_note(a, 62, 1, 1); add_note(a, 67, 4, 4)
      reaper.MIDI_Sort(a)
      local _, b = add_item(vn, 8, 28)
      add_note(b, 72, 8, 6); add_note(b, 60, 16, 1); add_note(b, 65, 22.5, 0.5)
      reaper.MIDI_Sort(b)
      local vc = add_track(proj, "Cello")
      local _, c = add_item(vc, 0, 28)
      add_note(c, 36, 0, 4); add_note(c, 43, 4, 4, { muted = true }); add_note(c, 48, 13, 1, { vel = 100, chan = 2 })
      add_note(c, 36, 16, 3)
      reaper.MIDI_InsertCC(c, false, false, ppq_of(c, 2), 0xB0, 0, 64, 127)
      reaper.MIDI_Sort(c)
      reaper.AddMediaItemToTrack(vc)   -- MIDI 以外（空）のアイテム
      reaper.Undo_EndBlock2(proj, "fixture", -1)
    """)
    # 2 つ目のタブ（こちらがアクティブになる）
    reaper.lua(r"""
      reaper.Main_OnCommand(40859, 0)
      reaper.Main_openProject("noprompt:" .. [[%s]])
    """ % (d / "a" / "Study.rpp"))
    reaper.lua(r"""add_track(proj_named("Study.rpp"), "Solo")""")


ACTIVE_NAME = r"""return reaper.GetProjectName(reaper.EnumProjects(-1))"""

# アクティブな曲で、REAPER 上で選択状態になっているトラックの名前
SELECTED_TRACKS = r"""
  local proj = reaper.EnumProjects(-1)
  local out = json.array({})
  for i = 0, reaper.CountTracks(proj) - 1 do
    local tr = reaper.GetTrack(proj, i)
    if reaper.IsTrackSelected(tr) then
      local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
      out[#out + 1] = name
    end
  end
  return out
"""


def test_select_project_activates_tab_by_exact_name(client, reaper):
    names = [(p["project_name"], p["active"], p["addressable"]) for p in call(client, "list_projects")["projects"]]
    assert names == [("Harmony.rpp", False, True), ("Study.rpp", True, True)]
    assert code(client, "select_project", {"project_name": "harmony.rpp"}) == "PROJECT_NOT_FOUND"
    assert code(client, "select_project", {"project_name": "Harmony"}) == "PROJECT_NOT_FOUND"
    # 失敗した select_project はタブを切り替えない。対象は表のタブ（Study.rpp）のまま。
    assert call(client, "get_score_info")["project_name"] == "Study.rpp"
    # select_project は、その曲のタブを表に出す。以後の要求はその曲を対象にする。
    res = call(client, "select_project", {"project_name": "Harmony.rpp"})
    assert res == {"project_name": "Harmony.rpp", "selected_part_name": None}
    assert [p["active"] for p in call(client, "list_projects")["projects"]] == [True, False]
    assert reaper.lua(ACTIVE_NAME) == "Harmony.rpp"


def test_score_info(client):
    info = call(client, "get_score_info")
    assert info["project_name"] == "Harmony.rpp" and info["selected_part_name"] is None
    assert info["parts"] == [
        {"part_name": "Violin I", "order": 1, "addressable": True, "selected": False},
        {"part_name": "Cello", "order": 2, "addressable": True, "selected": False},
    ]
    assert info["meters"] == [
        {"start_qn": 0.0, "bar": 1, "numerator": 4, "denominator": 4},
        {"start_qn": 16.0, "bar": 5, "numerator": 3, "denominator": 4},
        {"start_qn": 22.0, "bar": 7, "numerator": 6, "denominator": 8},
    ]
    assert info["note_range"] == {"start_qn": 0.0, "end_qn": 23.0}
    assert info["note_range_complete"] is True and info["constraints"] == []
    assert [(w["code"], w["details"]["part_name"], w["details"]["count"]) for w in info["warnings"]] == \
        [("AUDIO_ITEMS_IGNORED", "Cello", 1)]


# ------------------------------------------------------------------ 2. 取得


def test_get_all_notes(client):
    res = call(client, "get_notes")  # パート未選択 → 全パート。
    assert res["project_name"] == "Harmony.rpp" and res["time_unit"] == "quarter_note"
    assert res["parts"] == [{"part_name": "Violin I", "order": 1}, {"part_name": "Cello", "order": 2}]
    assert brief(res["notes"]) == VIOLIN + CELLO
    assert res["range"] == {"start_qn": 0.0, "end_qn": 23.0}
    assert [m["bar"] for m in res["meters"]] == [1, 5, 7]
    by_id = {(n["part_name"], n["note_id"]): n for n in res["notes"]}
    assert by_id[("Violin I", 6)]["position"] == {"bar": 5, "beat": 1, "offset_qn": 0.0}
    assert by_id[("Violin I", 7)]["position"] == {"bar": 7, "beat": 2, "offset_qn": 0.0}   # 6/8 の 1 拍は 0.5
    assert by_id[("Violin I", 5)]["position"] == {"bar": 3, "beat": 1, "offset_qn": 0.0}
    cello3 = by_id[("Cello", 3)]
    assert cello3["position"] == {"bar": 4, "beat": 2, "offset_qn": 0.0}
    assert (cello3["velocity"], cello3["channel"], cello3["muted"], cello3["selected"]) == (100, 2, False, False)
    assert set(cello3) == {"note_id", "part_name", "pitch", "start_qn", "duration_qn", "position", "velocity",
                           "channel", "muted", "item_muted", "track_muted", "selected"}
    assert [w["code"] for w in res["warnings"]] == ["AUDIO_ITEMS_IGNORED"]


def test_mute_filter_keeps_numbering(client):
    res = call(client, "get_notes", {"part_names": ["Cello"], "include_muted": True})
    assert brief(res["notes"]) == CELLO_ALL
    assert [n["muted"] for n in res["notes"]] == [False, True, False, False]


def test_ranges_and_relations(client):
    def ids(params):
        return [(n["part_name"], n["note_id"]) for n in call(client, "get_notes", params)["notes"]]

    bars_1_2 = {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}}
    res = call(client, "get_notes", {"range": bars_1_2})
    assert res["range"] == {"start_qn": 0.0, "end_qn": 8.0}
    assert brief(res["notes"]) == VIOLIN[:4] + CELLO[:1]
    assert [m["bar"] for m in res["meters"]] == [1]
    # 範囲前からの持続音を含み、終了境界の音符は含めない。部分取得でも番号は変わらない。
    assert ids({"range": {"start": {"qn": 6}, "end": {"qn": 9}}}) == [("Violin I", 4), ("Violin I", 5)]
    assert ids({"range": {"start": {"qn": 8}, "end": {"qn": 9}}}) == [("Violin I", 5)]
    assert ids({"range": {"start": {"qn": 4}, "end": {"qn": 8}}, "relation": "starts_in"}) == [("Violin I", 4)]
    assert ids({"range": {"start": {"qn": 5}, "end": {"qn": 8}}, "relation": "starts_in"}) == []
    assert ids({"at": {"bar": 4, "beat": 2}, "relation": "sounding_at"}) == [("Violin I", 5), ("Cello", 3)]
    assert ids({"at": {"qn": 8}, "relation": "sounding_at"}) == [("Violin I", 5)]
    assert ids({"at": {"bar": 7, "beat": 2}, "relation": "sounding_at"}) == [("Violin I", 7)]
    empty = call(client, "get_notes", {"range": {"start": {"bar": 9, "beat": 1}, "end": {"bar": 10, "beat": 1}}})
    assert empty["notes"] == [] and empty["range"] == {"start_qn": 28.0, "end_qn": 31.0}
    nothing = call(client, "get_notes", {"at": {"qn": 27}, "relation": "sounding_at"})
    assert nothing["notes"] == [] and nothing["range"] is None and len(nothing["meters"]) == 3


def test_invalid_positions(client):
    def bad(params):
        return code(client, "get_notes", params)

    assert bad({"at": {"bar": 5, "beat": 4}, "relation": "sounding_at"}) == "INVALID_POSITION"      # 3/4 に第4拍なし
    assert bad({"at": {"bar": 7, "beat": 1, "offset_qn": 0.5}, "relation": "sounding_at"}) == "INVALID_POSITION"
    assert bad({"range": {"start": {"bar": 3, "beat": 1}, "end": {"qn": 8}}}) == "INVALID_POSITION"


def test_part_selection_defaults(client, reaper):
    assert code(client, "select_part", {"part_name": "cello"}) == "PART_NOT_FOUND"
    assert reaper.lua(SELECTED_TRACKS) == []
    assert call(client, "select_part", {"part_name": "Cello"}) == \
        {"project_name": "Harmony.rpp", "selected_part_name": "Cello"}
    # select_part は REAPER 上でもそのトラックだけを選択状態にする。
    assert reaper.lua(SELECTED_TRACKS) == ["Cello"]
    # 逆方向には連動しない: 人間が別のトラックを選択しても、SIO の既定パートは変わらない。
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.SetOnlyTrackSelected(track_named(proj, "Violin I"))
    """)
    assert call(client, "get_score_info")["selected_part_name"] == "Cello"
    assert brief(call(client, "get_notes", {"selected_part": True})["notes"]) == CELLO
    # 人間が複数のトラックを選択していても、select_part の後は 1 本だけが選択される。
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.SetTrackSelected(track_named(proj, "Cello"), true)
    """)
    assert reaper.lua(SELECTED_TRACKS) == ["Violin I", "Cello"]
    assert call(client, "select_part", {"part_name": "Violin I"})["selected_part_name"] == "Violin I"
    assert reaper.lua(SELECTED_TRACKS) == ["Violin I"]
    assert call(client, "select_part", {"part_name": "Cello"})["selected_part_name"] == "Cello"
    assert reaper.lua(SELECTED_TRACKS) == ["Cello"]
    # 既定は選択中パートがあっても常に全パート。選択中パートは selected_part:true で明示する。
    assert brief(call(client, "get_notes")["notes"]) == VIOLIN + CELLO
    assert brief(call(client, "get_notes", {"selected_part": True})["notes"]) == CELLO
    assert brief(call(client, "get_notes", {"part_names": ["Violin I"]})["notes"]) == VIOLIN
    # expect_project_name は取り違えの検出であり、曲を切り替えない。
    ok = call(client, "get_notes", {"selected_part": True, "expect_project_name": "Harmony.rpp"})
    assert brief(ok["notes"]) == CELLO
    mismatch = rpc(client, "get_notes", {"expect_project_name": "Study.rpp"})["error"]
    assert mismatch["code"] == "PROJECT_MISMATCH"
    assert mismatch["details"] == {"expected": "Study.rpp", "actual": "Harmony.rpp"}
    assert code(client, "select_part", {"part_name": "Violin I", "expect_project_name": "Study.rpp"}) \
        == "PROJECT_MISMATCH"
    assert code(client, "get_score_info", {"expect_project_name": "Study.rpp"}) == "PROJECT_MISMATCH"
    assert code(client, "get_notes", {"part_names": ["Viola"]}) == "PART_NOT_FOUND"
    info = call(client, "get_score_info")
    assert info["selected_part_name"] == "Cello" and [p["selected"] for p in info["parts"]] == [False, True]
    # 同じ曲の再選択ではパート選択を維持する。
    assert call(client, "select_project", {"project_name": "Harmony.rpp"})["selected_part_name"] == "Cello"
    # 既定パートは曲ごとに覚える。別の曲を表に出すとその曲の既定パート（未選択）になり、戻ると元に戻る。
    assert call(client, "select_project", {"project_name": "Study.rpp"}) == \
        {"project_name": "Study.rpp", "selected_part_name": None}
    assert call(client, "get_score_info")["project_name"] == "Study.rpp"
    assert code(client, "get_notes", {"selected_part": True}) == "PART_NOT_SELECTED"
    # 人間がタブを切り替えても、表のタブの曲が対象になる。
    reaper.lua(r"""reaper.SelectProjectInstance(proj_named("Harmony.rpp"))""")
    info = call(client, "get_score_info")
    assert (info["project_name"], info["selected_part_name"]) == ("Harmony.rpp", "Cello")
    time.sleep(1.2)
    st = call(client, "get_status")
    assert (st["active_project_name"], st["selected_part_name"]) == ("Harmony.rpp", "Cello")


# ------------------------------------------------------------------ 3. 選択と編集


SELECTED = r"""
  local proj = proj_named("Harmony.rpp")
  local out = json.array({})
  for _, name in ipairs({ "Violin I", "Cello" }) do
    local tr = track_named(proj, name)
    for i = 0, reaper.CountTrackMediaItems(tr) - 1 do
      local take = reaper.GetActiveTake(reaper.GetTrackMediaItem(tr, i))
      if take then
        local _, n = reaper.MIDI_CountEvts(take)
        for k = 0, n - 1 do
          local _, sel, _, s, _, _, pitch = reaper.MIDI_GetNote(take, k)
          if sel then out[#out + 1] = name .. ":" .. math.floor(pitch) .. "@" .. qn_of(take, s) end
        end
      end
    end
  end
  return out
"""


def test_select_notes(client, reaper):
    res = call(client, "select_notes", {"targets": [
        {"target": {"note_id": 3}},                                         # 選択中パート（Cello）
        {"part_name": "Violin I", "target": {"pitch": 72, "start_qn": 8.0000000001}},
    ]})
    assert res == {"project_name": "Harmony.rpp", "selected_count": 2}
    assert sorted(reaper.lua(SELECTED)) == ["Cello:48@13.0", "Violin I:72@8.0"]
    # 座標指定の開始位置は小節・拍でも書ける（Cello の 48 は第4小節第2拍、Violin I の 65 は 6/8 の第7小節第2拍）。
    assert call(client, "select_notes", {"targets": [
        {"target": {"pitch": 48, "start": {"bar": 4, "beat": 2}, "expect": {"start": {"qn": 13}}}},
        {"part_name": "Violin I", "target": {"pitch": 65, "start": {"bar": 7, "beat": 2}}},
    ]})["selected_count"] == 2
    assert sorted(reaper.lua(SELECTED)) == ["Cello:48@13.0", "Violin I:65@22.5"]
    bad = rpc(client, "select_notes", {"targets": [
        {"target": {"note_id": 1}}, {"target": {"pitch": 48, "start": {"bar": 5, "beat": 4}}}]})["error"]
    assert bad["code"] == "INVALID_POSITION" and bad["details"]["index"] == 1      # 3/4 に第4拍はない
    assert code(client, "select_notes", {"targets": [
        {"target": {"pitch": 48, "start": {"bar": 4, "beat": 2}, "expect": {"start": {"bar": 4, "beat": 3}}}}]}) \
        == "NOTE_MISMATCH"
    call(client, "select_notes", {"targets": [
        {"target": {"note_id": 3}}, {"part_name": "Violin I", "target": {"pitch": 72, "start_qn": 8}}]})
    # expect の不一致・曲の不一致は、選択を変えずに拒否する。
    stale = rpc(client, "select_notes", {"targets": [
        {"target": {"note_id": 1}},
        {"target": {"note_id": 3, "expect": {"pitch": 48, "start_qn": 12.0, "velocity": 99}}}]})["error"]
    assert stale["code"] == "NOTE_MISMATCH" and stale["details"]["index"] == 1
    assert stale["details"]["mismatched_fields"] == ["start_qn", "velocity"]
    assert (stale["details"]["actual"]["pitch"], stale["details"]["actual"]["start_qn"]) == (48, 13.0)
    assert code(client, "select_notes", {"targets": [], "expect_project_name": "Study.rpp"}) == "PROJECT_MISMATCH"
    # 異なる指定方式で同じ音符 → 拒否し、選択は変えない。
    assert code(client, "select_notes", {"targets": [
        {"target": {"note_id": 1}}, {"target": {"pitch": 36, "start_qn": 0}}]}) == "INVALID_REQUEST"
    missing = rpc(client, "select_notes", {"targets": [{"target": {"note_id": 1}}, {"target": {"note_id": 99}}]})
    assert missing["error"]["code"] == "NOTE_NOT_FOUND" and missing["error"]["details"]["index"] == 1
    assert code(client, "select_notes", {"targets": [{"target": {"pitch": 36, "start_qn": 0.001}}]}) == "NOTE_NOT_FOUND"
    assert sorted(reaper.lua(SELECTED)) == ["Cello:48@13.0", "Violin I:72@8.0"]
    assert brief(call(client, "get_notes")["notes"]) == VIOLIN + CELLO  # 付番は不変
    assert call(client, "select_notes", {"targets": [
        {"target": {"note_id": 1, "expect": {"pitch": 36, "duration_qn": 4.0, "muted": False}}}]}) \
        == {"project_name": "Harmony.rpp", "selected_count": 1}
    assert reaper.lua(SELECTED) == ["Cello:36@0.0"]
    assert call(client, "select_notes", {"targets": []}) == {"project_name": "Harmony.rpp", "selected_count": 0}
    assert reaper.lua(SELECTED) == []


PATCH = [
    {"op": "update", "target": {"note_id": 3}, "set": {"pitch": 50, "duration_qn": 2.0}},             # Cello 48@13
    {"op": "delete", "part_name": "Violin I", "target": {"pitch": 64, "start_qn": 0.0}},
    {"op": "update", "part_name": "Violin I", "target": {"note_id": 7}, "set": {"start_qn": 22.0, "velocity": 30}},
    {"op": "insert", "client_note_id": "new-1", "part_name": "Violin I",
     "note": {"pitch": 76, "start_qn": 10.0, "duration_qn": 0.25, "velocity": 90, "channel": 3, "muted": False}},
    {"op": "insert", "client_note_id": "new-2",
     "note": {"pitch": 40, "start_qn": 20.0, "duration_qn": 1.0, "velocity": 80, "channel": 1, "muted": True}},
]
VIOLIN_PATCHED = [("Violin I", 1, 60, 0.0, 1.0), ("Violin I", 2, 62, 1.0, 1.0), ("Violin I", 3, 67, 4.0, 4.0),
                  ("Violin I", 4, 72, 8.0, 6.0), ("Violin I", 5, 76, 10.0, 0.25), ("Violin I", 6, 60, 16.0, 1.0),
                  ("Violin I", 7, 65, 22.0, 0.5)]
CELLO_PATCHED = [("Cello", 1, 36, 0.0, 4.0), ("Cello", 2, 43, 4.0, 4.0), ("Cello", 3, 50, 13.0, 2.0),
                 ("Cello", 4, 36, 16.0, 3.0), ("Cello", 5, 40, 20.0, 1.0)]

EVENT_COUNTS = r"""
  local _, notes, cc = reaper.MIDI_CountEvts(take_of(proj_named("Harmony.rpp"), "Cello", 0))
  return { notes, cc }
"""


def all_notes(client):
    return brief(call(client, "get_notes", {"include_muted": True})["notes"])


def test_apply_patch_dry_run_changes_nothing(client):
    res = call(client, "apply_patch", {"operations": PATCH, "dry_run": True})
    assert set(res) == {"project_name", "added", "updated", "deleted", "dry_run", "resolved", "inserted"}
    assert (res["project_name"], res["added"], res["updated"], res["deleted"], res["dry_run"]) == \
        ("Harmony.rpp", 2, 2, 1, True)
    # dry_run でも、解決した音符（変更前）を確認できる。
    assert [(r["operation_index"], r["op"]) + brief([r["note"]])[0] for r in res["resolved"]] == [
        (0, "update", "Cello", 3, 48, 13.0, 1.0),
        (1, "delete", "Violin I", 2, 64, 0.0, 1.0),
        (2, "update", "Violin I", 7, 65, 22.5, 0.5),
    ]
    assert res["inserted"] == []
    assert all_notes(client) == VIOLIN + CELLO_ALL


def test_apply_patch_and_single_undo(client, reaper):
    call(client, "select_notes", {"targets": [{"target": {"note_id": 4}}]})   # 既存の選択フラグは保持される
    assert reaper.lua(EVENT_COUNTS) == [4, 1]
    rid = str(uuid.uuid4())
    body = rpc(client, "apply_patch", {"operations": PATCH, "label": "CT: 修正"}, rid)
    res = body["result"]
    assert (res["project_name"], res["added"], res["updated"], res["deleted"], res["dry_run"]) == \
        ("Harmony.rpp", 2, 2, 1, False)
    assert [r["note"]["note_id"] for r in res["resolved"]] == [3, 2, 7]
    # inserted は編集後の付番による note_id を返す。
    assert res["inserted"] == [
        {"operation_index": 3, "client_note_id": "new-1", "part_name": "Violin I", "note_id": 5},
        {"operation_index": 4, "client_note_id": "new-2", "part_name": "Cello", "note_id": 5},
    ]
    assert all_notes(client) == VIOLIN_PATCHED + CELLO_PATCHED
    notes = {(n["part_name"], n["note_id"]): n for n in
             call(client, "get_notes", {"include_muted": True})["notes"]}
    assert (notes[("Violin I", 5)]["velocity"], notes[("Violin I", 5)]["channel"]) == (90, 3)
    assert notes[("Violin I", 7)]["velocity"] == 30
    assert notes[("Cello", 5)]["muted"] is True and notes[("Cello", 5)]["selected"] is False
    assert notes[("Cello", 4)]["selected"] is True
    assert reaper.lua(EVENT_COUNTS) == [5, 1]                                  # CC を保持
    # 同じ request_id の再送は二重編集しない。
    assert rpc(client, "apply_patch", {"operations": PATCH, "label": "CT: 修正"}, rid) == body
    assert all_notes(client) == VIOLIN_PATCHED + CELLO_PATCHED
    # 通常の Undo 1 回で元に戻る。
    label = reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      local label = reaper.Undo_CanUndo2(proj)
      reaper.Undo_DoUndo2(proj)
      return label
    """)
    assert label == "CT: 修正"
    assert all_notes(client) == VIOLIN + CELLO_ALL
    assert reaper.lua(EVENT_COUNTS) == [4, 1]


def test_patch_validation_is_all_or_nothing(client):
    def patch_code(ops):
        return code(client, "apply_patch", {"operations": ops})

    good = {"op": "update", "target": {"note_id": 1}, "set": {"pitch": 37}}
    # 異なる方式で同じ音符
    assert patch_code([good, {"op": "delete", "target": {"pitch": 36, "start_qn": 0}}]) == "INVALID_REQUEST"
    missing = rpc(client, "apply_patch", {"operations": [good, {"op": "delete", "target": {"note_id": 9}}]})
    assert missing["error"]["code"] == "NOTE_NOT_FOUND" and missing["error"]["details"]["operation_index"] == 1
    # expect: 古い番号が別の音符を指していたら、何も変更しない。
    stale = rpc(client, "apply_patch", {"operations": [
        good, {"op": "delete", "target": {"note_id": 3, "expect": {"pitch": 36}}}]})["error"]
    assert stale["code"] == "NOTE_MISMATCH" and stale["details"]["operation_index"] == 1
    assert stale["details"]["mismatched_fields"] == ["pitch"] and stale["details"]["actual"]["pitch"] == 48
    # 座標指定でも、長さが変わっていたら拒否できる。
    assert patch_code([{"op": "delete", "target": {"pitch": 48, "start_qn": 13, "expect": {"duration_qn": 2}}}]) \
        == "NOTE_MISMATCH"
    assert code(client, "apply_patch", {"operations": [good], "expect_project_name": "Study.rpp"}) \
        == "PROJECT_MISMATCH"
    assert patch_code([good, {"op": "delete", "part_name": "Viola", "target": {"note_id": 1}}]) == "PART_NOT_FOUND"
    # アイテムに収まらない移動・延長・追加
    assert patch_code([good, {"op": "update", "part_name": "Violin I", "target": {"note_id": 4},
                              "set": {"duration_qn": 4.5}}]) == "INVALID_POSITION"
    assert patch_code([good, {"op": "update", "part_name": "Violin I", "target": {"note_id": 5},
                              "set": {"start_qn": 7.0}}]) == "INVALID_POSITION"
    new = {"pitch": 60, "start_qn": 27.5, "duration_qn": 1.0, "velocity": 80, "channel": 1, "muted": False}
    assert patch_code([good, {"op": "insert", "client_note_id": "x", "note": new}]) == "NO_TARGET_ITEM"
    # 2 つのアイテムにまたがる追加
    assert patch_code([{"op": "insert", "client_note_id": "x", "part_name": "Violin I",
                        "note": {**new, "start_qn": 7.5}}]) == "NO_TARGET_ITEM"
    # ティックで表現できない位置
    assert patch_code([{"op": "update", "target": {"note_id": 1}, "set": {"start_qn": 0.00001}}]) == "INVALID_POSITION"
    assert all_notes(client) == VIOLIN + CELLO_ALL


def test_duplicate_notes_and_coordinates(client):
    # 追加・移動・座標指定の開始位置を小節・拍で書く。
    res = call(client, "apply_patch", {"operations": [
        {"op": "insert", "client_note_id": "bb", "note": {
            "pitch": 55, "start": {"bar": 6, "beat": 2, "offset_qn": 0.5}, "duration_qn": 0.5,
            "velocity": 80, "channel": 1, "muted": False}},
        {"op": "update", "target": {"pitch": 48, "start": {"bar": 4, "beat": 2}},
         "set": {"start": {"bar": 4, "beat": 3}}},
    ]})
    assert res["inserted"][0]["note_id"] == 5
    moved = {(n["pitch"], n["start_qn"], n["duration_qn"]) for n in
             call(client, "get_notes", {"selected_part": True, "include_muted": True})["notes"]}
    assert (55, 20.5, 0.5) in moved and (48, 14.0, 1.0) in moved        # 第6小節(3/4)第2拍+0.5 = QN 20.5
    bad = rpc(client, "apply_patch", {"operations": [
        {"op": "delete", "target": {"pitch": 55, "start": {"bar": 6, "beat": 2, "offset_qn": 0.5}}},
        {"op": "update", "target": {"note_id": 1}, "set": {"start": {"bar": 7, "beat": 7}}}]})["error"]
    assert bad["code"] == "INVALID_POSITION" and bad["details"]["operation_index"] == 1   # 6/8 に第7拍はない
    call(client, "apply_patch", {"operations": [
        {"op": "delete", "target": {"pitch": 55, "start": {"bar": 6, "beat": 2, "offset_qn": 0.5}}},
        {"op": "update", "target": {"pitch": 48, "start_qn": 14}, "set": {"start_qn": 13.0}},
    ]})
    assert brief(call(client, "get_notes", {"selected_part": True, "include_muted": True})["notes"]) == CELLO_ALL

    # 同チャンネル・同音高で重なる音符は MIDI として表現できない（REAPER が対応付けを変える）ため、
    # 別チャンネルで「同時刻・同音高の別音符」を作る。
    dup = {"pitch": 36, "start_qn": 0.0, "duration_qn": 2.0, "velocity": 70, "channel": 2, "muted": False}
    call(client, "apply_patch", {"operations": [{"op": "insert", "client_note_id": "dup", "note": dup}]})
    cello = {"selected_part": True, "include_muted": True}
    notes = brief(call(client, "get_notes", cello)["notes"])
    assert [n[1:] for n in notes[:2]] == [(1, 36, 0.0, 2.0), (2, 36, 0.0, 4.0)] or \
        [n[1:] for n in notes[:2]] == [(1, 36, 0.0, 4.0), (2, 36, 0.0, 2.0)]
    assert len(notes) == 5                                                    # 同時刻・同音高でも統合しない
    body = rpc(client, "apply_patch", {"operations": [{"op": "delete", "target": {"pitch": 36, "start_qn": 0}}]})
    assert body["error"]["code"] == "AMBIGUOUS_NOTE" and body["error"]["details"]["match_count"] == 2
    # channel を指定すれば、別チャンネルにある同時刻・同音高の音符を区別できる。
    # 削除と同時に、削除より後ろの番号を参照しても参照先がずれない。
    res = call(client, "apply_patch", {"operations": [
        {"op": "delete", "target": {"pitch": 36, "start_qn": 0, "channel": 2}},
        {"op": "update", "target": {"note_id": 5}, "set": {"velocity": 81}},
    ]})
    assert (res["deleted"], res["updated"]) == (1, 1)
    assert (res["resolved"][0]["note"]["channel"], res["resolved"][0]["note"]["duration_qn"]) == (2, 2.0)
    after = call(client, "get_notes", cello)["notes"]
    assert brief(after) == CELLO_ALL and after[3]["velocity"] == 81


def test_part_not_selected_and_explicit_override(client, reaper):
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.GetSetMediaTrackInfo_String(track_named(proj, "Cello"), "P_NAME", "Violoncello", true)
    """)
    # 改名しても同じトラックの選択を維持し、現在名を返す。
    assert call(client, "get_score_info")["selected_part_name"] == "Violoncello"
    assert code(client, "select_part", {"part_name": "Cello"}) == "PART_NOT_FOUND"
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.GetSetMediaTrackInfo_String(track_named(proj, "Violoncello"), "P_NAME", "Violin I", true)
    """)
    # 重複名になった選択中パートは解除される。
    info = call(client, "get_score_info")
    assert info["selected_part_name"] is None
    assert [p["addressable"] for p in info["parts"]] == [False, False]
    assert {c["code"] for c in info["constraints"]} == {"DUPLICATE_PART_NAME"}
    assert code(client, "select_part", {"part_name": "Violin I"}) == "AMBIGUOUS_PART"
    assert code(client, "get_notes") == "AMBIGUOUS_PART"                      # 全パート取得は不可
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.GetSetMediaTrackInfo_String(reaper.GetTrack(proj, 1), "P_NAME", "", true)
    """)
    # 名前のないトラックはパートではない。除外して警告する。
    unnamed = call(client, "get_notes")
    assert brief(unnamed["notes"]) == VIOLIN and unnamed["parts"] == [{"part_name": "Violin I", "order": 1}]
    assert [(w["code"], w["details"]["count"]) for w in unnamed["warnings"]] == [("UNNAMED_TRACKS_IGNORED", 1)]
    info = call(client, "get_score_info")
    assert [p["part_name"] for p in info["parts"]] == ["Violin I"] and info["constraints"] == []
    assert [w["code"] for w in info["warnings"]] == ["UNNAMED_TRACKS_IGNORED"]
    assert code(client, "select_part", {"part_name": ""}) == "INVALID_REQUEST"
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.GetSetMediaTrackInfo_String(reaper.GetTrack(proj, 1), "P_NAME", "Cello", true)
    """)
    op = {"op": "update", "target": {"note_id": 1}, "set": {"velocity": 64}}
    assert code(client, "apply_patch", {"operations": [op]}) == "PART_NOT_SELECTED"
    assert code(client, "select_notes", {"targets": [{"target": {"note_id": 1}}]}) == "PART_NOT_SELECTED"
    res = call(client, "apply_patch", {"operations": [{**op, "part_name": "Cello"}]})
    assert res["updated"] == 1
    assert call(client, "get_score_info")["selected_part_name"] is None        # 明示指定は選択を変えない


def test_deleting_selected_track_clears_part(client, reaper):
    reaper.lua(r"""add_track(proj_named("Harmony.rpp"), "Scratch")""")
    assert call(client, "select_part", {"part_name": "Scratch"})["selected_part_name"] == "Scratch"
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.DeleteTrack(track_named(proj, "Scratch"))
    """)
    info = call(client, "get_score_info")
    assert info["selected_part_name"] is None and len(info["parts"]) == 2
    assert info["project_name"] == "Harmony.rpp"                              # 曲の選択は維持


def test_injected_write_failure_restores_everything(client, reaper):
    before = call(client, "get_notes", {"include_muted": True})["notes"]
    ops = [{"op": "delete", "part_name": "Cello", "target": {"note_id": 1}},
           {"op": "update", "part_name": "Violin I", "target": {"note_id": 5}, "set": {"pitch": 73}}]
    reaper.lua(r"""reaper.SetExtState("SIO", "test_inject", "write_fail", false)""")
    try:
        assert code(client, "apply_patch", {"operations": ops}) == "WRITE_FAILED"
    finally:
        reaper.lua(r"""reaper.DeleteExtState("SIO", "test_inject", false)""")
    assert call(client, "get_notes", {"include_muted": True})["notes"] == before
    assert reaper.lua(EVENT_COUNTS) == [4, 1]


# ------------------------------------------------------------------ 4. 対応外の構造


def test_unsupported_structures(client, reaper):
    reaper.lua(r"""
      local proj = proj_named("Study.rpp")
      local _, t = add_item(track_named(proj, "Solo"), 0, 4); add_note(t, 60, 0, 1); reaper.MIDI_Sort(t)
      -- 複数テイク
      local tr = add_track(proj, "Takes")
      local item, take = add_item(tr, 0, 4); add_note(take, 60, 0, 1); reaper.MIDI_Sort(take)
      reaper.AddTakeToMediaItem(item)
      -- 重なったアイテム
      tr = add_track(proj, "Overlap")
      add_item(tr, 0, 4); add_item(tr, 2, 6)
      -- ソースをループするアイテム
      tr = add_track(proj, "Loop")
      item, take = add_item(tr, 0, 4); add_note(take, 60, 0, 1); reaper.MIDI_Sort(take)
      reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 1)
      reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.TimeMap2_QNToTime(proj, 8))
      -- 再生速度
      tr = add_track(proj, "Rate")
      item, take = add_item(tr, 0, 4)
      reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", 2)
      -- アイテム境界の外の音符
      tr = add_track(proj, "Outside")
      item, take = add_item(tr, 0, 4); add_note(take, 60, 3, 1); reaper.MIDI_Sort(take)
      reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)
      reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.TimeMap2_QNToTime(proj, 3.5))
      -- 端を縮めて、音符が完全に境界の外に残ったアイテム（除外して警告）
      tr = add_track(proj, "Trimmed")
      item, take = add_item(tr, 0, 4); add_note(take, 60, 0, 1); add_note(take, 62, 3, 1); reaper.MIDI_Sort(take)
      reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)
      reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.TimeMap2_QNToTime(proj, 3))
      -- ソース開始オフセット（読取可・編集不可）
      tr = add_track(proj, "Offset")
      item, take = add_item(tr, 0, 8); add_note(take, 60, 4, 1); reaper.MIDI_Sort(take)
      reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)
      reaper.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", reaper.TimeMap2_QNToTime(proj, 2))
      reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.TimeMap2_QNToTime(proj, 6))
      -- 固定レーン
      tr = add_track(proj, "Lanes")
      add_item(tr, 0, 4)
      reaper.SetMediaTrackInfo_Value(tr, "I_FREEMODE", 2)
      reaper.UpdateArrange()
    """)
    call(client, "select_project", {"project_name": "Study.rpp"})

    def problem(part):
        body = rpc(client, "get_notes", {"part_names": [part]})
        assert body["ok"] is False, (part, body)
        return body["error"]["code"], sorted({d["code"] for d in body["error"]["details"]["diagnostics"]})

    assert problem("Takes") == ("UNSUPPORTED_TAKES", ["MULTIPLE_TAKES"])
    assert problem("Overlap") == ("UNSUPPORTED_ITEM", ["OVERLAPPING_ITEMS"])
    assert problem("Loop") == ("UNSUPPORTED_ITEM", ["LOOPED_MIDI_ITEM"])
    assert problem("Rate")[0] == "UNSUPPORTED_ITEM" and "UNSUPPORTED_PLAYRATE" in problem("Rate")[1]
    assert problem("Outside") == ("UNSUPPORTED_ITEM", ["NOTE_OUTSIDE_ITEM"])
    assert problem("Lanes") == ("UNSUPPORTED_ITEM", ["UNSUPPORTED_LANES"])
    assert code(client, "get_notes") in ("UNSUPPORTED_ITEM", "UNSUPPORTED_TAKES")

    # 完全に境界の外にある音符は付番せず、編集しても内容を保つ。
    trimmed = call(client, "get_notes", {"part_names": ["Trimmed"]})
    assert brief(trimmed["notes"]) == [("Trimmed", 1, 60, 0.0, 1.0)]
    assert [(w["code"], w["details"]["count"]) for w in trimmed["warnings"]] == [("NOTES_OUTSIDE_ITEM_IGNORED", 1)]
    assert code(client, "select_notes", {"targets": [{"part_name": "Trimmed", "target": {"note_id": 2}}]}) \
        == "NOTE_NOT_FOUND"
    assert call(client, "apply_patch", {"operations": [
        {"op": "update", "part_name": "Trimmed", "target": {"note_id": 1}, "set": {"pitch": 61}}]})["updated"] == 1
    assert reaper.lua(r"""
      local take = take_of(proj_named("Study.rpp"), "Trimmed", 0)
      local out = json.array({})
      local _, n = reaper.MIDI_CountEvts(take)
      for k = 0, n - 1 do
        local _, _, _, _, _, _, pitch = reaper.MIDI_GetNote(take, k)
        out[#out + 1] = math.floor(pitch)
      end
      return out
    """) == [61, 62]
    # 既定設定で作った（ループ設定が有効なだけで繰り返していない）アイテムは読める。
    assert brief(call(client, "get_notes", {"part_names": ["Solo"]})["notes"]) == [("Solo", 1, 60, 0.0, 1.0)]

    offset = call(client, "get_notes", {"part_names": ["Offset"]})
    assert [(n["pitch"], n["start_qn"]) for n in offset["notes"]] == [(60, 2.0)]
    assert [w["code"] for w in offset["warnings"]] == ["SOURCE_OFFSET_READ_ONLY"]
    edit = rpc(client, "apply_patch", {"operations": [
        {"op": "update", "part_name": "Offset", "target": {"note_id": 1}, "set": {"pitch": 61}}]})
    assert edit["error"]["code"] == "UNSUPPORTED_ITEM"
    assert code(client, "apply_patch", {"operations": [
        {"op": "delete", "part_name": "Takes", "target": {"note_id": 1}}]}) == "UNSUPPORTED_TAKES"

    info = call(client, "get_score_info")
    assert info["note_range"] is None and info["note_range_complete"] is False
    assert {c["code"] for c in info["constraints"]} >= {
        "MULTIPLE_TAKES", "OVERLAPPING_ITEMS", "LOOPED_MIDI_ITEM", "UNSUPPORTED_PLAYRATE", "NOTE_OUTSIDE_ITEM",
        "UNSUPPORTED_LANES", "SOURCE_OFFSET_READ_ONLY"}
    assert "NOTES_OUTSIDE_ITEM_IGNORED" in {w["code"] for w in info["warnings"]}


# ------------------------------------------------------------------ 5. 曲の重複名・クローズ


def test_duplicate_project_names_and_closing(client, reaper):
    d = reaper.work
    reaper.lua(r"""
      reaper.Main_OnCommand(40859, 0)
      reaper.Main_openProject("noprompt:" .. [[%s]])
    """ % (d / "b" / "Study.rpp"))
    projects = call(client, "list_projects")["projects"]
    assert [(p["project_name"], p["addressable"], p["active"]) for p in projects] == [
        ("Harmony.rpp", True, False), ("Study.rpp", False, False), ("Study.rpp", False, True)]
    assert code(client, "select_project", {"project_name": "Study.rpp"}) == "AMBIGUOUS_PROJECT"
    # 失敗してもタブは変わらない。対象は表のタブ（新しく開いた、空の Study.rpp）。
    assert call(client, "get_score_info")["parts"] == []
    # 同名の曲は名前で選べないが、人間がタブを表に出せば対象になる。
    reaper.lua(r"""reaper.SelectProjectInstance(reaper.EnumProjects(1))""")
    assert len(call(client, "get_score_info")["parts"]) == 9
    reaper.lua(r"""
      reaper.SelectProjectInstance(reaper.EnumProjects(2))
      reaper.Main_OnCommand(40860, 0)                       -- 追加した同名タブ（未変更）を閉じる
      reaper.Main_OnCommand(40859, 0)
      reaper.Main_openProject("noprompt:" .. [[%s]])
    """ % (d / "a" / "Temp.rpp"))
    assert call(client, "get_score_info")["project_name"] == "Temp.rpp"     # 開いた曲が表のタブ
    assert call(client, "select_project", {"project_name": "Temp.rpp"})["project_name"] == "Temp.rpp"
    reaper.lua(r"""reaper.Main_OnCommand(40860, 0)""")       # 対象の曲を閉じる
    # 閉じた後は、REAPER が表に出した曲が対象になる。
    name = call(client, "get_score_info")["project_name"]
    assert name in ("Harmony.rpp", "Study.rpp") and name == reaper.lua(ACTIVE_NAME)
    assert sorted(p["active"] for p in call(client, "list_projects")["projects"]) == [False, True]
    time.sleep(1.2)
    assert call(client, "get_status")["active_project_name"] == name


# ------------------------------------------------------------------ 6. 再生


PLAY_STATE = r"""
  local proj = proj_named("Harmony.rpp")
  return { reaper.GetPlayStateEx(proj), reaper.GetCursorPositionEx(proj), reaper.GetSetRepeatEx(proj, -1) }
"""


def test_play_range(client, reaper):
    call(client, "select_project", {"project_name": "Harmony.rpp"})
    bar1 = {"range": {"start": {"bar": 1, "beat": 3}, "end": {"bar": 2, "beat": 1}}}     # QN 2〜4 = 1 秒（120 BPM）
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      reaper.SetEditCurPos2(proj, 3.25, false, false)
      reaper.GetSetRepeatEx(proj, 1)
    """)
    assert call(client, "stop") == {"stopped": False}
    started = {"project_name": "Harmony.rpp", "started": True}
    assert code(client, "play_range", {**bar1, "expect_project_name": "Study.rpp"}) == "PROJECT_MISMATCH"
    assert call(client, "play_range", bar1) == started
    time.sleep(0.3)
    state, cursor, repeat = reaper.lua(PLAY_STATE)
    assert state == 1 and cursor == pytest.approx(1.0) and repeat == 0
    assert code(client, "play_range", bar1) == "TRANSPORT_BUSY"
    time.sleep(1.5)
    state, cursor, repeat = reaper.lua(PLAY_STATE)                       # 終端で停止し、状態を復元
    assert state == 0 and cursor == pytest.approx(3.25) and repeat == 1

    # stop は SIO 管理の再生だけを止める。
    long = {"range": {"start": {"qn": 0}, "end": {"qn": 16}}}
    assert call(client, "play_range", long) == started
    time.sleep(1.4)
    assert call(client, "get_status")["playback_active"] is True
    assert call(client, "stop") == {"stopped": True}
    state, cursor, repeat = reaper.lua(PLAY_STATE)
    assert state == 0 and cursor == pytest.approx(3.25) and repeat == 1
    assert call(client, "stop") == {"stopped": False}
    time.sleep(1.4)
    assert call(client, "get_status")["playback_active"] is False
    # select_project で別の曲に切り替えるときは、管理中の再生を先に止めて状態を復元する。
    assert call(client, "play_range", long) == started
    time.sleep(0.3)
    call(client, "select_project", {"project_name": "Study.rpp"})
    state, cursor, repeat = reaper.lua(PLAY_STATE)
    assert state == 0 and cursor == pytest.approx(3.25) and repeat == 1
    assert call(client, "stop") == {"stopped": False}
    call(client, "select_project", {"project_name": "Harmony.rpp"})

    # 人間が止めた場合は管理を解除し、カーソルを古い値で上書きしない。
    assert call(client, "play_range", bar1) == started
    time.sleep(0.3)
    reaper.lua(r"""reaper.OnStopButtonEx(proj_named("Harmony.rpp"))""")
    time.sleep(0.8)
    assert call(client, "stop") == {"stopped": False}
    state, cursor, repeat = reaper.lua(PLAY_STATE)
    assert state == 0 and cursor == pytest.approx(1.0) and repeat == 1

    # 人間が始めた再生は stop で止めない。
    reaper.lua(r"""reaper.OnPlayButtonEx(proj_named("Harmony.rpp"))""")
    time.sleep(0.3)
    assert code(client, "play_range", bar1) == "TRANSPORT_BUSY"
    assert call(client, "stop") == {"stopped": False}
    assert reaper.lua(PLAY_STATE)[0] == 1
    reaper.lua(r"""reaper.OnStopButtonEx(proj_named("Harmony.rpp"))""")


# ------------------------------------------------------------------ 7. 規模


def test_performance_2000_notes(client, reaper):
    reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      for p = 1, 8 do
        local tr = add_track(proj, "Perf " .. p)
        for half = 0, 1 do                                  -- 32 小節相当を 2 アイテムに分ける
          local _, take = add_item(tr, 28 + half * 64, 28 + (half + 1) * 64)
          for i = 0, 124 do
            add_note(take, 48 + (i * 7 + p) % 36, 28 + half * 64 + i * 0.5, 0.5)
          end
          reaper.MIDI_Sort(take)
        end
      end
    """, timeout=120)
    t0 = time.perf_counter()
    res = call(client, "get_notes")
    elapsed = time.perf_counter() - t0
    perf = [n for n in res["notes"] if n["part_name"].startswith("Perf")]
    assert len(perf) == 2000
    ids = [n["note_id"] for n in perf if n["part_name"] == "Perf 3"]
    assert ids == list(range(1, 251))                       # 複数アイテムを通した全体付番
    print(f"\n2000 音符の取得: {elapsed * 1000:.0f} ms")
    assert elapsed < 1.0


# ------------------------------------------------------------------ 7b. 曲とトラックの作成


TRACK_FX = r"""
  local proj = reaper.EnumProjects(-1)
  local out = json.array({})
  for i = 0, reaper.CountTracks(proj) - 1 do
    local tr = reaper.GetTrack(proj, i)
    local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
    out[#out + 1] = { name, reaper.TrackFX_GetCount(tr), reaper.TrackFX_GetInstrument(tr) }
  end
  return out
"""


def test_add_track_to_existing_project(client, reaper):
    info = call(client, "get_score_info")
    count = len(info["parts"])
    assert code(client, "add_track", {"part_name": "Cello"}) == "PART_ALREADY_EXISTS"
    assert code(client, "add_track", {"part_name": "X", "expect_project_name": "Study.rpp"}) == "PROJECT_MISMATCH"
    assert len(call(client, "get_score_info")["parts"]) == count
    # 名前だけを指定すると空のトラックになる。
    res = call(client, "add_track", {"part_name": "Added"})
    assert res == {"project_name": "Harmony.rpp", "part_name": "Added", "order": res["order"],
                   "selected_part_name": "Added",
                   "instrument_loaded": False, "instrument_name": None, "warnings": []}
    # 追加したトラックは select_part したのと同じ状態になる（既定パート、REAPER 上の選択）。
    info = call(client, "get_score_info")
    parts = info["parts"]
    assert len(parts) == count + 1 and info["selected_part_name"] == "Added"
    assert parts[-1] == {"part_name": "Added", "order": res["order"], "addressable": True, "selected": True}
    assert reaper.lua(SELECTED_TRACKS) == ["Added"]
    assert call(client, "get_notes", {"part_names": ["Added"]})["notes"] == []


def new_note(cid, pitch, start_qn, duration_qn=1.0):
    return {"op": "insert", "client_note_id": cid, "part_name": "Added", "note": {
        "pitch": pitch, "start_qn": start_qn, "duration_qn": duration_qn,
        "velocity": 80, "channel": 1, "muted": False}}


def test_add_item_across_tempo_and_meter_changes(client, reaper):
    def bars(a, b):
        return {"start": {"bar": a, "beat": 1}, "end": {"bar": b, "beat": 1}}

    # アイテムのないトラックには音符を追加できない。
    assert code(client, "apply_patch", {"operations": [new_note("a", 60, 0.0)]}) == "NO_TARGET_ITEM"
    assert code(client, "add_item", {"part_name": "Nope", "range": bars(1, 5)}) == "PART_NOT_FOUND"
    assert code(client, "add_item", {"part_name": "Added", "range": bars(1, 5),
                                     "expect_project_name": "Study.rpp"}) == "PROJECT_MISMATCH"
    # 第1〜4小節（4/4、途中でテンポ変更）。
    assert call(client, "add_item", {"part_name": "Added", "range": bars(1, 5)}) == {
        "project_name": "Harmony.rpp", "part_name": "Added", "range": {"start_qn": 0.0, "end_qn": 16.0}}
    overlap = rpc(client, "add_item", {"part_name": "Added", "range": {"start": {"qn": 15.0}, "end": {"qn": 20.0}}})
    assert overlap["error"]["code"] == "RANGE_OCCUPIED"
    assert overlap["error"]["details"]["item_range"] == {"start_qn": 0.0, "end_qn": 16.0}
    # 端が接する範囲には作れる: 第5〜6小節（3/4）。
    assert call(client, "add_item", {"part_name": "Added", "range": bars(5, 7)})["range"] == {
        "start_qn": 16.0, "end_qn": 22.0}

    res = call(client, "apply_patch", {"operations": [
        new_note("a", 60, 0.0), new_note("b", 62, 15.0), new_note("c", 64, 16.0), new_note("d", 65, 21.0)]})
    assert res["inserted"] and len(res["inserted"]) == 4
    assert brief(call(client, "get_notes", {"part_names": ["Added"]})["notes"]) == [
        ("Added", 1, 60, 0.0, 1.0), ("Added", 2, 62, 15.0, 1.0), ("Added", 3, 64, 16.0, 1.0),
        ("Added", 4, 65, 21.0, 1.0)]
    # 2 つのアイテムにまたがる音符、アイテムの外の音符は追加できない。
    assert code(client, "apply_patch", {"operations": [new_note("e", 67, 15.5)]}) == "NO_TARGET_ITEM"
    assert code(client, "apply_patch", {"operations": [new_note("f", 67, 22.0)]}) == "NO_TARGET_ITEM"
    info = call(client, "get_score_info")
    assert [d for d in info["constraints"] + info["warnings"] if d["details"].get("part_name") == "Added"] == []
    # 作ったアイテムはループしない設定で、ソースがアイテム全体を覆う。
    assert reaper.lua(r"""
      local proj = proj_named("Harmony.rpp")
      local tr = track_named(proj, "Added")
      local out = json.array({})
      for i = 0, reaper.CountTrackMediaItems(tr) - 1 do
        local item = reaper.GetTrackMediaItem(tr, i)
        out[#out + 1] = { reaper.CountTakes(item), reaper.GetMediaItemInfo_Value(item, "B_LOOPSRC") }
      end
      return out
    """) == [[1, 0], [1, 0]]


def test_create_project_and_add_tracks(client, reaper):
    assert call(client, "create_project") == {"project_name": "", "selected_part_name": None}
    projects = call(client, "list_projects")["projects"]
    assert sorted((p["project_name"], p["active"]) for p in projects) == [
        ("", True), ("Harmony.rpp", False), ("Study.rpp", False)]
    assert call(client, "get_score_info")["parts"] == []

    res = call(client, "add_track", {"part_name": "Piano", "instrument": "ReaSynth"})
    assert (res["project_name"], res["part_name"], res["order"]) == ("", "Piano", 1)
    assert res["instrument_loaded"] is True and "ReaSynth" in res["instrument_name"] and res["warnings"] == []

    # 見つからない名前、音源ではないプラグイン: 空のトラックを追加して警告する。
    for order, (name, plugin) in enumerate([("Missing", "No Such Instrument 12345"), ("Effect", "ReaEQ")], start=2):
        res = call(client, "add_track", {"part_name": name, "instrument": plugin})
        assert (res["order"], res["instrument_loaded"], res["instrument_name"]) == (order, False, None)
        assert [(w["code"], w["details"]) for w in res["warnings"]] == [
            ("INSTRUMENT_NOT_FOUND", {"instrument": plugin})]
        assert res["selected_part_name"] == name                 # 音源がなくても既定パートになる
    assert call(client, "get_score_info")["selected_part_name"] == "Effect"
    assert reaper.lua(SELECTED_TRACKS) == ["Effect"]
    assert reaper.lua(TRACK_FX) == [["Piano", 1, 0], ["Missing", 0, -1], ["Effect", 0, -1]]
    assert [p["part_name"] for p in call(client, "get_score_info")["parts"]] == ["Piano", "Missing", "Effect"]
    assert call(client, "select_part", {"part_name": "Piano"})["selected_part_name"] == "Piano"

    # 追加 1 回は Undo 1 回で取り消せる。
    reaper.lua(r"""reaper.Undo_DoUndo2(reaper.EnumProjects(-1))""")
    assert [row[0] for row in reaper.lua(TRACK_FX)] == ["Piano", "Missing"]

    # 新しい曲でも、アイテムを作れば音符を書き込める。パート名を省略すると選択中のパートを使う。
    rng = {"start": {"bar": 1, "beat": 1}, "end": {"bar": 3, "beat": 1}}
    call(client, "select_part", {"part_name": "Missing"})
    assert call(client, "add_item", {"range": rng}) == {
        "project_name": "", "part_name": "Missing", "range": {"start_qn": 0.0, "end_qn": 8.0}}
    assert code(client, "add_item", {"range": rng}) == "RANGE_OCCUPIED"
    call(client, "add_item", {"part_name": "Piano", "range": rng})
    op = {"op": "insert", "client_note_id": "n", "part_name": "Piano", "note": {
        "pitch": 60, "start": {"bar": 2, "beat": 1}, "duration_qn": 4.0, "velocity": 80, "channel": 1,
        "muted": False}}
    call(client, "apply_patch", {"operations": [op]})
    assert brief(call(client, "get_notes")["notes"]) == [("Piano", 1, 60, 4.0, 4.0)]
    reaper.lua(r"""reaper.Undo_DoUndo2(reaper.EnumProjects(-1))""")          # 音符の追加を取り消す
    reaper.lua(r"""reaper.Undo_DoUndo2(reaper.EnumProjects(-1))""")          # Piano のアイテムを取り消す
    assert code(client, "apply_patch", {"operations": [op]}) == "NO_TARGET_ITEM"
    call(client, "select_project", {"project_name": "Harmony.rpp"})


# ------------------------------------------------------------------ 7c. sio_client


def test_client_library(client, reaper):
    """sio_client を、実際の Service と REAPER に対して使う。"""
    import sio_client
    from sio_client import SIO

    def transport(body, headers, timeout):
        r = client.post("/v2/rpc", content=body, headers=headers)
        return r.status_code, r.content

    sio = SIO(url="http://testserver", token=TOKEN, transport=transport)
    st = sio.status()
    assert st.connected and st.project == "Harmony.rpp"
    assert sio.lock_project() == "Harmony.rpp"

    track = sio.add_track("Lib", "No Such Instrument 12345")
    assert track.name == "Lib" and not track.instrument_loaded
    assert [w.code for w in track.warnings] == ["INSTRUMENT_NOT_FOUND"]
    assert sio.add_item(bars=(1, 2)) == (0.0, 8.0)              # パート省略: 追加したトラックが既定パート
    with pytest.raises(sio_client.SIOError) as e:
        sio.add_item("Lib", bars=2)
    assert e.value.code == "RANGE_OCCUPIED"

    with sio.patch(label="lib") as p:
        p.insert(None, "C4", (1, 1))
        p.insert("Lib", 64, 4.0, duration=2, velocity=100)
    assert (p.result.added, [i.part for i in p.result.inserted]) == (2, ["Lib", "Lib"])
    notes = sio.notes("Lib")
    assert [(n.id, n.pitch, n.start, n.duration, n.bar, n.beat, n.velocity) for n in notes] == [
        (1, 60, 0.0, 1.0, 1, 1, 80), (2, 64, 4.0, 2.0, 2, 1, 100)]
    assert notes.project == "Harmony.rpp" and [x.name for x in notes.parts] == ["Lib"]
    assert [n.pitch for n in sio.notes("Lib", at=(2, 2))] == [64]
    assert [n.pitch for n in sio.notes("Lib", bars=1)] == [60]

    # 読み取った音符をそのまま渡す。読み取り後に変わっていたら、別の音を書き換えずにエラーになる。
    assert sio.update(notes[0], pitch="D4").updated == 1
    with pytest.raises(sio_client.NoteMismatch):
        sio.update(notes[0], velocity=90)
    assert sio.delete((64, (2, 1)), part="Lib").deleted == 1
    notes = sio.notes("Lib")
    assert [(n.pitch, n.pitch_name) for n in notes] == [(62, "D4")]
    assert sio.select_notes(notes) == 1 and sio.notes("Lib")[0].selected is True
    assert sio.select_notes() == 0

    info = sio.score_info()
    assert info.selected_part == "Lib" and info.parts[-1].name == "Lib" and info.meters[0].numerator == 4

    started = time.perf_counter()
    sio.play(range=(0, 2), wait=True, timeout=10)              # 120 BPM で 1 秒
    assert 0.8 < time.perf_counter() - started < 5 and not sio.status().playback_active
    assert sio.stop() is False

    # 人間がタブを切り替えたら、固定した曲と違うのでエラーになり、何も実行されない。
    reaper.lua(r"""reaper.SelectProjectInstance(proj_named("Study.rpp"))""")
    with pytest.raises(sio_client.ProjectMismatch):
        sio.insert("Lib", 60, 0.0)
    assert [(x.name, x.active) for x in sio.projects() if x.active] == [("Study.rpp", True)]
    assert sio.select_project("Harmony.rpp").part == "Lib"
    assert len(sio.notes("Lib")) == 1


# ------------------------------------------------------------------ 8. 失敗時の書込禁止と再起動


def test_rollback_failure_blocks_writes_until_bridge_restart(client, reaper):
    call(client, "select_part", {"part_name": "Cello"})
    op = {"operations": [{"op": "update", "target": {"note_id": 1}, "set": {"velocity": 65}}]}
    reaper.lua(r"""reaper.SetExtState("SIO", "test_inject", "rollback_fail", false)""")
    try:
        assert code(client, "apply_patch", op) == "ROLLBACK_FAILED"
    finally:
        reaper.lua(r"""reaper.DeleteExtState("SIO", "test_inject", false)""")
    blocked = rpc(client, "apply_patch", op)
    assert blocked["error"]["code"] == "ROLLBACK_FAILED" and blocked["error"]["details"]["blocked"] is True
    assert len(call(client, "get_notes", {"selected_part": True})["notes"]) == 3      # 読取は可能
    assert "WRITE_BLOCKED" in {c["code"] for c in call(client, "get_score_info")["constraints"]}
    assert code(client, "apply_patch", {**op, "dry_run": True}) == "ROLLBACK_FAILED"

    old = call(client, "get_status")["bridge_instance_id"]
    toggle = r"""
      local path = reaper.GetResourcePath() .. "/Scripts/SIO/sio_bridge.lua"
      reaper.Main_OnCommand(reaper.AddRemoveReaScript(true, 0, path, true), 0)
    """
    reaper.lua(toggle)                                       # Action の再実行で Bridge を停止
    time.sleep(0.5)
    st = call(client, "get_status")
    assert st["bridge_state"] == "unavailable" and st["bridge_instance_id"] == old
    assert code(client, "list_projects") == "BRIDGE_UNAVAILABLE"
    reaper.lua(toggle)                                       # もう一度実行して起動
    time.sleep(1.5)
    st = call(client, "get_status")
    assert st["bridge_state"] == "connected" and st["bridge_instance_id"] != old
    assert (st["active_project_name"], st["selected_part_name"]) == ("Harmony.rpp", None)   # 再起動で既定パート解除
    assert call(client, "get_notes")["project_name"] == "Harmony.rpp"
    call(client, "select_project", {"project_name": "Harmony.rpp"})
    call(client, "select_part", {"part_name": "Cello"})
    assert call(client, "apply_patch", op)["updated"] == 1   # 書込禁止も解除
    assert call(client, "get_score_info")["constraints"] == []


def test_service_restart_clears_selection(client, config):
    assert call(client, "get_score_info")["selected_part_name"] == "Cello"
    # 最後の試験: 別世代の Service を起動する（以後、元の client は旧世代になり使えない）。
    with TestClient(create_app(config)) as fresh:
        time.sleep(1.5)
        st = call(fresh, "get_status")
        assert st["bridge_state"] == "connected"
        assert (st["active_project_name"], st["selected_part_name"]) == ("Harmony.rpp", None)
        assert call(fresh, "get_score_info")["selected_part_name"] is None
        assert call(fresh, "select_project", {"project_name": "Harmony.rpp"})["selected_part_name"] is None
