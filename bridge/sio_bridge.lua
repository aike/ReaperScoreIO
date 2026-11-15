-- sio_bridge.lua : Score IO (SIO) Bridge — REAPER 常駐 Lua ReaScript
-- プロトコル 2.3。SIO Service とファイル IPC（<REAPERリソース>/SIO/ipc/）で通信する。
-- このスクリプトを Action として 1 回起動すると defer で常駐する。もう一度実行すると停止する。

local BRIDGE_VERSION = "0.6.0"
local PROTOCOL_VERSION = "2.3"
local EPS = 1e-9                -- epsilon_qn
local MAX_NOTES = 100000        -- 1 取得あたりの音符上限
local POLL_INTERVAL = 0.1       -- 要求確認周期（秒）
local HEARTBEAT_INTERVAL = 1.0  -- heartbeat 周期（秒）
local EXT_SECTION = "SIO"

local function script_dir()
    local path = select(2, reaper.get_action_context())
    if not path or path == "" then
        path = debug.getinfo(1, "S").source:gsub("^@", "")
    end
    return path:match("^(.*)[/\\][^/\\]*$") or "."
end

local json = dofile(script_dir() .. "/sio_json.lua")
local A = json.array
local NULL = json.null

-- ------------------------------------------------------------------ 状態

local function new_uuid()
    return (reaper.genGuid(""):gsub("[{}]", ""):lower())
end

local S = {
    instance_id = new_uuid(),
    service_id = nil,          -- 最後に確認した Service 起動世代
    sel_proj = nil,            -- 対象の曲＝アクティブなタブの曲（要求ごとに求め直す）
    sel_track = nil,           -- 対象の曲の既定パート（MediaTrack 参照）
    part_memory = {},          -- 曲ごとの既定パート { proj, track } の一覧
    blocked = {},              -- ROLLBACK_FAILED で書込禁止にした曲の参照
    executed = {},             -- 実行済み request_id -> os.time()
    playback = nil,            -- SIO 管理中の範囲再生
    running_request_id = nil,
    tmp_counter = 0,
    stopped = false,
}

local IPC_DIR = reaper.GetResourcePath() .. "/SIO/ipc"
local REQ_DIR = IPC_DIR .. "/requests"
local CLAIM_DIR = IPC_DIR .. "/claimed"
local RESP_DIR = IPC_DIR .. "/responses"
local BRIDGE_FILE = IPC_DIR .. "/bridge.json"
local SERVICE_FILE = IPC_DIR .. "/service.json"

-- ------------------------------------------------------------------ 共通

local function fail(code, message, details, retryable)
    error({
        sio_error = true,
        code = code,
        message = message,
        details = details or {},
        retryable = retryable or false,
    }, 0)
end

local function int(x)
    return math.floor(x + 0.5)
end

local function log(msg)
    reaper.ShowConsoleMsg("[SIO Bridge] " .. tostring(msg) .. "\n")
end

local function read_file(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read("a")
    f:close()
    return data
end

-- 一時ファイルに書いて close した後、同じディレクトリ内の rename で公開する。
local function write_file_atomic(final, content, replace)
    S.tmp_counter = S.tmp_counter + 1
    local tmp = string.format("%s.%s.%d.tmp", final, S.instance_id:sub(1, 8), S.tmp_counter)
    local f = io.open(tmp, "wb")
    if not f then return false end
    f:write(content)
    f:close()
    if replace then os.remove(final) end
    if not os.rename(tmp, final) then
        os.remove(tmp)
        return false
    end
    return true
end

local function file_exists(path)
    local f = io.open(path, "rb")
    if f then f:close() return true end
    return false
end

-- ------------------------------------------------------------------ 曲とパート

local function open_projects()
    local list = {}
    local i = 0
    while true do
        local p = reaper.EnumProjects(i)
        if not p then break end
        list[#list + 1] = p
        i = i + 1
    end
    return list
end

local function project_is_open(proj)
    if not proj then return false end
    for _, p in ipairs(open_projects()) do
        if p == proj then return true end
    end
    return false
end

local function project_name(proj)
    return reaper.GetProjectName(proj) or ""
end

local function track_name(track)
    local _, name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
    return name or ""
end

-- パート（名前のあるトラック）の一覧と、名前のないトラックの数を返す。
local function list_parts(proj)
    local parts, counts, unnamed = {}, {}, 0
    for i = 0, reaper.CountTracks(proj) - 1 do
        local tr = reaper.GetTrack(proj, i)
        local name = track_name(tr)
        if name == "" then
            unnamed = unnamed + 1
        else
            parts[#parts + 1] = { track = tr, name = name, order = i + 1 }
            counts[name] = (counts[name] or 0) + 1
        end
    end
    for _, p in ipairs(parts) do
        p.addressable = counts[p.name] == 1
        p.name_count = counts[p.name]
    end
    return parts, unnamed
end

local function find_part(parts, name)
    local found, n = nil, 0
    for _, p in ipairs(parts) do
        if p.name == name then
            found = found or p
            n = n + 1
        end
    end
    if n == 0 then
        fail("PART_NOT_FOUND", "指定した名前のパートがありません。", { part_name = name })
    elseif n > 1 then
        fail("AMBIGUOUS_PART", "同じ名前のパートが複数あります。", { part_name = name, match_count = n })
    end
    return found
end

-- 対象の曲は、常に REAPER のアクティブなタブの曲である（SIO は曲の選択状態を持たない）。
-- 既定パートは曲ごとに覚える。S.sel_proj / S.sel_track は、要求の実行開始時に
-- refresh_selection() が求め直す「現在の対象」である。
local function remembered_part(proj)
    for i, m in ipairs(S.part_memory) do
        if m.proj == proj then return m, i end
    end
    return nil
end

local function remember_part(proj, track)
    local m = remembered_part(proj)
    if m then
        m.track = track
    else
        S.part_memory[#S.part_memory + 1] = { proj = proj, track = track }
    end
end

local function forget_part(proj)
    local _, i = remembered_part(proj)
    if i then table.remove(S.part_memory, i) end
end

-- アクティブな曲と、その曲の既定パートを求め直す。
-- 閉じられた曲・削除されたトラック・一意でなくなった名前の既定パートは解除する。
local function refresh_selection()
    local open = open_projects()
    local function is_open(proj)
        for _, p in ipairs(open) do
            if p == proj then return true end
        end
        return false
    end
    for i = #S.part_memory, 1, -1 do
        if not is_open(S.part_memory[i].proj) then table.remove(S.part_memory, i) end
    end
    local active = reaper.EnumProjects(-1)
    S.sel_proj = active
    S.sel_track = nil
    local m = active and remembered_part(active)
    if m then
        local valid = reaper.ValidatePtr2(active, m.track, "MediaTrack*")
        if valid then
            local name = track_name(m.track)
            local n = 0
            if name ~= "" then
                for i = 0, reaper.CountTracks(active) - 1 do
                    if track_name(reaper.GetTrack(active, i)) == name then n = n + 1 end
                end
            end
            valid = n == 1
        end
        if valid then
            S.sel_track = m.track
        else
            forget_part(active)
        end
    end
    for i = #S.blocked, 1, -1 do
        if not is_open(S.blocked[i]) then table.remove(S.blocked, i) end
    end
    return open
end

-- 全曲の既定パートを解除する（Service の起動世代が変わったとき）。
local function clear_selection()
    S.part_memory = {}
    S.sel_track = nil
end

-- 対象の曲（アクティブなタブの曲）を返す。
local function resolve_project(soft)
    refresh_selection()
    if S.sel_proj then return S.sel_proj end
    if soft then return nil end
    fail("NO_PROJECT_OPEN", "開いている曲がありません。")
end

local function selected_part_name()
    if S.sel_track then return track_name(S.sel_track) end
    return NULL
end

local function selected_part(parts)
    if not S.sel_track then return nil end
    for _, p in ipairs(parts) do
        if p.track == S.sel_track then return p end
    end
    return nil
end

local function selection_result(proj)
    return { project_name = project_name(proj), selected_part_name = selected_part_name() }
end

-- expect_project_name の照合（共有の選択状態による曲の取り違えを検出する）。
local function check_expected_project(proj, params)
    local expected = params.expect_project_name
    if expected ~= nil and project_name(proj) ~= expected then
        fail("PROJECT_MISMATCH", "実行時の曲が expect_project_name と一致しません。何も実行していません。",
            { expected = expected, actual = project_name(proj) })
    end
end

local function unnamed_warning(count)
    return {
        code = "UNNAMED_TRACKS_IGNORED",
        message = "名前のないトラックはパートとして扱いません。",
        details = { count = count },
    }
end

local function is_blocked(proj)
    for _, b in ipairs(S.blocked) do
        if b == proj then return true end
    end
    return false
end

-- fn の中で起きた SIO エラーの details に、操作番号などの位置情報を付ける。
local function tagged(key, index, fn)
    local ok, err = pcall(fn)
    if ok then return end
    if type(err) == "table" and err.sio_error and err.details[key] == nil then
        err.details[key] = index
    end
    error(err, 0)
end

-- ------------------------------------------------------------------ 時間

-- 拍子表 { {start_qn, bar, num, den}, ... } を作る。対応外の時間構造は UNSUPPORTED_PROJECT。
local function build_meters(proj)
    local function unsupported(reason, details)
        details = details or {}
        details.reason = reason
        fail("UNSUPPORTED_PROJECT", "この曲の小節・拍子の構成には対応していません（" .. reason .. "）。", details)
    end
    local _, qn0, _, num0, den0 = reaper.TimeMap_GetMeasureInfo(proj, 0)
    if math.abs(qn0) > 1e-6 then
        unsupported("first bar does not start at time zero")
    end
    if proj == reaper.EnumProjects(-1) and reaper.get_config_var_string then
        local ok, offs = reaper.get_config_var_string("projmeasoffs")
        if ok and tonumber(offs) and tonumber(offs) ~= 0 then
            unsupported("nonzero display bar offset")
        end
    end
    local meters = { { start_qn = 0.0, bar = 1, num = int(num0), den = int(den0) } }
    for i = 0, reaper.CountTempoTimeSigMarkers(proj) - 1 do
        local ok, timepos, measurepos, beatpos, _, num, den = reaper.GetTempoTimeSigMarker(proj, i)
        if ok and num > 0 and den > 0 then
            local qn = reaper.TimeMap2_timeToQN(proj, timepos)
            local last = meters[#meters]
            if qn <= 1e-6 then
                if #meters == 1 then
                    last.num, last.den = int(num), int(den)
                end
            else
                local bar = int(measurepos) + 1
                if math.abs(beatpos) > 1e-6 then
                    unsupported("meter change inside a bar", { bar = bar })
                end
                local expected = last.start_qn + (bar - last.bar) * last.num * 4 / last.den
                if math.abs(expected - qn) > 1e-6 then
                    unsupported("partial bar before a meter change", { bar = bar })
                end
                if int(num) ~= last.num or int(den) ~= last.den then
                    meters[#meters + 1] = { start_qn = qn, bar = bar, num = int(num), den = int(den) }
                end
            end
        end
    end
    return meters
end

local function meters_json(meters, end_qn)
    local out = A({})
    for i, m in ipairs(meters) do
        if i == 1 or end_qn == nil or m.start_qn < end_qn + EPS then
            out[#out + 1] = { start_qn = m.start_qn, bar = m.bar, numerator = m.num, denominator = m.den }
        end
    end
    return out
end

-- 要求の Position を QN に変換する。
local function position_to_qn(meters, pos, what)
    if pos.qn ~= nil then
        if pos.qn < 0 then
            fail("INVALID_POSITION", "位置は 0 以上でなければなりません。", { field = what })
        end
        return pos.qn + 0.0
    end
    local bar, beat, offset = pos.bar, pos.beat, pos.offset_qn or 0.0
    if type(bar) ~= "number" or type(beat) ~= "number" or bar < 1 or beat < 1 then
        fail("INVALID_POSITION", "bar と beat は 1 以上の整数でなければなりません。", { field = what })
    end
    local m = meters[1]
    for i = 2, #meters do
        if meters[i].bar <= bar then m = meters[i] else break end
    end
    local beat_len = 4 / m.den
    if beat > m.num then
        fail("INVALID_POSITION", "beat がその小節の拍数を超えています。",
            { field = what, bar = bar, beat = beat, numerator = m.num, denominator = m.den })
    end
    if offset < 0 or offset >= beat_len then
        fail("INVALID_POSITION", "offset_qn は 0 以上かつ 1 拍の長さ未満でなければなりません。",
            { field = what, offset_qn = offset, beat_length_qn = beat_len })
    end
    return m.start_qn + (bar - m.bar) * m.num * beat_len + (beat - 1) * beat_len + offset
end

local function qn_to_position(meters, qn)
    local m = meters[1]
    for i = 2, #meters do
        if meters[i].start_qn <= qn + EPS then m = meters[i] else break end
    end
    local beat_len = 4 / m.den
    local bar_len = m.num * beat_len
    local rel = qn - m.start_qn
    if rel < 0 then rel = 0 end
    local bars = math.floor((rel + EPS) / bar_len)
    local rem = rel - bars * bar_len
    if rem < 0 then rem = 0 end
    local beats = math.floor((rem + EPS) / beat_len)
    if beats >= m.num then beats = m.num - 1 end
    local off = rem - beats * beat_len
    if off < EPS then off = 0.0 end
    return { bar = m.bar + bars, beat = beats + 1, offset_qn = off + 0.0 }
end

local function resolve_range(meters, range)
    local s = position_to_qn(meters, range.start, "range.start")
    local e = position_to_qn(meters, range["end"], "range.end")
    if not (e > s + EPS) then
        fail("INVALID_POSITION", "range の end は start より後でなければなりません。", { start_qn = s, end_qn = e })
    end
    return s, e
end

-- ------------------------------------------------------------------ 列挙

-- テイクの PPQ と曲の QN の変換器を作る。
--
-- MIDI_GetProjQNFromPPQPos / MIDI_GetPPQPosFromProjQN は、非アクティブなタブのテイクに対しても
-- 「アクティブな曲」のテンポ地図で換算してしまう（REAPER 7.86 で確認）。そのため必ず
-- 秒を経由し、曲を明示できる TimeMap2_* で換算する。
-- 秒経由の換算には 1e-15 程度の浮動小数誤差が乗るので、PPQ と QN が線形に対応する通常の
-- アイテムでは「ソース先頭の QN + ティック数 / 分解能」で求めた値を使い、テンポ無視などで
-- 線形に対応しない位置では秒経由の値をそのまま使う。
local function make_clock(proj, take)
    local function general(ppq)
        return reaper.TimeMap2_timeToQN(proj, reaper.MIDI_GetProjTimeFromPPQPos(take, ppq))
    end
    local origin = general(0)
    local clock = { origin_qn = origin }
    local span = 3840
    local per_qn = span / (general(span) - origin)
    local ticks = math.floor(per_qn + 0.5)
    local anchor = nil
    if ticks > 0 and math.abs(per_qn - ticks) < 1e-6 * ticks then
        anchor = math.floor(origin * ticks + 0.5) / ticks
        if math.abs(anchor - origin) > EPS then anchor = origin end
    end
    function clock.to_qn(ppq)
        local g = general(ppq)
        if anchor then
            local l = anchor + ppq / ticks
            if math.abs(l - g) <= EPS then return l end
        end
        return g
    end
    function clock.to_ppq(qn)
        return reaper.MIDI_GetPPQPosFromProjTime(take, reaper.TimeMap2_QNToTime(proj, qn))
    end
    return clock
end

local function is_track_muted(track)
    local tr = track
    while tr do
        if reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") ~= 0 then return true end
        tr = reaper.GetParentTrack(tr)
    end
    return false
end

-- パート内の全 MIDI 音符を列挙して note_id を付ける。途中で defer しない。
-- 戻り値: { part, notes, items, problems, warnings, track_muted }
--   problems が空でなければ、そのパートは取得・編集とも拒否する。
local function enumerate_part(proj, part)
    local tr = part.track
    local en = {
        part = part,
        notes = {},
        items = {},
        problems = A({}),
        warnings = A({}),
        track_muted = is_track_muted(tr),
    }
    local function diag(list, code, message, details)
        details = details or {}
        details.part_name = part.name
        details.track_order = part.order
        list[#list + 1] = { code = code, message = message, details = details }
    end

    local freemode = reaper.GetMediaTrackInfo_Value(tr, "I_FREEMODE")
    local lanes_reported = false
    local audio_items = 0
    local notes = en.notes

    for ii = 0, reaper.CountTrackMediaItems(tr) - 1 do
        local item = reaper.GetTrackMediaItem(tr, ii)
        local ntakes = reaper.CountTakes(item)
        local midi_take, midi_takes = nil, 0
        for ti = 0, ntakes - 1 do
            local tk = reaper.GetTake(item, ti)
            if tk and reaper.TakeIsMIDI(tk) then
                midi_takes = midi_takes + 1
                midi_take = midi_take or tk
            end
        end
        if midi_takes == 0 then
            audio_items = audio_items + 1
        elseif ntakes > 1 then
            diag(en.problems, "MULTIPLE_TAKES", "複数テイクを持つ MIDI アイテムには対応していません。",
                { item_order = ii + 1, count = ntakes })
        else
            local take = midi_take
            local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
            local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
            local rec = {
                item = item,
                take = take,
                index = ii,
                start_qn = reaper.TimeMap2_timeToQN(proj, pos),
                end_qn = reaper.TimeMap2_timeToQN(proj, pos + len),
                muted = reaper.GetMediaItemInfo_Value(item, "B_MUTE") ~= 0,
                readonly = false,
                notes = {},
                ignored = {},       -- アイテム境界の完全に外にある音符（付番しない）
                clock = make_clock(proj, take),
            }
            local clock = rec.clock
            en.items[#en.items + 1] = rec

            if freemode ~= 0 and not lanes_reported then
                lanes_reported = true
                diag(en.problems, "UNSUPPORTED_LANES", "固定レーン／フリー配置のトラックには対応していません。", {})
            end
            local src = reaper.GetMediaItemTake_Source(take)
            local src_type = src and reaper.GetMediaSourceType(src, "") or ""
            if src_type == "MIDIPOOL" then
                diag(en.problems, "POOLED_MIDI_SOURCE", "共有（プール）された MIDI ソースには対応していません。",
                    { item_order = ii + 1 })
            end
            local playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE")
            if math.abs(playrate - 1) > 1e-9 then
                diag(en.problems, "UNSUPPORTED_PLAYRATE", "再生速度が 1 以外のテイクには対応していません。",
                    { item_order = ii + 1, playrate = playrate })
            end
            -- ソース先頭（PPQ 0）の曲内 QN。アイテム先頭と異なればソース開始オフセットがある。
            local src_start_qn = clock.origin_qn
            if reaper.GetMediaItemInfo_Value(item, "B_LOOPSRC") ~= 0 and src then
                -- ループ設定が有効で、実際にソースが繰り返される長さのアイテムを拒否する。
                local src_len, len_is_qn = reaper.GetMediaSourceLength(src)
                if len_is_qn and rec.end_qn > src_start_qn + src_len + 1e-6 then
                    diag(en.problems, "LOOPED_MIDI_ITEM", "ソースをループ再生する MIDI アイテムには対応していません。",
                        { item_order = ii + 1 })
                end
            end
            local start_offs = reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")
            if math.abs(start_offs) > 1e-9 or math.abs(src_start_qn - rec.start_qn) > 1e-6 then
                rec.readonly = true
                diag(en.warnings, "SOURCE_OFFSET_READ_ONLY",
                    "ソース開始オフセットのあるアイテムは読取専用です。", { item_order = ii + 1 })
            end

            local _, note_count = reaper.MIDI_CountEvts(take)
            local outside = 0
            for ni = 0, note_count - 1 do
                local _, selected, muted, sppq, eppq, chan, pitch, vel = reaper.MIDI_GetNote(take, ni)
                local s_qn = clock.to_qn(sppq)
                local e_qn = clock.to_qn(eppq)
                local note = {
                    item = rec,
                    idx = ni,
                    start_qn = s_qn,
                    end_qn = e_qn,
                    sppq = sppq,
                    eppq = eppq,
                    pitch = int(pitch),
                    vel = int(vel),
                    chan = int(chan) + 1,
                    muted = muted and true or false,
                    selected = selected and true or false,
                }
                if e_qn > s_qn and (e_qn <= rec.start_qn + EPS or s_qn >= rec.end_qn - EPS) then
                    -- 完全に境界の外: 鳴らないので除外する。
                    rec.ignored[#rec.ignored + 1] = note
                else
                    if s_qn < rec.start_qn - EPS or e_qn > rec.end_qn + EPS or not (e_qn > s_qn) then
                        outside = outside + 1       -- 境界をまたぐ（または長さが不正）
                    end
                    notes[#notes + 1] = note
                    rec.notes[#rec.notes + 1] = note
                end
            end
            if outside > 0 then
                diag(en.problems, "NOTE_OUTSIDE_ITEM", "アイテム境界をまたぐ音符があります。",
                    { item_order = ii + 1, count = outside })
            end
            if #rec.ignored > 0 then
                diag(en.warnings, "NOTES_OUTSIDE_ITEM_IGNORED", "アイテム境界の外にある音符を除外しました。",
                    { item_order = ii + 1, count = #rec.ignored })
            end
        end
    end

    -- 重なった MIDI アイテム
    local sorted = {}
    for i, rec in ipairs(en.items) do sorted[i] = rec end
    table.sort(sorted, function(a, b)
        if a.start_qn ~= b.start_qn then return a.start_qn < b.start_qn end
        return a.index < b.index
    end)
    for i = 2, #sorted do
        if sorted[i].start_qn < sorted[i - 1].end_qn - 1e-7 then
            diag(en.problems, "OVERLAPPING_ITEMS", "重なった MIDI アイテムには対応していません。",
                { item_order = sorted[i].index + 1, other_item_order = sorted[i - 1].index + 1 })
        end
    end

    if audio_items > 0 then
        diag(en.warnings, "AUDIO_ITEMS_IGNORED", "MIDI 以外のアイテムを無視しました。", { count = audio_items })
    end

    table.sort(notes, function(a, b)
        if a.start_qn ~= b.start_qn then return a.start_qn < b.start_qn end
        if a.pitch ~= b.pitch then return a.pitch < b.pitch end
        if a.item.index ~= b.item.index then return a.item.index < b.item.index end
        return a.idx < b.idx
    end)
    for i, n in ipairs(notes) do n.note_id = i end
    return en
end

local function require_supported(en)
    if #en.problems == 0 then return end
    local only_takes = true
    for _, d in ipairs(en.problems) do
        if d.code ~= "MULTIPLE_TAKES" then only_takes = false end
    end
    if only_takes then
        fail("UNSUPPORTED_TAKES", "複数テイクを持つ MIDI アイテムがあります。テイクを 1 つにしてください。",
            { part_name = en.part.name, diagnostics = en.problems })
    end
    fail("UNSUPPORTED_ITEM", "このパートには対応していない MIDI 構造があります。",
        { part_name = en.part.name, diagnostics = en.problems })
end

-- 列挙のキャッシュ（1 要求の処理中だけ有効）
local function make_enumerator(proj)
    local cache = {}
    return function(part)
        local en = cache[part.order]
        if not en then
            en = enumerate_part(proj, part)
            cache[part.order] = en
        end
        return en
    end
end

-- 要求中の開始位置を QN に統一する。start（Position）があれば start_qn に解決して置き換える。
local function normalize_start(get_meters, obj, field)
    if obj.start ~= nil then
        obj.start_qn = position_to_qn(get_meters(), obj.start, field)
        obj.start = nil
    end
end

local function note_json(meters, en, n)
    return {
        note_id = n.note_id,
        part_name = en.part.name,
        pitch = n.pitch,
        start_qn = n.start_qn,
        duration_qn = n.end_qn - n.start_qn,
        position = qn_to_position(meters, n.start_qn),
        velocity = n.vel,
        channel = n.chan,
        muted = n.muted,
        item_muted = n.item.muted,
        track_muted = en.track_muted,
        selected = n.selected,
    }
end

-- target を現在の列挙結果に対して解決し、expect があれば照合する。
-- get_meters() は拍子表を返す関数（小節・拍で書かれた開始位置の解決に使う）。
-- describe(note) は NOTE_MISMATCH の details.actual 用に音符を JSON 化する関数。
local function resolve_target(en, target, get_meters, describe)
    local notes = en.notes
    local found
    normalize_start(get_meters, target, "target.start")
    if target.expect ~= nil then
        normalize_start(get_meters, target.expect, "target.expect.start")
    end
    if target.note_id ~= nil then
        found = notes[target.note_id]
        if not found then
            fail("NOTE_NOT_FOUND", "指定した note_id の音符がありません。",
                { part_name = en.part.name, note_id = target.note_id, note_count = #notes })
        end
    else
        local pitch, start, channel = target.pitch, target.start_qn, target.channel
        -- start_qn - EPS 以上の最初の音符を二分探索
        local lo, hi = 1, #notes + 1
        local bound = start - EPS
        while lo < hi do
            local mid = (lo + hi) // 2
            if notes[mid].start_qn < bound then lo = mid + 1 else hi = mid end
        end
        local count = 0
        local i = lo
        while i <= #notes and notes[i].start_qn <= start + EPS do
            if notes[i].pitch == pitch and (channel == nil or notes[i].chan == channel) then
                found = found or notes[i]
                count = count + 1
            end
            i = i + 1
        end
        if count == 0 then
            fail("NOTE_NOT_FOUND", "指定した音高と開始位置に一致する音符がありません。",
                { part_name = en.part.name, pitch = pitch, start_qn = start, channel = channel })
        elseif count > 1 then
            fail("AMBIGUOUS_NOTE", "指定した音高と開始位置に一致する音符が複数あります。",
                { part_name = en.part.name, pitch = pitch, start_qn = start, channel = channel, match_count = count })
        end
    end

    local expect = target.expect
    if expect ~= nil then
        local bad = A({})
        if expect.pitch ~= nil and expect.pitch ~= found.pitch then bad[#bad + 1] = "pitch" end
        if expect.start_qn ~= nil and math.abs(expect.start_qn - found.start_qn) > EPS then
            bad[#bad + 1] = "start_qn"
        end
        if expect.duration_qn ~= nil
            and math.abs(expect.duration_qn - (found.end_qn - found.start_qn)) > EPS then
            bad[#bad + 1] = "duration_qn"
        end
        if expect.velocity ~= nil and expect.velocity ~= found.vel then bad[#bad + 1] = "velocity" end
        if expect.channel ~= nil and expect.channel ~= found.chan then bad[#bad + 1] = "channel" end
        if expect.muted ~= nil and expect.muted ~= found.muted then bad[#bad + 1] = "muted" end
        if #bad > 0 then
            fail("NOTE_MISMATCH", "解決した音符が expect と一致しません。何も変更していません。",
                { part_name = en.part.name, mismatched_fields = bad, actual = describe(found) })
        end
    end
    return found
end

-- 操作の対象パート（明示 part_name、なければ選択中パート）
local function operation_part(parts, part_name)
    if part_name ~= nil then
        return find_part(parts, part_name)
    end
    local p = selected_part(parts)
    if not p then
        fail("PART_NOT_SELECTED", "パートが選択されていません。select_part を呼ぶか part_name を指定してください。")
    end
    return p
end

local function is_recording(proj)
    return reaper.GetPlayStateEx(proj) & 4 ~= 0
end

-- ------------------------------------------------------------------ メソッド

local M = {}

function M.list_projects()
    local open = refresh_selection()
    local active = reaper.EnumProjects(-1)
    local counts = {}
    for _, p in ipairs(open) do
        local name = project_name(p)
        counts[name] = (counts[name] or 0) + 1
    end
    local out = A({})
    for _, p in ipairs(open) do
        local name = project_name(p)
        out[#out + 1] = {
            project_name = name,
            active = p == active,
            addressable = name ~= "" and counts[name] == 1,
        }
    end
    return { projects = out }
end

function M.select_part(params)
    local name = params.part_name
    if type(name) ~= "string" or name == "" then
        fail("INVALID_REQUEST", "part_name は空でない文字列でなければなりません。")
    end
    local proj = resolve_project()
    check_expected_project(proj, params)
    local parts = list_parts(proj)
    local part = find_part(parts, name)
    remember_part(proj, part.track)
    S.sel_track = part.track
    reaper.SetOnlyTrackSelected(part.track)     -- REAPER 上でも、このトラックだけを選択状態にする
    return selection_result(proj)
end

function M.get_score_info(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    local meters = build_meters(proj)
    local parts, unnamed = list_parts(proj)
    local constraints, warnings = A({}), A({})
    if is_blocked(proj) then
        constraints[#constraints + 1] = {
            code = "WRITE_BLOCKED",
            message = "復元を確認できなかった書込があるため、この曲への書込を禁止しています。",
            details = { project_name = project_name(proj) },
        }
    end
    if unnamed > 0 then
        warnings[#warnings + 1] = unnamed_warning(unnamed)
    end
    local parts_out = A({})
    local complete = true
    local lo, hi = nil, nil
    for _, p in ipairs(parts) do
        parts_out[#parts_out + 1] = {
            part_name = p.name,
            order = p.order,
            addressable = p.addressable,
            selected = p.track == S.sel_track,
        }
        if not p.addressable then
            constraints[#constraints + 1] = {
                code = "DUPLICATE_PART_NAME",
                message = "同じ名前のトラックが複数あり、名前で指定できません。",
                details = { part_name = p.name, track_order = p.order, count = p.name_count },
            }
        end
        local en = enumerate_part(proj, p)
        for _, d in ipairs(en.problems) do constraints[#constraints + 1] = d end
        for _, d in ipairs(en.warnings) do
            if d.code == "SOURCE_OFFSET_READ_ONLY" then
                constraints[#constraints + 1] = d
            else
                warnings[#warnings + 1] = d
            end
        end
        if #en.problems > 0 then
            complete = false
        else
            for _, n in ipairs(en.notes) do
                if not (n.muted or n.item.muted or en.track_muted) then
                    if not lo or n.start_qn < lo then lo = n.start_qn end
                    if not hi or n.end_qn > hi then hi = n.end_qn end
                end
            end
        end
    end
    local note_range = NULL
    if complete and lo then
        note_range = { start_qn = lo, end_qn = hi }
    end
    return {
        project_name = project_name(proj),
        selected_part_name = selected_part_name(),
        parts = parts_out,
        meters = meters_json(meters, nil),
        note_range = note_range,
        note_range_complete = complete,
        constraints = constraints,
        warnings = warnings,
    }
end

function M.get_notes(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    local parts, unnamed = list_parts(proj)
    local relation = params.relation or "overlaps"
    local include_muted = params.include_muted == true

    -- 対象パート（既定は常に全パート。選択中パートは selected_part:true のときだけ使う）
    local targets = {}
    local warnings = A({})
    if params.part_names ~= nil then
        if params.selected_part == true then
            fail("INVALID_REQUEST", "selected_part:true と part_names は同時に指定できません。")
        end
        local seen = {}
        for _, name in ipairs(params.part_names) do
            if seen[name] then
                fail("INVALID_REQUEST", "part_names に重複があります。", { part_name = name })
            end
            seen[name] = true
            targets[#targets + 1] = find_part(parts, name)
        end
        if #targets == 0 then
            fail("INVALID_REQUEST", "part_names は空にできません。")
        end
        table.sort(targets, function(a, b) return a.order < b.order end)
    elseif params.selected_part == true then
        local sel = selected_part(parts)
        if not sel then
            fail("PART_NOT_SELECTED", "パートが選択されていません。select_part を呼んでください。")
        end
        targets[1] = sel
    else
        local diags, reported = A({}), {}
        for _, p in ipairs(parts) do
            if not p.addressable and not reported[p.name] then
                reported[p.name] = true
                diags[#diags + 1] = {
                    code = "DUPLICATE_PART_NAME",
                    message = "同じ名前のトラックが複数あります。",
                    details = { part_name = p.name, count = p.name_count },
                }
            end
        end
        if #diags > 0 then
            fail("AMBIGUOUS_PART", "名前が重複したパートがあるため、全パートを取得できません。",
                { diagnostics = diags })
        end
        if unnamed > 0 then
            warnings[#warnings + 1] = unnamed_warning(unnamed)
        end
        targets = parts
    end

    -- 範囲
    local meters = build_meters(proj)
    local range_s, range_e, at_qn
    if relation == "sounding_at" then
        if params.at == nil or params.range ~= nil then
            fail("INVALID_REQUEST", "sounding_at には at が必要で、range は指定できません。")
        end
        at_qn = position_to_qn(meters, params.at, "at")
    else
        if params.at ~= nil then
            fail("INVALID_REQUEST", "at は sounding_at でのみ指定できます。")
        end
        if relation ~= "starts_in" and relation ~= "overlaps" then
            fail("INVALID_REQUEST", "relation が不正です。", { relation = relation })
        end
        if params.range ~= nil then
            range_s, range_e = resolve_range(meters, params.range)
        end
    end

    local parts_out, notes_out = A({}), A({})
    local lo, hi = nil, nil
    local count = 0
    for _, p in ipairs(targets) do
        local en = enumerate_part(proj, p)
        require_supported(en)
        for _, w in ipairs(en.warnings) do warnings[#warnings + 1] = w end
        parts_out[#parts_out + 1] = { part_name = p.name, order = p.order }
        local track_muted = en.track_muted
        for _, n in ipairs(en.notes) do
            local keep
            if at_qn then
                keep = n.start_qn <= at_qn + EPS and n.end_qn > at_qn + EPS
            elseif range_s then
                if relation == "starts_in" then
                    keep = n.start_qn >= range_s - EPS and n.start_qn < range_e - EPS
                else
                    keep = n.start_qn < range_e - EPS and n.end_qn > range_s + EPS
                end
            else
                keep = true
            end
            if keep and not include_muted and (n.muted or n.item.muted or track_muted) then
                keep = false
            end
            if keep then
                count = count + 1
                if count > MAX_NOTES then
                    fail("LIMIT_EXCEEDED", "1 回の取得で返せる音符数の上限を超えました。範囲やパートを絞ってください。",
                        { limit = MAX_NOTES })
                end
                if not lo or n.start_qn < lo then lo = n.start_qn end
                if not hi or n.end_qn > hi then hi = n.end_qn end
                notes_out[count] = {
                    note_id = n.note_id,
                    part_name = p.name,
                    pitch = n.pitch,
                    start_qn = n.start_qn,
                    duration_qn = n.end_qn - n.start_qn,
                    position = qn_to_position(meters, n.start_qn),
                    velocity = n.vel,
                    channel = n.chan,
                    muted = n.muted,
                    item_muted = n.item.muted,
                    track_muted = track_muted,
                    selected = n.selected,
                }
            end
        end
    end

    local range_out = NULL
    local extent = at_qn or 0
    if range_s then
        range_out = { start_qn = range_s, end_qn = range_e }
        extent = range_e
    elseif lo then
        range_out = { start_qn = lo, end_qn = hi }
        if hi > extent then extent = hi end
    end

    return {
        project_name = project_name(proj),
        captured_at = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        time_unit = "quarter_note",
        range = range_out,
        parts = parts_out,
        meters = meters_json(meters, extent),
        notes = notes_out,
        warnings = warnings,
    }
end

function M.select_notes(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    if is_recording(proj) then
        fail("TRANSPORT_BUSY", "録音中は音符を選択できません。", {}, true)
    end
    local parts = list_parts(proj)
    local enumerate = make_enumerator(proj)
    local meters
    local resolved, seen = {}, {}
    for i, entry in ipairs(params.targets or {}) do
        tagged("index", i - 1, function()
            local part = operation_part(parts, entry.part_name)
            local en = enumerate(part)
            require_supported(en)
            local function get_meters()
                meters = meters or build_meters(proj)
                return meters
            end
            local note = resolve_target(en, entry.target, get_meters, function(n)
                return note_json(get_meters(), en, n)
            end)
            if seen[note] then
                fail("INVALID_REQUEST", "同じ音符が複数回指定されています。",
                    { part_name = part.name, first_index = seen[note] - 1 })
            end
            seen[note] = i
            resolved[#resolved + 1] = note
        end)
    end

    -- 曲内の他の音符選択を解除し、指定音符だけを選択する。音符以外の選択は変えない。
    -- 解除は対応外のアイテムや複数テイクを含む全 MIDI テイクに対して行う。Undo 点は作らない。
    for ti = 0, reaper.CountTracks(proj) - 1 do
        local tr = reaper.GetTrack(proj, ti)
        for ii = 0, reaper.CountTrackMediaItems(tr) - 1 do
            local item = reaper.GetTrackMediaItem(tr, ii)
            for ki = 0, reaper.CountTakes(item) - 1 do
                local take = reaper.GetTake(item, ki)
                if take and reaper.TakeIsMIDI(take) then
                    local _, note_count = reaper.MIDI_CountEvts(take)
                    for ni = 0, note_count - 1 do
                        local _, selected = reaper.MIDI_GetNote(take, ni)
                        if selected then
                            reaper.MIDI_SetNote(take, ni, false, nil, nil, nil, nil, nil, nil, true)
                        end
                    end
                end
            end
        end
    end
    for _, note in ipairs(resolved) do
        reaper.MIDI_SetNote(note.item.take, note.idx, true, nil, nil, nil, nil, nil, nil, true)
    end
    reaper.UpdateArrange()
    return { project_name = project_name(proj), selected_count = #resolved }
end

-- ---- apply_patch

-- QN をテイクの PPQ（整数ティック）へ。分解能で表現できない位置は拒否する。
local function qn_to_ppq(rec, qn, field, op_index)
    local ppq = math.floor(rec.clock.to_ppq(qn) + 0.5)
    local back = rec.clock.to_qn(ppq)
    if math.abs(back - qn) > EPS then
        fail("INVALID_POSITION", "この位置は MIDI アイテムの分解能（ティック）で表現できません。",
            { operation_index = op_index, field = field, qn = qn, nearest_qn = back })
    end
    return ppq
end

local function note_signature(take)
    local _, note_count, cc_count, text_count = reaper.MIDI_CountEvts(take)
    local list = {}
    for ni = 0, note_count - 1 do
        local _, selected, muted, sppq, eppq, chan, pitch, vel = reaper.MIDI_GetNote(take, ni)
        list[#list + 1] = {
            sppq = sppq, eppq = eppq, pitch = int(pitch), chan = int(chan) + 1, vel = int(vel),
            muted = muted and true or false, selected = selected and true or false,
        }
    end
    return list, cc_count, text_count
end

local function sort_signature(list)
    table.sort(list, function(a, b)
        if a.sppq ~= b.sppq then return a.sppq < b.sppq end
        if a.eppq ~= b.eppq then return a.eppq < b.eppq end
        if a.pitch ~= b.pitch then return a.pitch < b.pitch end
        if a.chan ~= b.chan then return a.chan < b.chan end
        if a.vel ~= b.vel then return a.vel < b.vel end
        if a.muted ~= b.muted then return b.muted end
        if a.selected ~= b.selected then return b.selected end
        return false
    end)
    return list
end

local function signatures_equal(a, b)
    if #a ~= #b then return false end
    for i = 1, #a do
        local x, y = a[i], b[i]
        if x.sppq ~= y.sppq or x.eppq ~= y.eppq or x.pitch ~= y.pitch or x.chan ~= y.chan
            or x.vel ~= y.vel or x.muted ~= y.muted or x.selected ~= y.selected then
            return false
        end
    end
    return true
end

function M.apply_patch(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    if is_blocked(proj) then
        fail("ROLLBACK_FAILED",
            "以前の書込の復元を確認できなかったため、この曲への書込を禁止しています。曲を確認して Bridge を再起動してください。",
            { project_name = project_name(proj), blocked = true })
    end
    if is_recording(proj) then
        fail("TRANSPORT_BUSY", "録音中は編集できません。", {}, true)
    end
    local label = params.label
    if type(label) ~= "string" or label == "" then label = "SIO: Edit notes" end
    local dry_run = params.dry_run == true
    local parts = list_parts(proj)
    local enumerate = make_enumerator(proj)
    local meters = build_meters(proj)
    local function get_meters() return meters end

    -- 1〜3. 全操作を現在状態に対して解決・検証し、変更計画を作る。
    local plans = {}       -- item rec -> { updates = {note -> newvals}, deletes = {note,...}, inserts = {...} }
    local plan_order = {}
    local function plan_for(rec)
        local p = plans[rec]
        if not p then
            p = { rec = rec, updates = {}, deletes = {}, inserts = {} }
            plans[rec] = p
            plan_order[#plan_order + 1] = p
        end
        return p
    end
    local touched = {}     -- note -> 1 始まりの操作番号
    local client_ids = {}
    local added, updated, deleted = 0, 0, 0
    local resolved = A({}) -- update/delete が解決した音符（変更前）
    local inserts = {}     -- insert の計画（結果の inserted 用）

    for i, op in ipairs(params.operations or {}) do
        local op_index = i - 1
        tagged("operation_index", op_index, function()
            local part = operation_part(parts, op.part_name)
            local en = enumerate(part)
            require_supported(en)

            local function writable(rec)
                if rec.readonly then
                    fail("UNSUPPORTED_ITEM", "ソース開始オフセットのあるアイテムは編集できません。", {
                        part_name = part.name,
                        diagnostics = A({ {
                            code = "SOURCE_OFFSET_READ_ONLY",
                            message = "ソース開始オフセットのあるアイテムは読取専用です。",
                            details = { part_name = part.name, track_order = part.order, item_order = rec.index + 1 },
                        } }),
                    })
                end
            end

            if op.op == "insert" then
                local cid = op.client_note_id
                if client_ids[cid] then
                    fail("INVALID_REQUEST", "client_note_id が重複しています。", { client_note_id = cid })
                end
                client_ids[cid] = true
                local nn = op.note
                normalize_start(get_meters, nn, "note.start")
                local s_qn, e_qn = nn.start_qn, nn.start_qn + nn.duration_qn
                -- 重なったアイテムは拒否済みなので、音符全体を含むアイテムは高々 1 つ。
                local target
                for _, rec in ipairs(en.items) do
                    if s_qn >= rec.start_qn - EPS and e_qn <= rec.end_qn + EPS then
                        target = rec
                        break
                    end
                end
                if not target then
                    fail("NO_TARGET_ITEM", "追加する音符全体を含む MIDI アイテムがありません。",
                        { part_name = part.name, client_note_id = cid })
                end
                writable(target)
                local sppq = qn_to_ppq(target, s_qn, "note.start_qn", op_index)
                local eppq = qn_to_ppq(target, e_qn, "note.duration_qn", op_index)
                if eppq <= sppq then
                    fail("INVALID_POSITION", "音符が短すぎて表現できません。", {})
                end
                local nv = {
                    sppq = sppq, eppq = eppq, start_qn = s_qn, end_qn = e_qn,
                    pitch = nn.pitch, vel = nn.velocity, chan = nn.channel,
                    muted = nn.muted and true or false, selected = false,
                    op_index = op_index, client_note_id = cid, part = part, rec = target,
                }
                local p = plan_for(target)
                p.inserts[#p.inserts + 1] = nv
                inserts[#inserts + 1] = nv
                added = added + 1
            elseif op.op == "update" or op.op == "delete" then
                local note = resolve_target(en, op.target, get_meters,
                    function(n) return note_json(meters, en, n) end)
                if touched[note] then
                    fail("INVALID_REQUEST", "同じ音符に対する操作が複数あります。1 つの update にまとめてください。", {
                        part_name = part.name,
                        first_operation_index = touched[note] - 1,
                    })
                end
                touched[note] = i
                local rec = note.item
                writable(rec)
                local p = plan_for(rec)
                if op.op == "delete" then
                    p.deletes[#p.deletes + 1] = note
                    deleted = deleted + 1
                else
                    local set = op.set
                    normalize_start(get_meters, set, "set.start")
                    local nv = {
                        sppq = note.sppq, eppq = note.eppq,
                        start_qn = note.start_qn, end_qn = note.end_qn,
                        pitch = note.pitch, vel = note.vel, chan = note.chan,
                        muted = note.muted, selected = note.selected,
                    }
                    if set.pitch ~= nil then nv.pitch = set.pitch end
                    if set.velocity ~= nil then nv.vel = set.velocity end
                    if set.channel ~= nil then nv.chan = set.channel end
                    if set.muted ~= nil then nv.muted = set.muted and true or false end
                    if set.start_qn ~= nil or set.duration_qn ~= nil then
                        -- start_qn だけなら長さを保って移動し、duration_qn だけなら開始を保つ。
                        local s_qn = set.start_qn ~= nil and set.start_qn or note.start_qn
                        local dur = set.duration_qn ~= nil and set.duration_qn or (note.end_qn - note.start_qn)
                        local e_qn = s_qn + dur
                        if s_qn < rec.start_qn - EPS or e_qn > rec.end_qn + EPS then
                            fail("INVALID_POSITION", "移動・延長後の音符が元の MIDI アイテムに収まりません。", {
                                part_name = part.name,
                                reason = "outside_item",
                                item_range = { start_qn = rec.start_qn, end_qn = rec.end_qn },
                            })
                        end
                        if set.start_qn ~= nil then
                            nv.sppq = qn_to_ppq(rec, s_qn, "set.start_qn", op_index)
                            nv.start_qn = s_qn
                        end
                        nv.eppq = qn_to_ppq(rec, e_qn, "set.duration_qn", op_index)
                        nv.end_qn = e_qn
                        if nv.eppq <= nv.sppq then
                            fail("INVALID_POSITION", "音符が短すぎて表現できません。", {})
                        end
                    end
                    p.updates[note] = nv
                    updated = updated + 1
                end
                resolved[#resolved + 1] = {
                    operation_index = op_index,
                    op = op.op,
                    note = note_json(meters, en, note),
                }
            else
                fail("INVALID_REQUEST", "op が不正です。", {})
            end
        end)
    end

    -- 4. dry_run
    if dry_run then
        return {
            project_name = project_name(proj),
            added = added, updated = updated, deleted = deleted,
            dry_run = true, resolved = resolved, inserted = A({}),
        }
    end

    -- 5. 影響する全テイクの生 MIDI を退避し、書込後の期待状態を作る。
    for _, p in ipairs(plan_order) do
        local take = p.rec.take
        local ok, buf = reaper.MIDI_GetAllEvts(take, "")
        if not ok then
            fail("WRITE_FAILED", "MIDI データを退避できないため、書込を行いませんでした。", {})
        end
        p.backup = buf
        local _, _, cc_count, text_count = reaper.MIDI_CountEvts(take)
        p.cc_count, p.text_count = cc_count, text_count
        local deleted_set = {}
        for _, n in ipairs(p.deletes) do deleted_set[n] = true end
        local expected = {}
        for _, n in ipairs(p.rec.notes) do
            if not deleted_set[n] then
                expected[#expected + 1] = p.updates[n] or n
            end
        end
        for _, n in ipairs(p.rec.ignored) do expected[#expected + 1] = n end   -- 境界外の音符は変更しない
        for _, nv in ipairs(p.inserts) do expected[#expected + 1] = nv end
        p.expected = sort_signature(expected)
    end

    -- 6. Undo ブロック内で、defer を挟まずに適用して再読取検証する。
    local inject = reaper.GetExtState(EXT_SECTION, "test_inject")   -- 受入試験用の失敗注入
    reaper.Undo_BeginBlock2(proj)
    local ok, err = pcall(function()
        for _, p in ipairs(plan_order) do
            local take = p.rec.take
            for note, nv in pairs(p.updates) do
                if not reaper.MIDI_SetNote(take, note.idx, nil, nv.muted, nv.sppq, nv.eppq,
                        nv.chan - 1, nv.pitch, nv.vel, true) then
                    error("MIDI_SetNote failed")
                end
            end
            table.sort(p.deletes, function(a, b) return a.idx > b.idx end)
            for _, note in ipairs(p.deletes) do
                if not reaper.MIDI_DeleteNote(take, note.idx) then
                    error("MIDI_DeleteNote failed")
                end
            end
            for _, nv in ipairs(p.inserts) do
                if not reaper.MIDI_InsertNote(take, false, nv.muted, nv.sppq, nv.eppq,
                        nv.chan - 1, nv.pitch, nv.vel, true) then
                    error("MIDI_InsertNote failed")
                end
            end
            reaper.MIDI_Sort(take)
        end
        if inject == "write_fail" or inject == "rollback_fail" then
            error("injected failure")
        end
        for _, p in ipairs(plan_order) do
            local take = p.rec.take
            local actual, cc_count, text_count = note_signature(take)
            if cc_count ~= p.cc_count or text_count ~= p.text_count then
                error("non-note events changed")
            end
            sort_signature(actual)
            if not signatures_equal(actual, p.expected) then
                error("verification mismatch")
            end
            for k, a in ipairs(actual) do
                local e = p.expected[k]
                if math.abs(p.rec.clock.to_qn(a.sppq) - e.start_qn) > EPS
                    or math.abs(p.rec.clock.to_qn(a.eppq) - e.end_qn) > EPS then
                    error("verification mismatch (time)")
                end
            end
        end
    end)

    if ok then
        for _, p in ipairs(plan_order) do
            reaper.MarkTrackItemsDirty(reaper.GetMediaItem_Track(p.rec.item), p.rec.item)
            reaper.UpdateItemInProject(p.rec.item)
        end
        reaper.Undo_EndBlock2(proj, label, 4)
        reaper.UpdateArrange()

        -- 追加した音符の、編集後の付番による note_id を求める。
        local inserted = A({})
        local function key_of(item_index, sppq, eppq, pitch, chan)
            return string.format("%d:%.0f:%.0f:%d:%d", item_index, sppq, eppq, pitch, chan)
        end
        local fresh = {}   -- part.order -> { key -> {note_id, ...} }
        for _, nv in ipairs(inserts) do
            local map = fresh[nv.part.order]
            if not map then
                map = {}
                for _, n in ipairs(enumerate_part(proj, nv.part).notes) do
                    local key = key_of(n.item.index, n.sppq, n.eppq, n.pitch, n.chan)
                    local list = map[key]
                    if not list then
                        list = {}
                        map[key] = list
                    end
                    list[#list + 1] = n.note_id
                end
                fresh[nv.part.order] = map
            end
            local list = map[key_of(nv.rec.index, nv.sppq, nv.eppq, nv.pitch, nv.chan)]
            inserted[#inserted + 1] = {
                operation_index = nv.op_index,
                client_note_id = nv.client_note_id,
                part_name = nv.part.name,
                note_id = list and table.remove(list, 1) or NULL,
            }
        end
        return {
            project_name = project_name(proj),
            added = added, updated = updated, deleted = deleted,
            dry_run = false, resolved = resolved, inserted = inserted,
        }
    end

    -- 7. 失敗: 全影響データを復元して確認する。
    local restored = true
    for _, p in ipairs(plan_order) do
        local take = p.rec.take
        local set_ok = pcall(reaper.MIDI_SetAllEvts, take, p.backup)
        local got_ok, buf = reaper.MIDI_GetAllEvts(take, "")
        if not set_ok or not got_ok or buf ~= p.backup or inject == "rollback_fail" then
            restored = false
        end
        reaper.UpdateItemInProject(p.rec.item)
    end
    reaper.Undo_EndBlock2(proj, label .. " (failed, restored)", 4)
    reaper.UpdateArrange()
    local reason = type(err) == "table" and (err.message or err.code) or tostring(err)
    if not restored then
        S.blocked[#S.blocked + 1] = proj
        fail("ROLLBACK_FAILED", "書込に失敗し、復元を確認できませんでした。曲を確認して Bridge を再起動してください。",
            { project_name = project_name(proj), reason = reason })
    end
    fail("WRITE_FAILED", "書込に失敗したため、元の状態に復元しました。", { reason = reason })
end

-- ---- 再生

local function playback_release(restore_cursor)
    local pb = S.playback
    S.playback = nil
    if not pb or not project_is_open(pb.proj) then return end
    if restore_cursor then
        reaper.SetEditCurPos2(pb.proj, pb.saved_cursor, false, false)
    end
    -- リピートは、SIO が無効にしたままの場合だけ元に戻す（人間の変更を上書きしない）。
    if pb.saved_repeat == 1 and reaper.GetSetRepeatEx(pb.proj, -1) == 0 then
        reaper.GetSetRepeatEx(pb.proj, 1)
    end
end

local function playback_stop()
    local pb = S.playback
    if not pb then return false end
    if project_is_open(pb.proj) and reaper.GetPlayStateEx(pb.proj) & 1 ~= 0 then
        reaper.OnStopButtonEx(pb.proj)
    end
    playback_release(true)
    return true
end

-- 管理中の範囲再生を確認する（defer の毎周期）。
local function check_playback()
    local pb = S.playback
    if not pb then return end
    if not project_is_open(pb.proj) then
        S.playback = nil
        return
    end
    if reaper.EnumProjects(-1) ~= pb.proj then
        return playback_release(false)          -- タブ切替
    end
    local now = reaper.time_precise()
    local state = reaper.GetPlayStateEx(pb.proj)
    if state & 4 ~= 0 then
        return playback_release(false)          -- 録音の介入
    end
    if state & 1 == 0 or state & 2 ~= 0 then
        if now - pb.started_at < 0.5 then return end
        -- 終端付近で REAPER 自身が停止した場合は正常終了として扱う。
        return playback_release(pb.last_pos >= pb.end_t - 0.3)
    end
    if math.abs(reaper.GetCursorPositionEx(pb.proj) - pb.start_t) > 1e-6 then
        return playback_release(false)          -- 人間のカーソル移動・ジャンプ
    end
    local pos = reaper.GetPlayPositionEx(pb.proj)
    if pos >= pb.end_t then
        reaper.OnStopButtonEx(pb.proj)
        return playback_release(true)
    end
    local rate = math.max(reaper.Master_GetPlayRate(pb.proj), 1)
    if pos < pb.start_t - 0.5 or pos < pb.last_pos - 0.25
        or pos - pb.last_pos > (now - pb.last_time) * rate * 2 + 0.5 then
        return playback_release(false)          -- 位置ジャンプ
    end
    pb.last_pos = pos
    pb.last_time = now
end

function M.play_range(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    local meters = build_meters(proj)
    local s_qn, e_qn = resolve_range(meters, params.range)
    if S.playback or reaper.GetPlayStateEx(proj) ~= 0 then
        fail("TRANSPORT_BUSY", "再生中または録音中です。", {}, true)
    end
    local start_t = reaper.TimeMap2_QNToTime(proj, s_qn)
    local end_t = reaper.TimeMap2_QNToTime(proj, e_qn)
    local pb = {
        proj = proj,
        start_t = start_t,
        end_t = end_t,
        saved_cursor = reaper.GetCursorPositionEx(proj),
        saved_repeat = reaper.GetSetRepeatEx(proj, -1),
        started_at = reaper.time_precise(),
        last_time = reaper.time_precise(),
        last_pos = start_t,
    }
    if pb.saved_repeat == 1 then reaper.GetSetRepeatEx(proj, 0) end
    reaper.SetEditCurPos2(proj, start_t, false, false)
    pb.start_t = reaper.GetCursorPositionEx(proj)
    reaper.OnPlayButtonEx(proj)
    S.playback = pb
    return { project_name = project_name(proj), started = true }
end

-- 選択状態に依存しない: 曲を解決せず、SIO 管理の再生があれば止める。
function M.stop()
    if S.playback then
        playback_stop()
        return { stopped = true }
    end
    return { stopped = false }
end

-- ---- 曲とトラックの作成

local ACTION_NEW_PROJECT_TAB = 40859

-- 指定した名前の曲のタブをアクティブにする（以後の要求の対象になる）。
function M.select_project(params)
    local name = params.project_name
    if type(name) ~= "string" or name == "" then
        fail("INVALID_REQUEST", "project_name は空でない文字列でなければなりません。")
    end
    refresh_selection()
    local found, n = nil, 0
    for _, p in ipairs(open_projects()) do
        if project_name(p) == name then
            found = found or p
            n = n + 1
        end
    end
    if n == 0 then
        fail("PROJECT_NOT_FOUND", "指定した名前の曲は開かれていません。", { project_name = name })
    elseif n > 1 then
        fail("AMBIGUOUS_PROJECT", "同じ名前の曲が複数開かれています。", { project_name = name, match_count = n })
    end
    local active = reaper.EnumProjects(-1)
    if found ~= active then
        if active and is_recording(active) then
            fail("TRANSPORT_BUSY", "録音中は曲を切り替えられません。", {}, true)
        end
        playback_stop()                     -- 管理中の再生は、タブを切り替える前に止める
        reaper.SelectProjectInstance(found)
        if reaper.EnumProjects(-1) ~= found then
            fail("INTERNAL_ERROR", "REAPER が曲のタブを切り替えませんでした。", { reason = "project tab was not activated" })
        end
    end
    refresh_selection()
    return selection_result(found)
end

-- 新しい曲（未保存のタブ）を作る。新しい曲がアクティブなタブになり、以後の要求の対象になる。
function M.create_project()
    refresh_selection()
    local active = reaper.EnumProjects(-1)
    if active and is_recording(active) then
        fail("TRANSPORT_BUSY", "録音中は曲を作成できません。", {}, true)
    end
    playback_stop()                         -- タブが切り替わるので、管理中の再生は先に止める
    local before = open_projects()
    reaper.Main_OnCommand(ACTION_NEW_PROJECT_TAB, 0)
    local proj = reaper.EnumProjects(-1)
    local created = proj ~= nil and #open_projects() == #before + 1
    for _, p in ipairs(before) do
        if p == proj then created = false end
    end
    if not created then
        fail("INTERNAL_ERROR", "REAPER が新しい曲を作成しませんでした。", { reason = "new project tab was not created" })
    end
    forget_part(proj)
    refresh_selection()
    return selection_result(proj)
end

-- 選択中の曲の末尾に、名前付きのトラック（パート）を 1 本追加する。
-- instrument を指定し、その名前の音源プラグインが見つかれば挿入する。なければ空のトラックのままにする。
function M.add_track(params)
    local name = params.part_name
    if type(name) ~= "string" or name == "" then
        fail("INVALID_REQUEST", "part_name は空でない文字列でなければなりません。")
    end
    local instrument = params.instrument
    if instrument ~= nil and (type(instrument) ~= "string" or instrument == "") then
        fail("INVALID_REQUEST", "instrument は空でない文字列でなければなりません。")
    end
    local proj = resolve_project()
    check_expected_project(proj, params)
    if is_blocked(proj) then
        fail("ROLLBACK_FAILED",
            "以前の書込の復元を確認できなかったため、この曲への書込を禁止しています。曲を確認して Bridge を再起動してください。",
            { project_name = project_name(proj), blocked = true })
    end
    if is_recording(proj) then
        fail("TRANSPORT_BUSY", "録音中はトラックを追加できません。", {}, true)
    end
    local index = reaper.CountTracks(proj)
    for i = 0, index - 1 do
        if track_name(reaper.GetTrack(proj, i)) == name then
            fail("PART_ALREADY_EXISTS", "同じ名前のパートがすでにあります。", { part_name = name })
        end
    end

    reaper.Undo_BeginBlock2(proj)
    reaper.InsertTrackInProject(proj, index, 0)         -- flags 0: 既定の FX・エンベロープを付けない
    local track = reaper.GetTrack(proj, index)
    if not track or reaper.CountTracks(proj) ~= index + 1 then
        reaper.Undo_EndBlock2(proj, "SIO: Add track (failed)", -1)
        fail("INTERNAL_ERROR", "REAPER がトラックを追加しませんでした。", { reason = "track was not inserted" })
    end
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)

    local loaded, loaded_name = false, NULL
    local warnings = A({})
    if instrument then
        local fx = reaper.TrackFX_AddByName(track, instrument, false, -1)
        if fx >= 0 then
            if reaper.TrackFX_GetInstrument(track) == fx then
                loaded = true
                local ok, fx_name = reaper.TrackFX_GetFXName(track, fx, "")
                loaded_name = (ok and fx_name ~= "") and fx_name or instrument
            else
                reaper.TrackFX_Delete(track, fx)         -- 音源ではないプラグインは挿入しない
            end
        end
        if not loaded then
            warnings[#warnings + 1] = {
                code = "INSTRUMENT_NOT_FOUND",
                message = "指定した名前の音源プラグインが見つからないため、空のトラックを追加しました。",
                details = { instrument = instrument },
            }
        end
    end
    reaper.Undo_EndBlock2(proj, 'SIO: Add track "' .. name .. '"', -1)
    -- 追加したトラックを select_part したのと同じ状態にする（既定パート、REAPER 上の選択）。
    remember_part(proj, track)
    S.sel_track = track
    reaper.SetOnlyTrackSelected(track)
    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    return {
        project_name = project_name(proj),
        part_name = name,
        order = index + 1,
        selected_part_name = name,
        instrument_loaded = loaded,
        instrument_name = loaded_name,
        warnings = warnings,
    }
end

-- パートのトラックに、指定範囲の空の MIDI アイテムを 1 個作る（音符を入力できる範囲を用意する）。
function M.add_item(params)
    local proj = resolve_project()
    check_expected_project(proj, params)
    if is_blocked(proj) then
        fail("ROLLBACK_FAILED",
            "以前の書込の復元を確認できなかったため、この曲への書込を禁止しています。曲を確認して Bridge を再起動してください。",
            { project_name = project_name(proj), blocked = true })
    end
    if is_recording(proj) then
        fail("TRANSPORT_BUSY", "録音中はアイテムを追加できません。", {}, true)
    end
    if type(params.range) ~= "table" then
        fail("INVALID_REQUEST", "range が必要です。")
    end
    local part = operation_part(list_parts(proj), params.part_name)
    local s_qn, e_qn = resolve_range(build_meters(proj), params.range)
    local track = part.track

    -- 既存のアイテム（種類を問わない）と重なる範囲には作らない。端が接するのは構わない。
    local function item_range(item)
        local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
        local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
        return reaper.TimeMap2_timeToQN(proj, pos), reaper.TimeMap2_timeToQN(proj, pos + len)
    end
    for ii = 0, reaper.CountTrackMediaItems(track) - 1 do
        local a, b = item_range(reaper.GetTrackMediaItem(track, ii))
        if a < e_qn - EPS and b > s_qn + EPS then
            fail("RANGE_OCCUPIED", "指定した範囲に、すでにアイテムがあります。", {
                part_name = part.name,
                range = { start_qn = s_qn, end_qn = e_qn },
                item_range = { start_qn = a, end_qn = b },
            })
        end
    end

    -- QN 指定（qnIn=true）はアクティブな曲のテンポ地図で換算されるおそれがあるため、秒で指定する。
    reaper.Undo_BeginBlock2(proj)
    local item = reaper.CreateNewMIDIItemInProj(track,
        reaper.TimeMap2_QNToTime(proj, s_qn), reaper.TimeMap2_QNToTime(proj, e_qn), false)
    local problem = nil
    if not item then
        problem = "item was not created"
    else
        reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)     -- 人間が端を伸ばしてもループさせない
        local take = reaper.GetActiveTake(item)
        local a, b = item_range(item)
        if not take or not reaper.TakeIsMIDI(take) or reaper.CountTakes(item) ~= 1 then
            problem = "created item has no single MIDI take"
        elseif math.abs(a - s_qn) > EPS or math.abs(b - e_qn) > EPS then
            problem = "created item is not at the requested range"
        else
            local src_len, len_is_qn = reaper.GetMediaSourceLength(reaper.GetMediaItemTake_Source(take))
            local origin = make_clock(proj, take).origin_qn
            if math.abs(origin - s_qn) > 1e-6 or (len_is_qn and origin + src_len < e_qn - 1e-6) then
                problem = "created MIDI source does not cover the requested range"
            end
        end
        if problem then reaper.DeleteTrackMediaItem(track, item) end
    end
    if problem then
        reaper.Undo_EndBlock2(proj, "SIO: Add item (failed, removed)", -1)
        reaper.UpdateArrange()
        fail("WRITE_FAILED", "MIDI アイテムを作成できませんでした。作りかけのアイテムは取り除きました。", { reason = problem })
    end
    reaper.Undo_EndBlock2(proj, 'SIO: Add item to "' .. part.name .. '"', -1)
    reaper.UpdateArrange()
    return {
        project_name = project_name(proj),
        part_name = part.name,
        range = { start_qn = s_qn, end_qn = e_qn },
    }
end

-- ------------------------------------------------------------------ IPC

local function selected_names()
    local pname, tname = NULL, NULL
    if S.sel_proj then pname = project_name(S.sel_proj) end
    if S.sel_track then tname = track_name(S.sel_track) end
    return pname, tname
end

local function write_bridge_state(state)
    refresh_selection()     -- Undo 等で無効になった参照を名前の取得前に捨てる
    local pname, tname = selected_names()
    local doc = {
        protocol_version = PROTOCOL_VERSION,
        bridge_version = BRIDGE_VERSION,
        bridge_instance_id = S.instance_id,
        state = state,
        heartbeat_at = os.time(),
        running_request_id = S.running_request_id or NULL,
        service_instance_id = S.service_id or NULL,
        active_project_name = pname,
        selected_part_name = tname,
        playback_active = S.playback ~= nil,
    }
    return write_file_atomic(BRIDGE_FILE, json.encode(doc), true)
end

-- service.json を読み、Service 起動世代が変わっていたら選択を解除する。
local function check_service_generation()
    local data = read_file(SERVICE_FILE)
    if not data then return nil end
    local ok, doc = pcall(json.decode, data)
    if not ok or type(doc) ~= "table" or type(doc.service_instance_id) ~= "string" then
        return nil
    end
    if doc.service_instance_id ~= S.service_id then
        S.service_id = doc.service_instance_id
        clear_selection()
    end
    return S.service_id
end

local function heartbeat()
    check_service_generation()
    refresh_selection()
    reaper.SetExtState(EXT_SECTION, "bridge_heartbeat", tostring(os.time()), false)
    write_bridge_state("running")
    -- 実行済み ID の記録は 24 時間で捨てる。
    local limit = os.time() - 86400
    for id, t in pairs(S.executed) do
        if t < limit then S.executed[id] = nil end
    end
end

local function error_response(code, message, details, retryable)
    return {
        ok = false,
        error = { code = code, message = message, details = details or {}, retryable = retryable or false },
    }
end

local function execute(req)
    local handler = M[req.method]
    if not handler then
        return error_response("UNKNOWN_METHOD", "Bridge が対応していない method です。", { method = tostring(req.method) })
    end
    local params = req.params
    if type(params) ~= "table" then params = {} end
    local ok, res = xpcall(handler, function(e)
        if type(e) == "table" and e.sio_error then return e end
        return { traceback = debug.traceback(tostring(e), 2) }
    end, params)
    if ok then
        return { ok = true, result = res }
    end
    if res.sio_error then
        return error_response(res.code, res.message, res.details, res.retryable)
    end
    log(res.traceback)
    return error_response("INTERNAL_ERROR", "Bridge の内部エラーです。", { reason = res.traceback })
end

local UUID_PATTERN = "^(%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x)%.json$"

local function process_request(id)
    local claimed = CLAIM_DIR .. "/" .. id .. ".json"
    -- claim: rename に成功した側だけが要求を所有する（Service の取消と排他）。
    if not os.rename(REQ_DIR .. "/" .. id .. ".json", claimed) then
        return
    end
    local data = read_file(claimed)
    local ok, req = pcall(json.decode, data or "")
    local response
    if not ok or type(req) ~= "table" or req.request_id ~= id then
        response = error_response("INTERNAL_ERROR", "Bridge が要求ファイルを解釈できません。", {})
        req = { request_id = id }
    else
        if req.service_instance_id ~= S.service_id then
            check_service_generation()
        end
        if req.service_instance_id ~= S.service_id then
            os.remove(claimed)              -- 旧世代の Service の要求は実行しない
            return
        end
        if S.executed[id] or file_exists(RESP_DIR .. "/" .. id .. ".json") then
            os.remove(claimed)              -- 実行済み request_id は再実行しない
            return
        end
        if req.bridge_instance_id ~= S.instance_id then
            response = error_response("BRIDGE_UNAVAILABLE", "Bridge が再起動されたため、要求を実行しませんでした。",
                { bridge_state = "connected", reason = "bridge_generation_mismatch" }, true)
        elseif type(req.start_deadline) == "number" and os.time() > req.start_deadline then
            response = error_response("BRIDGE_UNAVAILABLE", "実行開始期限を過ぎたため、要求を実行しませんでした。",
                { bridge_state = "connected", reason = "start_deadline_exceeded" }, true)
        end
    end
    S.executed[id] = os.time()
    if not response then
        S.running_request_id = id
        write_bridge_state("running")
        response = execute(req)
    end
    response.request_id = id
    local doc = {
        request_id = id,
        bridge_instance_id = S.instance_id,
        service_instance_id = S.service_id or NULL,
        response = response,
    }
    local enc_ok, encoded = pcall(json.encode, doc)
    if not enc_ok then
        doc.response = error_response("INTERNAL_ERROR", "Bridge が応答を符号化できません。", { reason = tostring(encoded) })
        doc.response.request_id = id
        encoded = json.encode(doc)
    end
    if not write_file_atomic(RESP_DIR .. "/" .. id .. ".json", encoded, false) then
        log("failed to write response for " .. id)
    end
    os.remove(claimed)
    S.running_request_id = nil
    write_bridge_state("running")
end

local function poll_requests()
    reaper.EnumerateFiles(REQ_DIR, -1)       -- ディレクトリ一覧のキャッシュを破棄
    local ids = {}
    local i = 0
    while true do
        local name = reaper.EnumerateFiles(REQ_DIR, i)
        if not name then break end
        local id = name:match(UUID_PATTERN)
        if id then ids[#ids + 1] = id end
        i = i + 1
    end
    for _, id in ipairs(ids) do
        process_request(id)
    end
end

-- ------------------------------------------------------------------ 常駐

local next_poll, next_heartbeat = 0, 0

local function guarded(fn, what)
    local ok, err = xpcall(fn, debug.traceback)
    if not ok then log(what .. ": " .. tostring(err)) end
end

local function loop()
    if S.stopped then return end
    local now = reaper.time_precise()
    if now >= next_heartbeat then
        next_heartbeat = now + HEARTBEAT_INTERVAL
        guarded(heartbeat, "heartbeat")
    end
    if now >= next_poll then
        next_poll = now + POLL_INTERVAL
        guarded(poll_requests, "poll")
    end
    guarded(check_playback, "playback")
    reaper.defer(loop)
end

local function shutdown()
    if S.stopped then return end
    S.stopped = true
    pcall(playback_stop)
    pcall(write_bridge_state, "stopped")
    if reaper.GetExtState(EXT_SECTION, "bridge_instance") == S.instance_id then
        reaper.DeleteExtState(EXT_SECTION, "bridge_instance", false)
        reaper.DeleteExtState(EXT_SECTION, "bridge_heartbeat", false)
    end
    if reaper.set_action_options then reaper.set_action_options(8) end
end

local function main()
    -- 多重起動の防止: REAPER 内の状態（ExtState）に生存中の Bridge があれば起動しない。
    local other = reaper.GetExtState(EXT_SECTION, "bridge_instance")
    local beat = tonumber(reaper.GetExtState(EXT_SECTION, "bridge_heartbeat")) or 0
    if other ~= "" and os.time() - beat <= 3 then
        log("すでに起動しています（instance " .. other .. "）。")
        return
    end
    -- この Action を再実行したら終了する（トグル表示 on）。
    if reaper.set_action_options then reaper.set_action_options(1 | 4) end
    reaper.SetExtState(EXT_SECTION, "bridge_instance", S.instance_id, false)
    reaper.SetExtState(EXT_SECTION, "bridge_heartbeat", tostring(os.time()), false)
    reaper.RecursiveCreateDirectory(REQ_DIR, 0)
    reaper.RecursiveCreateDirectory(CLAIM_DIR, 0)
    reaper.RecursiveCreateDirectory(RESP_DIR, 0)
    reaper.atexit(shutdown)
    loop()
end

main()
