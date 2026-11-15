-- sio_json.lua : SIO Bridge 同梱の最小 JSON ライブラリ（Lua 5.3 以降）。
-- SIO プロジェクトのために書き下ろしたもので、第三者コードを含まない。
--
--   json.encode(value)  -> string         （失敗時は error）
--   json.decode(string) -> value          （失敗時は error）
--   json.null                             JSON null を表す番兵
--   json.array(t)                         t を（空でも）配列として出力させる
--
-- 規則:
--   * 空のテーブルは json.array() で印を付けない限りオブジェクト {} として出力する。
--   * 整数は整数表記、浮動小数は往復可能な精度で出力する。NaN/Infinity は拒否する。
--   * 文字列は UTF-8 のまま通し、制御文字・引用符・逆斜線だけをエスケープする。

local json = {}

json.null = setmetatable({}, { __tostring = function() return "null" end })

local array_mt = { __sio_json_array = true }

function json.array(t)
    return setmetatable(t or {}, array_mt)
end

-- ---------------------------------------------------------------- encode

local escape_map = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escape_char(c)
    return escape_map[c] or string.format("\\u%04x", c:byte())
end

local function encode_string(s)
    return '"' .. s:gsub('[%c"\\]', escape_char) .. '"'
end

local function encode_number(v)
    if math.type(v) == "integer" then
        return string.format("%d", v)
    end
    if v ~= v or v == math.huge or v == -math.huge then
        error("json.encode: non-finite number")
    end
    local s
    if v == math.floor(v) and v > -1e15 and v < 1e15 then
        s = string.format("%.1f", v)
    else
        s = string.format("%.15g", v)
        if tonumber(s) ~= v then
            s = string.format("%.17g", v)
        end
    end
    if s:find(",", 1, true) then
        s = s:gsub(",", ".")
    end
    return s
end

local encode_value

local function encode_table(t, out, depth)
    if depth > 64 then
        error("json.encode: nesting too deep")
    end
    local mt = getmetatable(t)
    local is_array = (mt == array_mt) or (t[1] ~= nil)
    if is_array then
        local n = #t
        if n == 0 then
            out[#out + 1] = "[]"
            return
        end
        out[#out + 1] = "["
        for i = 1, n do
            if i > 1 then out[#out + 1] = "," end
            encode_value(t[i], out, depth + 1)
        end
        out[#out + 1] = "]"
    else
        -- キーは名前順に出力する（出力を決定的にし、人間が見比べやすくするため）。
        local keys = {}
        for k in pairs(t) do
            if type(k) ~= "string" then
                error("json.encode: object key must be a string")
            end
            keys[#keys + 1] = k
        end
        table.sort(keys)
        out[#out + 1] = "{"
        for i, k in ipairs(keys) do
            if i > 1 then out[#out + 1] = "," end
            out[#out + 1] = encode_string(k)
            out[#out + 1] = ":"
            encode_value(t[k], out, depth + 1)
        end
        out[#out + 1] = "}"
    end
end

encode_value = function(v, out, depth)
    local tv = type(v)
    if v == json.null then
        out[#out + 1] = "null"
    elseif tv == "table" then
        encode_table(v, out, depth)
    elseif tv == "string" then
        out[#out + 1] = encode_string(v)
    elseif tv == "number" then
        out[#out + 1] = encode_number(v)
    elseif tv == "boolean" then
        out[#out + 1] = v and "true" or "false"
    else
        error("json.encode: unsupported type " .. tv)
    end
end

function json.encode(v)
    local out = {}
    encode_value(v, out, 0)
    return table.concat(out)
end

-- ---------------------------------------------------------------- decode

local function decode_error(s, pos, msg)
    error(string.format("json.decode: %s at byte %d", msg, pos), 0)
end

local function skip_ws(s, pos)
    local _, e = s:find("^[ \t\r\n]*", pos)
    return e + 1
end

local function utf8_encode(cp)
    return utf8.char(cp)
end

local unescape_map = {
    ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f',
    n = '\n', r = '\r', t = '\t',
}

local function decode_string(s, pos)
    -- pos は開き引用符の位置
    local i = pos + 1
    local parts = {}
    while true do
        local j = s:find('["\\]', i)
        if not j then decode_error(s, pos, "unterminated string") end
        if j > i then parts[#parts + 1] = s:sub(i, j - 1) end
        local c = s:sub(j, j)
        if c == '"' then
            return table.concat(parts), j + 1
        end
        local e = s:sub(j + 1, j + 1)
        if e == "u" then
            local hex = s:match("^%x%x%x%x", j + 2)
            if not hex then decode_error(s, j, "invalid unicode escape") end
            local cp = tonumber(hex, 16)
            i = j + 6
            if cp >= 0xD800 and cp <= 0xDBFF then
                local lo = s:match("^\\u(%x%x%x%x)", i)
                local lo_cp = lo and tonumber(lo, 16)
                if not lo_cp or lo_cp < 0xDC00 or lo_cp > 0xDFFF then
                    decode_error(s, j, "invalid surrogate pair")
                end
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo_cp - 0xDC00)
                i = i + 6
            elseif cp >= 0xDC00 and cp <= 0xDFFF then
                decode_error(s, j, "invalid surrogate pair")
            end
            parts[#parts + 1] = utf8_encode(cp)
        else
            local r = unescape_map[e]
            if not r then decode_error(s, j, "invalid escape") end
            parts[#parts + 1] = r
            i = j + 2
        end
    end
end

local decode_value

local function decode_array(s, pos, depth)
    local arr = json.array({})
    local i = skip_ws(s, pos + 1)
    if s:sub(i, i) == "]" then return arr, i + 1 end
    local n = 0
    while true do
        local v
        v, i = decode_value(s, i, depth + 1)
        n = n + 1
        arr[n] = v
        i = skip_ws(s, i)
        local c = s:sub(i, i)
        if c == "]" then return arr, i + 1 end
        if c ~= "," then decode_error(s, i, "expected ',' or ']'") end
        i = skip_ws(s, i + 1)
    end
end

local function decode_object(s, pos, depth)
    local obj = {}
    local i = skip_ws(s, pos + 1)
    if s:sub(i, i) == "}" then return obj, i + 1 end
    while true do
        if s:sub(i, i) ~= '"' then decode_error(s, i, "expected string key") end
        local k
        k, i = decode_string(s, i)
        i = skip_ws(s, i)
        if s:sub(i, i) ~= ":" then decode_error(s, i, "expected ':'") end
        i = skip_ws(s, i + 1)
        local v
        v, i = decode_value(s, i, depth + 1)
        obj[k] = v
        i = skip_ws(s, i)
        local c = s:sub(i, i)
        if c == "}" then return obj, i + 1 end
        if c ~= "," then decode_error(s, i, "expected ',' or '}'") end
        i = skip_ws(s, i + 1)
    end
end

decode_value = function(s, pos, depth)
    if depth > 64 then decode_error(s, pos, "nesting too deep") end
    local c = s:sub(pos, pos)
    if c == "{" then
        return decode_object(s, pos, depth)
    elseif c == "[" then
        return decode_array(s, pos, depth)
    elseif c == '"' then
        return decode_string(s, pos)
    elseif s:find("^true", pos) then
        return true, pos + 4
    elseif s:find("^false", pos) then
        return false, pos + 5
    elseif s:find("^null", pos) then
        return json.null, pos + 4
    end
    local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
    if not num or num == "" then decode_error(s, pos, "unexpected character") end
    -- tonumber は整数表記を Lua 整数、それ以外を浮動小数として返す。
    local v = tonumber(num)
    if v == nil then decode_error(s, pos, "invalid number") end
    return v, pos + #num
end

function json.decode(s)
    if type(s) ~= "string" then error("json.decode: string expected", 0) end
    local pos = 1
    if s:sub(1, 3) == "\239\187\191" then pos = 4 end
    pos = skip_ws(s, pos)
    local v
    v, pos = decode_value(s, pos, 0)
    pos = skip_ws(s, pos)
    if pos <= #s then decode_error(s, pos, "trailing data") end
    return v
end

return json
