# Score IO (SIO) Client Protocol Specification

**Protocol version:** `2.3`  
**Document revision:** 2.3  
**Updated:** 2026-11-14  
**Target:** REAPER-based SIO Service  
**Status:** Implemented by Service/Bridge 0.6.0 and validated on REAPER 7.86. Section 12 lists what has not been validated.  
**Companion:** `SIO_Specification_ja.md`, version 2.3.

## 1. Purpose

SIO lets client applications, such as score analysis programs and interactive teaching programs, read and edit notes entered in REAPER's piano roll. Applications do not need to select tracks, open an editor, or launch a REAPER action for each acquisition. REAPER and its resident SIO Bridge must already be running. The target project is normally already open; a client can also create an empty one (create_project), add tracks to it (add_track), and add empty MIDI items (add_item).

**This document is the authoritative external contract**: HTTP transport, request and response shapes, errors, and limits. The Japanese companion defines implementation and internal design and only summarizes the contract. If the two disagree about anything a client can observe, this document prevails. **MUST**, **MUST NOT**, **SHOULD**, and **MAY** indicate requirements, prohibitions, recommendations, and options.

SIO retains no snapshots, revision numbers, or public GUIDs. Commands act on the project in REAPER's active tab; projects and parts are named by `project_name` and `part_name`. Existing notes are addressed either by a temporary per-part integer `note_id` or by `pitch` plus `start_qn`. Multiple MIDI items per part are supported; multiple takes are not.

Use `/v2/rpc` and `protocol_version:"2.3"`. Section 12 lists the changes from 2.2. Request fields from earlier versions are not aliases.

### 1.1 Responsibilities

- REAPER owns note input, manual editing, instruments, playback, and saved projects.
- SIO supplies structured data and performs explicit operations.
- Clients own dialogue, exercises, analysis, composition, and pedagogical assessment.
- SIO does not infer notation, enharmonic spelling, voices, harmony, keys, ties, or rests.
- Pitch and duration mean piano-roll data, not audio after MIDI effects, sustain, routing, or instrument release.
- The private Lua/file-IPC transport is not a client interface. MCP is optional.

One SIO installation controls one REAPER process, including multiple open project tabs.

## 2. HTTP transport

| Property | Contract |
|---|---|
| Default base URL | `http://127.0.0.1:18741` |
| RPC endpoint | `POST /v2/rpc` |
| Health endpoint | `GET /health` |
| Encoding / content type | UTF-8 JSON / `application/json` |
| RPC authentication | `Authorization: Bearer <token>` |
| Network scope | Loopback only |
| CORS | Disabled by default |

Clients read the port and token from `<REAPER resource folder>/SIO/config.json`, which the Service creates on first start with fields `host`, `port`, `token`, and optional `allowed_origins`. The default resource folder is `%APPDATA%\REAPER` on Windows, `~/Library/Application Support/REAPER` on macOS, and `~/.config/REAPER` on Linux; portable installations use their own folder. Tokens MUST NOT appear in URLs or logs. Browser applications need their origin listed in `allowed_origins`, or a same-origin backend.

**Trust boundary.** The token and loopback binding protect against web pages, other users, and other hosts. Local processes running as the same user can read config.json and are trusted.

Unauthenticated `GET /health` returns HTTP 200 and `{"status":"ok"}` if the Service is alive. It does not establish Bridge connectivity; call get_status.

### 2.1 Limits

| Resource | Limit | Error when exceeded |
|---|---:|---|
| Request body | 1 MiB | HTTP 413, LIMIT_EXCEEDED |
| Response body | 32 MiB | LIMIT_EXCEEDED |
| Notes per acquisition | 100,000 | LIMIT_EXCEEDED |
| Operations per patch | 10,000 | LIMIT_EXCEEDED |
| Selection targets | 10,000 | LIMIT_EXCEEDED |
| Waiting requests | 32 | HTTP 503, QUEUE_FULL |
| Request result retention | Up to 24 hours within one Service lifetime | REQUEST_EXPIRED |

SIO MUST reject rather than truncate oversized acquisitions. There is no paging or partial-success acquisition. Clients may explicitly narrow ranges or parts. No acquisition cache, acquisition expiry, or revision token exists.

Retained results may expire earlier than 24 hours, oldest first, when their total size exceeds a Service-defined budget (default 256 MiB after compression). The most recent result is always retained.

## 3. RPC envelope

This is an SIO-specific protocol, **not JSON-RPC 2.0**.

### 3.1 Request

```json
{
  "protocol_version": "2.3",
  "request_id": "23e4a562-38be-4f4a-b89d-729dd2cfa327",
  "method": "list_projects",
  "params": {}
}
```

All four fields are required. request_id is a client-generated canonical lowercase UUID. params is always an object. A fresh logical operation uses a fresh ID; a retry of the same operation retains the original ID and payload.

Unknown request fields or method parameters, duplicate JSON keys, non-finite numbers, and incorrect types MUST be rejected. Booleans are not integers. Integer fields accept only JSON numbers written without a fraction or exponent: `60` is valid, `60.0` and `6e1` are not. Number fields accept any finite JSON number. Omit optional values rather than send null; null is never accepted in a request. The server matches protocol_version exactly.

### 3.2 Response

Success:

```json
{
  "request_id": "23e4a562-38be-4f4a-b89d-729dd2cfa327",
  "ok": true,
  "result": {
    "projects": []
  }
}
```

Application failure:

```json
{
  "request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27",
  "ok": false,
  "error": {
    "code": "AMBIGUOUS_NOTE",
    "message": "指定した音高と開始位置に一致する音符が複数あります。",
    "details": {
      "part_name": "Cello",
      "match_count": 2
    },
    "retryable": false
  }
}
```

Exactly one of result or error is present. All error fields are required: code string, message string, details object, retryable boolean. Messages may be Japanese; clients MUST branch on code, not text. retryable never authorizes automatic replay of an uncertain mutation under a new ID.

| HTTP status | Condition | error.code |
|---|---|---|
| 200 | Every request with a well-formed envelope, including version mismatch, unknown method, and invalid params; inspect ok | per section 8 |
| 400 | Body is not valid JSON (including duplicate keys, non-finite numbers, invalid UTF-8), or the envelope is malformed (missing, mistyped, or unknown envelope fields; non-canonical request_id) | INVALID_REQUEST |
| 401 | Authentication failure | UNAUTHORIZED |
| 413 | Request body too large | LIMIT_EXCEEDED |
| 503 | Queue full; not admitted and not executed | QUEUE_FULL |
| Other / disconnect | Transport/server failure; execution may be uncertain | — |

HTTP errors produced by SIO use the same error envelope. request_id is null if it cannot be recovered. Clients MUST tolerate non-JSON HTTP error bodies from intermediaries. Clients SHOULD ignore new response fields but MUST NOT send unknown request fields.

## 4. Names, time, and note references

### 4.1 The target project

**Every project-dependent command acts on the project in REAPER's active tab**, the one the human is looking at. SIO keeps no project selection of its own. The target is determined when a command starts executing, not when its HTTP request is received:

- Anything that brings a tab to the front changes the target: select_project, create_project, a human clicking a tab, and opening or closing a project in REAPER.
- A human or another client can therefore redirect a client between two of its requests. Use expect_project_name (below) to detect this.
- An unnamed (unsaved) project is a valid target while it is active. Its reported project_name is the empty string `""`.
- Renaming or Save As does not change the target; responses report the current name.
- When the active project is closed, the project in whichever tab REAPER activates next becomes the target.
- With no project open, project-dependent commands fail with NO_PROJECT_OPEN. REAPER normally keeps at least one tab, so this is rare.

**select_project** brings an open project's tab to the front. It takes project_name: the exact display name returned by REAPER/list_projects, normally including `.rpp`. It is not a path. Matching uses exact Unicode string equality, including case and whitespace; no normalization, trimming, or extension stripping occurs.

Empty names are INVALID_REQUEST, zero matches PROJECT_NOT_FOUND, and multiple matches AMBIGUOUS_PROJECT. A failed call switches no tab, so the previous target remains; callers must handle the error rather than assume the new target. SIO cannot activate an unnamed project or one whose name is duplicated; a human brings such a tab to the front. select_project opens no project file.

Only select_project accepts project_name as a request parameter. Other commands MUST omit it. Result fields named project_name report the actual current name and are not request selectors.

**expect_project_name.** Because the active tab can change at any time, project-dependent commands (section 6 lists which) accept an optional nonempty string expect_project_name. At execution time it is compared, by exact equality, with the current name of the target project. On a difference the command fails with PROJECT_MISMATCH (details `expected` and `actual`) and does nothing. It is a guard, not a selector: it never switches tabs. An unnamed project cannot be expected.

### 4.2 Parts, part selection, and overrides

**A part is one REAPER track with a nonempty name.** Unnamed tracks (buses, folders, and so on) are not parts: they are not listed, read, or edited. When any exist, get_score_info and all-part reads report the warning UNNAMED_TRACKS_IGNORED with their count. A part's `order` is its original 1-based track number, counting unnamed tracks.

select_part takes part_name, matched exactly within the active project. Empty names are INVALID_REQUEST, zero matches PART_NOT_FOUND, and multiple matches AMBIGUOUS_PART. A failed call does not replace a previous valid selection.

The selected part is held as an internal track reference, not a public GUID. It follows renaming if the resulting name remains nonempty and unique. **One default part is remembered for each open project.** When another tab becomes active, that project's default part (or none) applies, and returning to the first tab restores its default part. Deleting the track, renaming it to an empty/duplicate name, closing the project, or restarting the Service or Bridge clears the default part. The default part is set only by select_part and by add_track, which makes the track it adds the default part (section 6.13).

**The link to REAPER's track selection is one-way.** When select_part or add_track sets the default part, SIO also selects that track in REAPER and deselects every other track, so the human sees which part SIO will work on. The reverse does not hold: a human selecting other tracks in REAPER, one or several, does not change the default part, which stays the part of the last successful select_part or add_track. This differs from projects, where the active tab alone decides the target (section 4.1). There is always at most one default part per project, even though REAPER allows several selected tracks. SIO sets REAPER's track selection only at the moment of these two commands; it does not restore it later, for example after a tab switch. select_project changes the active tab but no track selection.

| Command | Part behavior |
|---|---|
| get_score_info | Always describes all parts in the active project |
| get_notes with neither part_names nor selected_part | All parts, regardless of the selected part |
| get_notes with part_names | Uses the explicit list; the selection is neither used nor changed |
| get_notes with selected_part:true | The selected part; PART_NOT_SELECTED if none |
| update/delete/insert and select_notes target | Explicit part_name overrides; omitted part_name uses the selected part; missing default is PART_NOT_SELECTED |
| play_range/stop | Project-level playback; part selection does not solo or filter playback |

All-part reads fail with AMBIGUOUS_PART if any parts share a name (details.diagnostics lists DUPLICATE_PART_NAME). Explicitly requesting unique parts is allowed even when other parts have duplicate names. get_score_info enumerates duplicate names for diagnosis.

The active tab and the default parts are shared state, not per-client sessions. Concurrent clients controlling different projects are outside this version's scope: another client, or the human, can change them. Use expect_project_name to detect it. Selection commands and REAPER operations are serialized; each operation resolves defaults at its execution start. A compound patch resolves its project and all targets once before editing.

### 4.3 Time types

Canonical time is a finite double-precision number in quarter-note units (`qn`), with project time zero at QN 0. A quarter note is 1.0. Positions are nonnegative; durations are strictly positive. SIO does not quantize to a musical grid.

Intervals are half-open. A note occupies `[start_qn, start_qn + duration_qn)`. Comparisons use `epsilon_qn = 1e-9`, treating boundary differences within epsilon as equal. A note starting at a query point is included; a note ending there is excluded.

**Floating-point noise.** Returned QN values are derived from REAPER's time maps and can differ from the mathematically exact value by about 1e-15. Clients MUST compare QN values using epsilon_qn, not exact equality.

**Tick resolution.** MIDI stores positions as integer ticks at the take's resolution (usually 960 per quarter note). A written start or end position must be representable: if the nearest tick, converted back, differs from the requested QN by more than epsilon_qn, the request fails with INVALID_POSITION before anything is written. SIO never rounds a requested position. For example, at 960 ticks, `0.25` and `1/3` are valid; `0.00001` is not.

A Position is exactly one of:

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

bar and beat are positive integers starting at 1. offset_qn defaults to 0 and must be nonnegative and smaller than one beat. One beat equals the meter denominator's note value: in 6/8, bar 1 beat 2 is QN 0.5. In 4/4, bar 4 beat 2 is QN 13. Clients MUST NOT reinterpret 6/8 as two dotted-quarter beats. A beat beyond the bar's numerator or an offset of one beat or more is INVALID_POSITION.

The supported profile requires zero display bar offset, a complete first bar beginning at time zero, and meter changes only on bar boundaries. Pickups, partial bars, negative positions, and mid-bar meter changes are unsupported (UNSUPPORTED_PROJECT). To write a pickup, begin with a complete bar padded with rests. Tempo changes are allowed; the server uses REAPER's time maps.

A request Range is:

```json
{
  "start": {
    "bar": 1,
    "beat": 1
  },
  "end": {
    "bar": 3,
    "beat": 1
  }
}
```

This represents bars 1–2. End MUST be later than start, otherwise INVALID_REQUEST (both positions in qn form) or INVALID_POSITION (resolved from bar/beat). Each Position independently chooses either representation. Returned QNRange is instead:

```json
{
  "start_qn": 0.0,
  "end_qn": 8.0
}
```

### 4.4 note_id: temporary ordinal

note_id is an integer starting at 1 **within each part**, assigned to all supported MIDI notes in the part in this order:

1. start_qn ascending.
2. pitch ascending.
3. REAPER's track-item enumeration index, then the item's internal note index, to break ties.

The start_qn comparison for ordering is exact, not epsilon-based. Notes from multiple items are combined before numbering. IDs are assigned **before** range and mute filtering; filtered results can contain gaps. The same unchanged part has the same IDs regardless of query range. Notes ignored under section 9 (wholly outside their item) receive no ID.

At operation time, SIO enumerates current notes by the same rules and resolves the ordinal. It does not retain an ID table from a prior acquisition. Insertion, deletion, moving notes, or rearranging items can change ordinals, including after an SIO edit.

A stale ordinal can address a different existing note. SIO does not detect this by itself. Clients MUST either use note_id only when no intervening editing is expected, or attach `expect` (section 4.5) so that a mismatch is rejected. After an edit, reacquire if another ordinal-based operation is needed. Selection alone does not change numbering.

### 4.5 Target

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

A Target MUST use exactly one of these two forms. note_id is a positive integer; pitch is an integer 0–127; start_qn is finite and nonnegative. Partial or mixed forms and unknown fields are invalid. The enclosing operation may supply part_name; if omitted, the selected part is used, or PART_NOT_SELECTED is returned.

The coordinate form MAY add `channel` (integer 1–16), restricting the match to that channel. This distinguishes simultaneous notes of the same pitch on different channels:

```json
{
  "pitch": 60,
  "start_qn": 16.0,
  "channel": 2
}
```

**Start positions as Position.** Wherever a request carries a start position as start_qn, it MAY instead carry `start`, a Position (section 4.3), so that the position can be written as bar and beat:

```json
{
  "pitch": 60,
  "start": {
    "bar": 5,
    "beat": 1
  }
}
```

This applies to four places: the coordinate form of Target, a Target's `expect`, the note of an insert, and the set of an update. start_qn and start MUST NOT both be present; the coordinate Target and the insert note require exactly one. `start` is resolved to QN with the project's meter map at execution time and then treated exactly like start_qn. An unresolvable position (for example a beat beyond the bar) is INVALID_POSITION; an unsupported timeline is UNSUPPORTED_PROJECT. Durations are always given in QN. Responses are unchanged: notes carry both start_qn and position.

Coordinate lookup searches all current notes in that part, including muted notes. Pitch and channel must match exactly and start_qn within epsilon_qn. No match is NOTE_NOT_FOUND; multiple matches are AMBIGUOUS_NOTE. The server MUST NOT choose the nearest note or apply to all matches.

Coordinate lookup is **not identity tracking**. A manually changed pitch/start can cause no match; a changed duration still matches. A newly created replacement at the same pitch/start also matches.

**expect.** Either form MAY carry `expect`, an object with one or more of pitch, start_qn (or start), duration_qn, velocity, channel, and muted:

```json
{
  "note_id": 5,
  "expect": {
    "pitch": 60,
    "start_qn": 16.0
  }
}
```

After the target is resolved, each listed attribute is compared with the current note (integers and booleans exactly, QN values within epsilon_qn). Any difference fails the whole request with NOTE_MISMATCH and nothing is modified. Details contain part_name, `mismatched_fields` (attribute names), and `actual` (the resolved Note). This detects a stale ordinal pointing at another note, or a coordinate match whose duration was changed. The server keeps no state for it.

## 5. Returned data types

All fields listed in this section are required unless explicitly nullable or optional.

### 5.1 Part

| Field | Type | Meaning |
|---|---|---|
| part_name | string | Exact, nonempty track name |
| order | integer >= 1 | Original REAPER track number, even in a filtered result |

### 5.2 Note

```json
{
  "note_id": 5,
  "part_name": "Cello",
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

| Field | Constraint / meaning |
|---|---|
| note_id | Positive per-part ordinal from section 4.4 |
| part_name | Exact part name |
| pitch | Integer 0–127; middle C is 60 |
| start_qn | Finite number >= 0 |
| duration_qn | Finite number > 0 |
| position | Derived bar/beat/offset display position |
| velocity | Integer 1–127 |
| channel | Integer 1–16, not REAPER's internal zero-based value |
| muted | Boolean note flag |
| item_muted | Boolean containing-item flag |
| track_muted | Boolean including muted ancestor folders |
| selected | Boolean captured note selection |

An **unmuted note** is one whose muted, item_muted, and track_muted are all false. Duplicate simultaneous notes remain distinct. Notes are not clipped to query boundaries. Solo, volume, plugin output, and acoustic release do not affect note-interval lookup.

### 5.3 Meter and Diagnostic

Meter has start_qn (number), bar (positive integer), numerator (positive integer), and denominator (positive integer).

Diagnostic has code (string), message (string), and details (object). Details may include part_name, track_order, item_order, and count. Internal GUIDs are not public targets.

| Diagnostic code | Meaning | Effect |
|---|---|---|
| MULTIPLE_TAKES | A MIDI item has more than one take | Part rejected (UNSUPPORTED_TAKES) |
| LOOPED_MIDI_ITEM | The item is longer than its source and actually repeats it | Part rejected (UNSUPPORTED_ITEM) |
| POOLED_MIDI_SOURCE | Pooled (shared) MIDI source | Part rejected (UNSUPPORTED_ITEM) |
| UNSUPPORTED_LANES | Fixed lanes or free item positioning | Part rejected (UNSUPPORTED_ITEM) |
| OVERLAPPING_ITEMS | Overlapping MIDI items on one track | Part rejected (UNSUPPORTED_ITEM) |
| UNSUPPORTED_PLAYRATE | Take playback rate is not 1 | Part rejected (UNSUPPORTED_ITEM) |
| NOTE_OUTSIDE_ITEM | A note straddles an item boundary | Part rejected (UNSUPPORTED_ITEM) |
| SOURCE_OFFSET_READ_ONLY | Nonzero source start offset | Readable; edits rejected (UNSUPPORTED_ITEM) |
| DUPLICATE_PART_NAME | Parts share a name | Not addressable by name; all-part reads fail (AMBIGUOUS_PART) |
| WRITE_BLOCKED | Writes blocked after ROLLBACK_FAILED | apply_patch, add_track, and add_item return ROLLBACK_FAILED |
| AUDIO_ITEMS_IGNORED | Non-MIDI or empty items present | Ignored; warning with count |
| UNNAMED_TRACKS_IGNORED | Unnamed tracks present | Not parts; warning with count |
| NOTES_OUTSIDE_ITEM_IGNORED | Notes lie wholly outside their item | Ignored and unnumbered; warning with count |
| INSTRUMENT_NOT_FOUND | add_track found no instrument plug-in with the requested name | Track added without a plug-in; warning with the requested name |

In get_score_info, `constraints` holds diagnostics that restrict reading, editing, or addressing (the first ten rows) and `warnings` holds notices about ignored content (AUDIO_ITEMS_IGNORED, UNNAMED_TRACKS_IGNORED, NOTES_OUTSIDE_ITEM_IGNORED). In a get_notes result, `warnings` holds those three plus SOURCE_OFFSET_READ_ONLY. INSTRUMENT_NOT_FOUND appears only in an add_track result.

### 5.4 Note acquisition result

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
      "part_name": "Cello",
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

captured_at is a UTC RFC 3339 timestamp, not a concurrency token. time_unit is always quarter_note. The server does not retain this acquisition for later reference.

For an explicit range, returned range is that resolved range even if no notes match. For an unbounded or sounding_at query, it covers returned notes, or is null when none match. It is not an editing permission boundary.

parts includes requested empty parts in track order. notes is ordered by part order then note_id. meters includes the meter at QN zero and subsequent changes through the requested/returned extent; an empty point query still includes the context needed for that point. warnings is a Diagnostic array.

Acquisition occurs within one Bridge callback without yielding to manual editing midway. This is not a promise that data remains current after it is returned. Clients retain returned JSON themselves if needed for analysis.

## 6. Methods

Commands marked **[E]** accept the optional expect_project_name parameter (section 4.1) in addition to the parameters listed.

### 6.1 get_status

Params: `{}`.

```json
{
  "protocol_version": "2.3",
  "service_version": "0.6.0",
  "service_instance_id": "9f514992-79da-4918-b013-1ee6b8a9576c",
  "bridge_state": "connected",
  "bridge_version": "0.6.0",
  "bridge_instance_id": "d183a940-d408-4dbe-a5e7-04ac15c84330",
  "running_request_id": null,
  "active_project_name": "Harmony.rpp",
  "selected_part_name": null,
  "playback_active": false
}
```

bridge_state is connected, unavailable (no Bridge, or it stopped cleanly), or unresponsive (no heartbeat for 3 seconds; the Bridge may be busy executing, so this does not prove it stopped). Bridge version/instance ID are nullable if unknown; running_request_id is a UUID string or null. Instance IDs change on component restart and identify **communication lifetimes**, not note revisions or project identity.

active_project_name is the name of the project in the active tab (the target, section 4.1) and selected_part_name is that project's default part; both are string-or-null. playback_active is a boolean telling whether SIO-managed playback is in progress. All three describe the last state confirmed by the Bridge, refreshed about once per second, and can be stale during disconnection or pending Bridge work. active_project_name is `""` for an unnamed project. This method does not wait behind Bridge work.

### 6.2 list_projects

Params: `{}`.

```json
{
  "projects": [
    {
      "project_name": "Harmony.rpp",
      "active": true,
      "addressable": true
    },
    {
      "project_name": "Study.rpp",
      "active": false,
      "addressable": false
    },
    {
      "project_name": "Study.rpp",
      "active": false,
      "addressable": false
    }
  ]
}
```

All fields are required. addressable means the name is nonempty and unique among open tabs. Duplicate or empty names are still enumerated, but cannot be used as selectors. active marks the project in the active tab, which is the target of project-dependent commands; exactly one entry is active. It queries the Bridge for current state; get_status may only have cached state.

### 6.3 select_project

Params: project_name (required nonempty string). Brings that project's tab to the front, making it the target (section 4.1).

```json
{
  "project_name": "Harmony.rpp"
}
```

Result:

```json
{
  "project_name": "Harmony.rpp",
  "selected_part_name": null
}
```

The result reports the current name and the default part remembered for that project (null if none; section 4.2). If the project is already active, nothing changes. Otherwise SIO-managed playback, if any, is first stopped exactly as by stop, and the tab is switched. While the currently active project is recording, switching fails with TRANSPORT_BUSY. A failed call switches no tab.

### 6.4 select_part [E]

Params: part_name (required nonempty string), in the active project.

```json
{
  "part_name": "Cello"
}
```

Result:

```json
{
  "project_name": "Harmony.rpp",
  "selected_part_name": "Cello"
}
```

Selection affects default targeting as listed in section 4.2. In REAPER, the part's track becomes the only selected track (section 4.2). Playback routing is not altered. A failed call changes neither the default part nor REAPER's track selection.

### 6.5 get_score_info [E]

Params: `{}`. Uses the active project.

```json
{
  "project_name": "Harmony.rpp",
  "selected_part_name": "Cello",
  "parts": [
    {
      "part_name": "Violin I",
      "order": 1,
      "addressable": true,
      "selected": false
    },
    {
      "part_name": "Cello",
      "order": 2,
      "addressable": true,
      "selected": true
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
  "note_range": {
    "start_qn": 0.0,
    "end_qn": 32.0
  },
  "note_range_complete": true,
  "constraints": [],
  "warnings": []
}
```

The parts list extends Part with required booleans addressable and selected. addressable is false for duplicate names; selected denotes the default SIO part. Unnamed tracks are not listed. selected_part_name is a string or null. constraints and warnings are Diagnostic arrays, divided as in section 5.3.

note_range is the extent of supported unmuted notes, or null for an empty score. If unsupported MIDI content prevents a complete extent, it is null and note_range_complete is false. Duplicate names alone do not prevent calculating the extent. A valid empty score returns null and true. This method returns no note IDs. An unsupported timeline fails the call with UNSUPPORTED_PROJECT.

### 6.6 get_notes [E]

| Param | Type | Requirement / default |
|---|---|---|
| part_names | nonempty string array | Optional explicit list; no duplicate entries; incompatible with selected_part:true |
| selected_part | boolean | Default false; true reads only the selected part |
| range | Range | Optional; incompatible with at |
| at | Position | Required with sounding_at; forbidden with other relations |
| relation | enum | starts_in / overlaps / sounding_at; default overlaps |
| include_muted | boolean | Default false |

Result: the acquisition result from section 5.4 directly in result. Uses the active project. **With neither part_names nor selected_part:true, all parts are read**; the selected part never changes the result implicitly. selected_part:true without a selected part is PART_NOT_SELECTED. Neither form changes the selection.

starts_in matches starts in the half-open range. overlaps matches a positive-duration intersection, including notes sustained from earlier. Omitting range for these relations retrieves all notes in the requested parts. sounding_at requires at and forbids range.

When include_muted is false, only unmuted notes (section 5.2) are returned. IDs are assigned before this exclusion. Unsupported MIDI in any requested part fails the whole request rather than silently returning incomplete data. Reads do not depend on REAPER's track or item selection.

```json
{
  "part_names": [
    "Violin I",
    "Cello"
  ],
  "at": {
    "bar": 4,
    "beat": 2
  },
  "relation": "sounding_at",
  "include_muted": false
}
```

### 6.7 select_notes [E]

Params: targets (required array of 0–10,000 entries). Each entry contains required target and optional part_name. Omitted part_name uses the selected part or fails with PART_NOT_SELECTED. Uses the active project:

```json
{
  "targets": [
    {
      "part_name": "Cello",
      "target": {
        "note_id": 5
      }
    },
    {
      "part_name": "Violin I",
      "target": {
        "pitch": 65,
        "start_qn": 16.0
      }
    }
  ]
}
```

All targets are resolved against current state before changing selection. Multiple entries resolving to the same actual note are INVALID_REQUEST. A missing, ambiguous, or mismatching (`expect`) target has no selection side effect. Errors caused by one entry include its zero-based `index` in details.

SIO selects these notes and clears every other MIDI note selection in the project, including in items SIO does not otherwise support, leaving non-note MIDI selection unchanged. An empty array clears note selection. It does not modify note contents, create an Undo point, open an editor, activate a tab, or bring REAPER forward. Selection alone does not change note_id numbering. Recording causes TRANSPORT_BUSY.

Result:

```json
{
  "project_name": "Harmony.rpp",
  "selected_count": 2
}
```

### 6.8 apply_patch [E]

| Param | Type | Requirement / default |
|---|---|---|
| operations | Operation[] | Required; 1–10,000 entries |
| label | string | Optional Undo label; omitted or empty means `SIO: Edit notes` |
| dry_run | boolean | Default false |

Uses the active project. Every operation may include part_name as a local override. Without it, use the selected part; if none is selected, return PART_NOT_SELECTED. Overrides do not modify selection, and one patch can edit multiple explicitly named parts.

#### Operation overview

| op | Required fields in addition to op | Meaning |
|---|---|---|
| insert | client_note_id, note (part_name optional) | Add a note |
| update | target, set (part_name optional) | Change existing note attributes |
| delete | target (part_name optional) | Delete one existing note |

#### insert

```json
{
  "op": "insert",
  "client_note_id": "new-1",
  "part_name": "Cello",
  "note": {
    "pitch": 60,
    "start_qn": 20.0,
    "duration_qn": 1.0,
    "velocity": 80,
    "channel": 1,
    "muted": false
  }
}
```

client_note_id is a nonempty string unique within the request. The result's `inserted` array maps it to the new note's note_id. All six note fields are required, using Note's value constraints; the start position is given as either start_qn or start (section 4.5). No defaults are inferred. The note must fit wholly inside one existing supported MIDI item; otherwise NO_TARGET_ITEM. SIO does not create or extend tracks/items. New notes are unselected.

#### update

```json
{
  "op": "update",
  "part_name": "Cello",
  "target": {
    "note_id": 5,
    "expect": {
      "pitch": 60
    }
  },
  "set": {
    "pitch": 64,
    "duration_qn": 2.0
  }
}
```

```json
{
  "op": "update",
  "part_name": "Cello",
  "target": {
    "pitch": 60,
    "start_qn": 16.0
  },
  "set": {
    "pitch": 64
  }
}
```

set contains at least one of pitch, start_qn (or start), duration_qn, velocity, channel, or muted, with the same type/range constraints as insert. Values are absolute replacements, not increments. Unspecified fields remain unchanged: setting only start_qn moves the note and keeps its duration; setting only duration_qn keeps its start.

part_name, position, selected, item_muted, and track_muted are not settable. To move a note to another part, delete it and insert into the other part. A moved or extended note must stay wholly inside its original item; otherwise INVALID_POSITION with details.reason `outside_item`.

#### delete

```json
{
  "op": "delete",
  "part_name": "Cello",
  "target": {
    "pitch": 60,
    "start_qn": 16.0
  }
}
```

The operation deletes exactly one resolved note. It does not delete all coincident notes. Use note_id, or the coordinate form with channel, to address one of several simultaneous notes of the same pitch.

#### Operation-set semantics

- All existing-note targets are resolved, and their `expect` checked, against current state **before any modification**.
- Operations form a change set, not an array-order program. The implementation may reorder writes internally to avoid shifting note indexes.
- Mixed target forms are allowed, but two operations resolving to the same actual note cause INVALID_REQUEST even if their syntax differs.
- An inserted note cannot be referenced by another operation in the same patch. Include its final attributes in insert.
- Duplicate client_note_id values, empty operations, empty set, unknown fields, and invalid values are INVALID_REQUEST. More than 10,000 operations is LIMIT_EXCEEDED.
- The server validates every operation before writing. It does not return partial success. Errors caused by one operation include its zero-based `operation_index` in details.
- Without `expect`, there is **no acquired-state validation**: correctly resolved current notes may differ from what an earlier analysis observed.
- Non-note MIDI data is preserved. Existing note selection flags are retained.
- Recording causes TRANSPORT_BUSY, including for dry_run.
- **Overlapping notes of the same pitch on the same channel cannot be written.** MIDI cannot pair their note-on and note-off events unambiguously, and REAPER re-pairs them. A patch that would create such an overlap fails verification with WRITE_FAILED and is rolled back. Put simultaneous same-pitch notes on different channels.

Result:

```json
{
  "project_name": "Harmony.rpp",
  "added": 1,
  "updated": 1,
  "deleted": 0,
  "dry_run": false,
  "resolved": [
    {
      "operation_index": 0,
      "op": "update",
      "note": {
        "note_id": 5,
        "part_name": "Cello",
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
      "operation_index": 1,
      "client_note_id": "new-1",
      "part_name": "Cello",
      "note_id": 7
    }
  ]
}
```

- project_name is the current name of the project that was edited.
- Counts describe accepted operations; setting an unchanged value still counts as an update.
- resolved lists, in operation order, the note each update and delete resolved to, **as it was before the edit**. It is returned for both dry runs and real patches.
- inserted lists the note_id of each added note **in the numbering after the edit**. It is empty for a dry run.
- A real patch is one normal REAPER Undo step in the edited project. Existing notes' ordinals can change; reacquire before reusing them.

For dry_run, counts and resolved describe the planned operations and dry_run is true. No musical data or Undo history is changed. A dry run reserves nothing and does not guarantee a later application targets the same notes. Actual application uses a fresh request_id.

On write failure SIO restores affected MIDI data and verifies restoration (WRITE_FAILED). A crash can leave an uncertain result. If restoration cannot be verified, ROLLBACK_FAILED blocks further apply_patch calls to that open project, which also return ROLLBACK_FAILED with details.blocked true, until human inspection and a Bridge restart. Reads remain possible, and get_score_info reports WRITE_BLOCKED. Renaming the project does not remove the block.

### 6.9 play_range [E]

Params: range (required Range). Uses the active project, ignoring part selection for playback routing. Result:

```json
{
  "project_name": "Harmony.rpp",
  "started": true
}
```

This acknowledges start, not completion.

Existing playback, recording, pause, or SIO-managed playback causes TRANSPORT_BUSY. SIO temporarily disables repeat and starts at the range start, stopping at the end and restoring the prior cursor and repeat state on normal completion. Stop timing follows GUI callbacks, not sample-accurate scheduling.

Human stop or pause, moving the edit cursor, a position jump, tab change, or recording releases SIO playback management. SIO then does not restart playback or restore the old cursor. It restores the repeat setting only if it is still as SIO left it. No completion notification is defined; poll get_status.playback_active.

### 6.10 stop

Params: `{}`. Result: `{"stopped":true}` if SIO-managed playback was stopped (with cursor and repeat restored), otherwise `{"stopped":false}`. It never stops human-started playback.

stop resolves no target project and needs no default part.

### 6.11 get_request_status

Params: target_request_id (required UUID).

```json
{
  "target_request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27",
  "state": "succeeded",
  "response": {
    "request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27",
    "ok": true,
    "result": {
      "stopped": false
    }
  }
}
```

| state | Meaning | response |
|---|---|---|
| queued | Accepted, awaiting Bridge | null |
| running | Dispatched, no terminal result | null |
| succeeded | Successful terminal result | Original success envelope |
| failed | Terminal error, including known indeterminate outcome | Original error envelope |
| expired | Known ID, retained result expired | null |
| unknown | No retained knowledge | null |

OUTCOME_UNKNOWN may appear with failed and does not prove no write occurred. Unknown status likewise does not prove absence of execution. Status queries have their own fresh outer request_id and do not replay the target. They do not wait behind Bridge work. Poll at 250–1,000 ms with backoff.

### 6.12 create_project

Params: `{}`.

Creates a new, unsaved project in a new REAPER tab. The new tab becomes the active tab, so the new project is the target of subsequent commands. It has no default part. The result has the same shape as that of select_project:

```json
{
  "project_name": "",
  "selected_part_name": null
}
```

- SIO-managed playback, if any, is stopped first, exactly as by stop.
- Nothing is written to disk. The new project is unnamed, so its project_name is `""`. Until a human saves it under a unique name, it cannot be chosen with select_project and cannot be guarded with expect_project_name. It remains the target for as long as its tab is active. After another tab has been activated, only a human can bring it back to the front.
- The project is created according to the user's REAPER settings for new projects. If a default project template is configured, the project may already contain tracks; call get_score_info to see them.
- While the active project is recording, the call fails with TRANSPORT_BUSY and creates nothing.
- Every successful call creates another project. On a lost response, follow section 7 (same request_id, or get_request_status); do not send it again under a new ID.

SIO has no method that opens, saves, renames, or closes a project.

### 6.13 add_track [E]

Params: part_name (required nonempty string), instrument (optional nonempty string).

```json
{
  "part_name": "Violin I",
  "instrument": "ReaSynth"
}
```

Appends one track named part_name after the last track of the active project. Because the track is named, it is a new part. If a track with exactly that name already exists, the call fails with PART_ALREADY_EXISTS (details part_name) and changes nothing.

instrument names a virtual instrument plug-in to insert on the new track. The string is passed unchanged to REAPER's plug-in lookup by name. The full name shown in REAPER's FX browser (`VSTi: ReaSynth (Cockos)`) always identifies a plug-in; a distinctive part of it (`ReaSynth`) usually does, and which plug-in a partial name selects is decided by REAPER. **If no plug-in is found, or the plug-in found is not an instrument, the track is still added, without any plug-in.** The call succeeds with instrument_loaded false and the warning INSTRUMENT_NOT_FOUND (details `instrument`: the requested string). Without instrument, the track is added empty and no warning is reported.

```json
{
  "project_name": "Harmony.rpp",
  "part_name": "Violin I",
  "order": 3,
  "selected_part_name": "Violin I",
  "instrument_loaded": true,
  "instrument_name": "VSTi: ReaSynth (Cockos)",
  "warnings": []
}
```

All fields are required. order is the part's track number as defined in section 4.2. selected_part_name is the default part after the call, always equal to part_name. instrument_name is the name REAPER reports for the inserted plug-in, or null when none was inserted. warnings is a Diagnostic array.

- The plug-in is inserted in its default state. SIO has no method for presets, plug-in parameters, routing, or removing tracks.
- The new track has no MIDI item. Notes can only be inserted into an existing item (NO_TARGET_ITEM otherwise); create one with add_item (section 6.14) or in REAPER.
- **The new track becomes the default part, exactly as if select_part had been called for it**: it replaces the previous default part and becomes the only selected track in REAPER (section 4.2). This happens whether or not an instrument was loaded. The result's selected_part_name reports it.
- The addition is one REAPER undo point labeled `SIO: Add track "<part_name>"`.
- While the project is recording, the call fails with TRANSPORT_BUSY. If writes to the project are blocked (WRITE_BLOCKED), it fails with ROLLBACK_FAILED.
- Loading a large instrument can take longer than the waiting budget; the call may then return REQUEST_PENDING (section 7). Every successful call adds another track, so never repeat it under a new ID after an uncertain outcome; reacquire with get_score_info instead.

### 6.14 add_item [E]

Params: range (required Range, section 4.3), part_name (optional nonempty string; the selected part when omitted, PART_NOT_SELECTED if there is none).

```json
{
  "part_name": "Violin I",
  "range": {
    "start": {"bar": 1, "beat": 1},
    "end": {"bar": 9, "beat": 1}
  }
}
```

Creates one empty MIDI item covering exactly the range on the part's track. Its purpose is to prepare a region into which notes can be inserted, by apply_patch or by a human in the piano roll.

```json
{
  "project_name": "Harmony.rpp",
  "part_name": "Violin I",
  "range": {
    "start_qn": 0.0,
    "end_qn": 32.0
  }
}
```

All fields are required. range is the resolved range. No item identifier is returned: items remain private, and the new item is afterwards indistinguishable from one a human created.

- If the range overlaps any existing item on the track, of any kind, the call fails with RANGE_OCCUPIED and changes nothing. Details contain part_name, range, and item_range (the first overlapping item, both as `{start_qn, end_qn}`). Ranges that merely touch an existing item are allowed. SIO does not extend, move, merge, or delete items.
- Any valid range is accepted, but the item start becomes the origin of the item's tick grid. Start items on a bar line (or at least on a beat); otherwise ordinary note positions may not be representable (INVALID_POSITION on insertion).
- A note must lie wholly inside one item. Two adjacent items do not make a note across their boundary insertable (NO_TARGET_ITEM); prepare one item for a continuous passage.
- The item is created with source looping disabled, so a human lengthening it later gets empty space instead of a repeated source.
- The addition is one REAPER undo point labeled `SIO: Add item to "<part_name>"`. Neither the default part nor REAPER's UI selection changes.
- While the project is recording, the call fails with TRANSPORT_BUSY. If writes to the project are blocked (WRITE_BLOCKED), it fails with ROLLBACK_FAILED. If REAPER does not produce the requested item, the partial item is removed and the call fails with WRITE_FAILED.
- Every successful call adds an item; a repeated call for the same range fails with RANGE_OCCUPIED rather than duplicating it.

## 7. Request lifecycle and retries

SIO serializes Bridge work. Default waiting budgets are 10 seconds queued and 30 seconds after dispatch. Exceeding a waiting budget returns REQUEST_PENDING with target_request_id in details. This is nonterminal and does not cancel execution.

```json
{
  "request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27",
  "ok": false,
  "error": {
    "code": "REQUEST_PENDING",
    "message": "処理結果はまだ確定していません。状態を確認してください。",
    "details": {
      "target_request_id": "0ef73ab5-46ec-40f6-a987-ea3e315e0e27"
    },
    "retryable": false
  }
}
```

HTTP read timeouts longer than 45 seconds are recommended. Disconnects still happen; closing HTTP does not cancel accepted work. There is no cancellation method.

**BRIDGE_UNAVAILABLE means the request was not executed.** It is returned when the Bridge is not connected at dispatch, or when the Bridge did not start the request within its start deadline (5 seconds) and the Service withdrew it. Once the Bridge has taken a request, a missing result becomes OUTCOME_UNKNOWN instead: when the Bridge restarts or stops, or after it stays unresponsive for 120 seconds.

**What is recorded.** Every admitted request with a well-formed envelope is recorded under its request_id, including get_status, get_request_status, and requests that fail validation immediately. Requests rejected with HTTP 400, 401, 413, or 503 are not recorded; a 503 request may be resent later under the same ID. REQUEST_PENDING, REQUEST_ID_REUSE, and REQUEST_EXPIRED responses are never stored as a request's result.

Within a Service instance and retention window:

- Same request_id and identical parsed JSON content return existing state or terminal response without re-execution. A retry of a pending request joins the wait.
- Object key order/whitespace are irrelevant; array order and omitted versus explicit optional values are significant.
- Changed payload with the same ID gives REQUEST_ID_REUSE.
- Known expired IDs give REQUEST_EXPIRED, not fresh execution.
- Terminal errors are cached too; use a new ID for an intentionally new attempt after resolving a known failure.

On a lost mutation response, poll get_request_status. Do not automatically send the mutation under a new ID. This is particularly important for ordinal targets that may have shifted after the first execution.

After Service/Bridge restart, unknown status, or OUTCOME_UNKNOWN, reacquire current data and reconcile. Do not automatically replay uncertain edits. Exactly-once behavior across crashes/restarts or unlimited time is not guaranteed. Old queued IPC generations are rejected, but a newly submitted HTTP request can still constitute a new operation. Names and ordinals do not make crash replay safe.

Read-only requests may be freshly issued after reconnection; their results describe a new acquisition.

## 8. Errors

| Code | Meaning / recovery | retryable |
|---|---|---|
| INVALID_REQUEST | Wrong field/type, duplicate resolved target, empty/invalid selector; fix request | false |
| UNKNOWN_METHOD / VERSION_MISMATCH | Unsupported method/version; fix client | false |
| UNAUTHORIZED | Missing or wrong token (HTTP 401) | false |
| BRIDGE_UNAVAILABLE | Bridge not connected; the request was not executed | true |
| PROJECT_NOT_FOUND | select_project name has no match | false |
| NO_PROJECT_OPEN | No open project available | false |
| PROJECT_MISMATCH | expect_project_name differs from the resolved project's name; nothing done | false |
| PART_NOT_SELECTED | A default part is required but absent; call select_part or supply part_name | false |
| AMBIGUOUS_PROJECT | Multiple open projects share the name; make names unique | false |
| PART_NOT_FOUND | Refresh names; track absent, renamed, or unnamed | false |
| AMBIGUOUS_PART | Duplicate part names; rename or explicitly request unique parts | false |
| PART_ALREADY_EXISTS | add_track: a track with that name already exists; nothing added | false |
| UNSUPPORTED_PROJECT | Unsupported timeline profile (pickup, partial bar, mid-bar meter change, bar offset) | false |
| UNSUPPORTED_ITEM | Unsupported MIDI structure | false |
| UNSUPPORTED_TAKES | MIDI item has multiple takes; prepare a single-take item | false |
| INVALID_POSITION | Invalid QN, bar, beat, or range; outside the item; not representable in ticks | false |
| NOTE_NOT_FOUND | No current note matches target | false |
| AMBIGUOUS_NOTE | Multiple current notes match; no note modified | false |
| NOTE_MISMATCH | Resolved note differs from `expect`; nothing modified; reacquire | false |
| NO_TARGET_ITEM | No item wholly contains the insertion | false |
| RANGE_OCCUPIED | add_item: the range overlaps an existing item on the track; nothing added | false |
| TRANSPORT_BUSY | Playback, pause, or recording in progress; recording blocks edits, note selection, tab switching by select_project, and project/track/item creation | true |
| REQUEST_PENDING | Poll request status | false |
| OUTCOME_UNKNOWN | Reacquire and reconcile; do not automatically replay mutation | false |
| REQUEST_EXPIRED / REQUEST_ID_REUSE | Request lifecycle violation | false |
| LIMIT_EXCEEDED | A limit in section 2.1 was exceeded; narrow the request | false |
| QUEUE_FULL | Not admitted (HTTP 503); back off | true |
| WRITE_FAILED | Write failed and restoration verified (for add_item: the partial item was removed); reacquire before next edit | false |
| ROLLBACK_FAILED | Restoration uncertain, or writes blocked because of it; human inspection and Bridge restart | false |
| INTERNAL_ERROR | Unexpected failure; treat mutation outcome conservatively | false |

Details always contain the following fields. Additional fields may appear and MUST be tolerated.

| Code | Required details |
|---|---|
| REQUEST_PENDING | target_request_id |
| PROJECT_NOT_FOUND | project_name |
| AMBIGUOUS_PROJECT | project_name, match_count |
| PROJECT_MISMATCH | expected, actual |
| PART_NOT_FOUND | part_name |
| PART_ALREADY_EXISTS | part_name |
| AMBIGUOUS_PART | part_name and match_count (name lookup), or diagnostics (all-part read) |
| UNSUPPORTED_ITEM / UNSUPPORTED_TAKES | part_name, diagnostics (Diagnostic[]) |
| UNSUPPORTED_PROJECT | reason |
| RANGE_OCCUPIED | part_name, range, item_range |
| NOTE_NOT_FOUND | part_name |
| AMBIGUOUS_NOTE | part_name, match_count |
| NOTE_MISMATCH | part_name, mismatched_fields, actual |
| LIMIT_EXCEEDED | limit |
| BRIDGE_UNAVAILABLE | bridge_state, reason |
| OUTCOME_UNKNOWN | reason |
| ROLLBACK_FAILED | project_name; blocked true when rejecting a blocked write |
| INVALID_REQUEST (params validation) | errors: array of `{path, reason}` |

Errors attributable to one patch operation add operation_index; those attributable to one select_notes entry add index. Both are zero-based.

## 9. Project profile and multiple clients

Multiple ordinary, non-overlapping MIDI items per part are supported and flattened into project QN positions. Item boundaries are private except for insertion/extension restrictions. MIDI items must have exactly one take; SIO does not choose an active take among alternatives. Non-MIDI and empty items are ignored with a warning.

The conditions in section 5.3 marked "Part rejected" fail every read and edit of the affected part. Specifics:

- **Looping.** REAPER enables "loop item source" on new MIDI items by default; that setting alone is accepted. A part is rejected only when an item is longer than its source and the source actually repeats.
- **Notes outside an item.** Shortening an item leaves source notes beyond its edges. A note lying wholly outside its item does not sound: it is ignored, receives no note_id, is never modified, and is counted in NOTES_OUTSIDE_ITEM_IGNORED. A note straddling an item edge would be truncated in playback and no longer match its piano-roll values, so the part is rejected with NOTE_OUTSIDE_ITEM.
- **Source offset.** Items with a nonzero source start offset are readable but not editable.
- **Same pitch, same channel.** Overlapping notes of one pitch on one channel cannot be written (section 6.8).

Non-note MIDI events may exist and survive note edits.

Recommended material: ordinary lanes, single-take, non-pooled items; uniquely named part tracks; no pickup bar; no overlapping same-pitch notes on one channel.

Clients are responsible for avoiding conflicting writers. SIO does not track whether a human or another client edited since acquisition, except through the optional `expect` and expect_project_name guards. A queued request resolves its target project (the active tab), default part, and explicit names/targets at **execution time**, not HTTP receipt time. A default part follows the same track across rename; the internal reference is not exposed as a protocol ID.

SIO cannot read or edit a project in an inactive tab without first bringing it to the front. Recording prevents editing, note selection, and tab switching by select_project. Acquisition is allowed during recording; returned data may immediately become outdated as recording or manual editing continues.

REAPER Undo notes: a patch is one Undo point in the edited project. REAPER's Undo restores the previous Undo point, so changes made by scripts that create no Undo point can be reverted together with it.

## 10. Client recipes

### 10.1 Analyze the entire score

1. Call get_status and establish connection.
2. The project in the active tab is the target. To work on another open project, call list_projects and then select_project with an addressable project_name.
3. Call get_score_info to inspect part names and limitations.
4. Call get_notes with no part parameters and no range. This always reads all parts, whatever part is selected.
5. Analyze the returned JSON in the client. SIO performs no musical inference.
6. Report positions and part names. For later highlighting after possible edits, use pitch/start targets and handle no/ambiguous matches.

### 10.2 Evaluate two bars

The client presents a task and waits for the human to submit from the client. Then:

```json
{
  "protocol_version": "2.3",
  "request_id": "d3023d1e-5867-4ecf-8997-7782c67fbcdc",
  "method": "get_notes",
  "params": {
    "expect_project_name": "Harmony.rpp",
    "range": {
      "start": {
        "bar": 1,
        "beat": 1
      },
      "end": {
        "bar": 3,
        "beat": 1
      }
    },
    "relation": "overlaps"
  }
}
```

The client evaluates against its own teaching conditions. A revised submission triggers a new acquisition, without any action in REAPER beyond the human's musical editing.

### 10.3 Guarded ordinal-based correction

Acquire notes, then submit one patch using the returned note_id values, each with `expect` copied from the acquired note (at least pitch and start_qn), plus expect_project_name. If anything changed in between, the patch fails with NOTE_MISMATCH or PROJECT_MISMATCH and modifies nothing; reacquire and decide again. Use dry_run first to see `resolved` when a human should confirm the targets. Reacquire if further ordinal-based actions are needed.

### 10.4 Later coordinate-based correction

After possible manual editing, submit pitch/start targets to explicit or selected parts, accepting that they match current conditions rather than original identity. Add `expect` with duration_qn if a changed length should block the edit. Handle no/ambiguous matches by reacquiring. Do not silently fall back to an old ordinal or nearest note.

### 10.5 Python transport example

```python
import json
import os
import uuid
import requests

config_path = os.path.expanduser("~/Library/Application Support/REAPER/SIO/config.json")
with open(config_path, encoding="utf-8") as fh:
    config = json.load(fh)

payload = {
    "protocol_version": "2.3",
    "request_id": str(uuid.uuid4()),
    "method": "list_projects",
    "params": {},
}
response = requests.post(
    f"http://127.0.0.1:{config['port']}/v2/rpc",
    headers={"Authorization": f"Bearer {config['token']}"},
    json=payload,
    timeout=(3, 60),
)
response.raise_for_status()
body = response.json()
if body["ok"]:
    projects = body["result"]["projects"]
else:
    handle_sio_error(body["error"])  # Application-defined handling.
```

Production clients also require pending-status handling and uncertain-outcome recovery. A requests timeout does not cancel the operation.

### 10.6 Select defaults once, then edit without names

The following are consecutive method/params pairs (each must be wrapped in its own RPC envelope and request_id):

```json
[
  {"method":"select_project","params":{"project_name":"Harmony.rpp"}},
  {"method":"select_part","params":{"part_name":"Cello"}},
  {"method":"get_notes","params":{"selected_part":true}},
  {"method":"apply_patch","params":{"expect_project_name":"Harmony.rpp","operations":[{"op":"update","target":{"note_id":5,"expect":{"pitch":60}},"set":{"pitch":64}}]}}
]
```

This array is documentation shorthand, not a supported HTTP batch request. Wait for each selection to succeed before continuing. If the project is already in the active tab, the select_project step can be omitted.

### 10.7 Prepare a new project

```json
[
  {"method":"create_project","params":{}},
  {"method":"add_track","params":{"part_name":"Soprano","instrument":"ReaSynth"}},
  {"method":"add_track","params":{"part_name":"Bass","instrument":"ReaSynth"}},
  {"method":"add_item","params":{"part_name":"Soprano","range":{"start":{"bar":1,"beat":1},"end":{"bar":9,"beat":1}}}},
  {"method":"add_item","params":{"part_name":"Bass","range":{"start":{"bar":1,"beat":1},"end":{"bar":9,"beat":1}}}},
  {"method":"get_score_info","params":{}}
]
```

As in 10.6, this is shorthand for consecutive requests. Check instrument_loaded and warnings in each add_track result: a missing instrument does not fail the call. The new project is in the active tab, so no select_project is needed (and none is possible until the project is saved under a name). After add_item, bars 1 to 8 of both parts accept notes from apply_patch and from a human in the piano roll.

## 11. Client conformance checklist

- Use protocol 2.3, /v2/rpc, and the token from config.json.
- Remember that the target is the active tab and can change between requests. Name projects and default parts exactly; handle a missing default part and closures.
- Do not read REAPER's track selection as the SIO default part: select_part and add_track set both, but a human changing the track selection does not change the default part. get_notes reads all parts unless told otherwise.
- Use expect_project_name on mutations: the human or another client may switch tabs at any time.
- Support multiple MIDI items per part; no public take/item GUID is needed.
- Respect QN units and denominator-based beats; compare QN values with epsilon_qn.
- Send only tick-representable positions, and send integers without a fraction.
- Keep per-part note ordinals, including gaps in filtered results.
- Guard ordinal targets with `expect`, or use them only in no-intervening-edit workflows.
- Use pitch/start (and channel) for condition-based current-state lookup, with zero/multiple-match handling; write the start as start_qn or as a bar/beat `start`.
- Build patches as change sets and never target one actual note twice.
- Distinguish HTTP failure, application error, and pending work; treat BRIDGE_UNAVAILABLE as not executed and OUTCOME_UNKNOWN as uncertain.
- Never automatically replay uncertain mutations under a new ID.
- Keep analysis, tasks, dialogue, and evaluation outside SIO.

## 12. Version history and implementation status

| Version | Summary |
|---|---|
| 2.0 | Removed server-retained snapshots, revision checks, GUID-based public references, and multiple-take support. |
| 2.1 | Project addressing by shared selection: only select_project takes project_name; select_part supplies a default part. |
| 2.2 | Changes below, following validation on REAPER 7.86. Incompatible with 2.1 requests. |
| 2.3 | The target project is the active tab (see the end of this section). Request shapes are unchanged, but their meaning differs from 2.2. |

Changes in 2.2:

- protocol_version is `2.2`.
- get_notes reads all parts by default; all_parts is removed and selected_part added.
- Unnamed tracks are not parts; UNNAMED_TRACKS_IGNORED replaces EMPTY_PART_NAME.
- Notes wholly outside an item are ignored with NOTES_OUTSIDE_ITEM_IGNORED instead of rejecting the part.
- Looping is rejected only when the source actually repeats.
- Target gains optional `channel` and `expect`; NOTE_MISMATCH is added.
- Project-dependent commands gain optional expect_project_name; PROJECT_MISMATCH is added.
- select_notes, apply_patch, and play_range results include project_name; apply_patch returns `resolved` and `inserted`; refresh_required is removed.
- stop no longer depends on the selection; get_status gains playback_active; the WRITE_BLOCKED diagnostic is added.
- Positions not representable in ticks are INVALID_POSITION; same-pitch same-channel overlaps yield WRITE_FAILED.
- Error code assignments, the HTTP 400/200 boundary, required details, and retryable values are defined; UNAUTHORIZED is added; AMBIGUOUS_TARGET_ITEM is removed.
- The start deadline, the BRIDGE_UNAVAILABLE / OUTCOME_UNKNOWN distinction, the retention budget, and the config.json location are defined.

Added in 2.2 rev. 1: a start position may be given as `start` (a Position, so bar and beat) instead of start_qn in coordinate targets, `expect`, insert notes, and update sets (section 4.5). This is purely additive, existing 2.2 requests remain valid, and protocol_version stays `2.2`.

Added in 2.2 rev. 2: the methods create_project (section 6.12), add_track (section 6.13), and add_item (section 6.14), the errors PART_ALREADY_EXISTS and RANGE_OCCUPIED, and the diagnostic INSTRUMENT_NOT_FOUND. This is purely additive and protocol_version stays `2.2`; a Service older than 0.5.0 answers these methods with UNKNOWN_METHOD.

Changes in 2.3:

- protocol_version is `2.3`; 2.2 requests are answered with VERSION_MISMATCH.
- SIO no longer keeps a project selection. Project-dependent commands act on the project in REAPER's active tab, including after a human switches tabs (section 4.1). The sole-open-project rule is gone.
- select_project brings the named project's tab to the front, stopping SIO-managed playback first; it fails with TRANSPORT_BUSY during recording.
- One default part is remembered per open project instead of being cleared when the project changes (section 4.2).
- select_part also makes the part's track the only selected track in REAPER. add_track makes the track it adds the default part in the same way and reports selected_part_name. Track selection made by a human in REAPER still does not change the default part.
- get_status reports active_project_name instead of selected_project_name; list_projects entries no longer have `selected`.
- PROJECT_NOT_SELECTED and PROJECT_NOT_ACTIVE are removed. play_range needs no separate activation.
- Projects in inactive tabs can no longer be read or edited without bringing them to the front.

Service/Bridge 0.6.0 implements all of the above. It passed the Service unit tests and the acceptance tests on a real REAPER 7.86 (Windows 11).

Not yet validated on a real installation: pooled MIDI source detection; takes that ignore project tempo; release of playback management on position jump, tab change, or recording (release on human stop is validated); rejection of edits during recording; UNSUPPORTED_PROJECT for pickups, partial bars, and mid-bar meter changes; UI stall time at 100,000 notes; select_project, create_project, add_track, and add_item during recording; the WRITE_FAILED path of add_item; add_track with third-party instrument plug-ins (validated with the bundled ReaSynth and ReaEQ only); create_project with a default project template configured; the automated acceptance tests on macOS (checked manually only); Linux.
