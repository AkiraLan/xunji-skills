---
name: xunji
description: Read, analyze, create, and update 训记 (Xunji) training records through the public Open API v2 endpoints `/api_trains_for_llm_v2` and `/api_upsert_trains_for_llm_v2` with local caching, action-name alignment, dry-run validation, and safe upsert handling.
---

# Xunji Training Skill

Use the bundled helpers instead of calling the API manually. This single skill covers both read and write workflows for 训记 training data and follows the official `train_open_api_v2` schema.

It handles:

- Bearer-token authentication from `XUNJI_API_KEY` (header `Authorization: Bearer ...`; also accepts `x-api-key`)
- `POST /api_trains_for_llm_v2` for reading records (lightweight by default; pass `--full` for `include_full_data: true`)
- `POST /api_upsert_trains_for_llm_v2` for writing records with `client_request_id` and optional server-side `dry_run`
- gzip JSON decoding and `res.trains` array parsing
- persistent per-date local caching
- action library maintenance for safer write-back
- same-day validation, normalization, and dry-run checks before writes

## Choose The Workflow

Use the read workflow when the user wants to inspect, summarize, analyze, compare, or reuse existing Xunji workout data.

Use the write workflow only when the user clearly wants to save, create, update, or write finalized training content back into Xunji.

Do not use the write helper for brainstorming, formatting-only, or read-only analysis that stops short of actual persistence.

## Read Command

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02
```

Add `--full` only when the user needs unchecked sets, per-set RPE, per-set remarks, or movement remarks (sends `include_full_data: true`):

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --full
```

Use line output when the user wants raw training text directly:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --format lines
```

Refresh only when the user explicitly asks for fresh data:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --refresh
```

## Read Rules

1. Always pass read dates as `YYYY-MM-DD`. Compact `YYMMDD` is no longer accepted.
2. Send the API key in `Authorization: Bearer ...`; the helper reads it from `XUNJI_API_KEY`.
3. Every request body includes `schema_version: "train_open_api_v2"`.
4. Default lightweight mode returns nearly v1-equivalent fields; use `--full` for unchecked sets, per-set RPE, per-set remarks, and movement remarks.
5. Preserve returned training data exactly when it may later be written back.
6. Keep `localid` (`id:...` in line format) and `start`/`end` (`train_time:...`) tokens unchanged.
7. Treat `localid` as the training record's identifier for later upsert writes.
8. Cardio, timed, and Tabata movements expose per-set `metrics` (distance/kcal/bpm/steps).
9. Prefer cached data for repeated analysis of the same date; the API rate-limits one read per training day to roughly once per 90 seconds.
10. If the API returns an auth, membership, or frequency error such as `apikey missing`, `apikey invalid`, `仅VIP可用`, or `too frequent, retry after 90s`, surface it clearly and respect the retry hint.
11. A read response with empty `res.trains` does not prove there are no unfinished or unchecked training items that day.
12. Use read data as comparison context for modifying existing records.

## Action Library

The read helper maintains the action library at:

- `~/.cache/opencode/xunji-reader/action-library.json`

This path is kept for compatibility with earlier versions of the split reader/writer skills.

Use this file as the first lookup source when checking whether a user-entered action name already exists. If it is missing or incomplete, run the read helper so it can refresh history and rebuild the library before deeper action-name analysis.

## Write Commands

For clearly new records with explicit lines:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --allow-new-records \
  --line '2026-04-02,胸部训练,状态不错,1.卧推,1组,60kg,10次,2组,60kg,8次'
```

With a JSON file containing either a raw array or an object with `res`:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --res-file /tmp/xunji-res.json
```

Before a real write, prefer validating the payload without sending it:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --res-file /tmp/xunji-res.json \
  --dry-run
```

## Write Rules

Stop and ask the user before writing whenever the outgoing structure is not uniquely determined.

Clarify before write when:

- the user has not clearly said to save, create, update, or write back into Xunji
- the target date is missing or unclear
- it is unclear whether this modifies an existing record or creates a new one
- an input token is ambiguous compact shorthand such as `10x3`
- a numeric token could mean weight, reps, or sets
- action names cannot be aligned safely against the canonical Chinese names from `https://github.com/Foveluy/movements` (the Xunji-movements reference) or the local action library

Mandatory write rules:

1. Every train in the outgoing `res` (or `res.trains`) must belong to the same target day.
2. A single call may carry at most 4 trains; each train may carry at most 15 movements; each movement may carry at most 20 sets. The server rejects anything larger.
3. Always send `schema_version: "train_open_api_v2"` and a fresh `client_request_id`.
4. When updating existing records, preserve `localid`, `start`, and `end` exactly. (In line format these surface as `id:...` and `train_time:start-end`.)
5. If cached data for the same `localid` contains `train_time:...`, preserve that exact value unless you intentionally pass `--allow-train-time-drop`.
6. If any non-rest-day train omits `localid`, require explicit create intent with `--allow-new-records`; the server will then assign a new id.
7. The upsert endpoint writes by `localid`; old same-day records omitted from the request are not automatically deleted.
8. For a new plan, combine same-plan actions into one top-level training instead of splitting them across multiple new records.
9. If one weight/reps payload is repeated across multiple sets, expand it into explicit `1组 ... 2组 ...` segments instead of compressed shorthand like `60kg,3组`.
10. For ordinary strength sets, every set must include at least one of weight (`60kg`), reps (`10次`), time (`time:60s`), or `自重` (selfWeight).
11. Send only Chinese movement `name` values. Never send the internal `key`. When unsure, look the name up against the canonical Xunji-movements table before writing.
12. Unfinished sets must be sent with `done: false`. Do not silently drop unchecked sets that were read in `--full` mode.
13. Cardio, timed, and Tabata sets keep their indicators inside `sets[].metrics` (distance/kcal/bpm/steps).
14. Prefer the local `--dry-run` flag for validation: it never contacts the server. The `--server-dry-run` flag still sends `dry_run: true` in the body, but live testing on 2026-05-19 showed the Xunji v2 server currently persists those calls anyway, so treat `--server-dry-run` as equivalent to a real write until upstream confirms `dry_run` is honored.
15. Treat the upsert response `res.trains` (server-normalized form) as the canonical final result and overwrite the local cache with it.
16. If the server acknowledges success with empty `res.trains`, use the normalized submitted trains as the immediate local result instead of forcing read-back verification.
17. Surface auth, membership (`仅VIP可用`), validation, and malformed API response errors clearly. On `too frequent` errors, respect the suggested retry interval (about 90 seconds per training day).

## Training Line Format

Examples:

```text
2026-04-02,id:123456,胸部训练,train_time:1744010000000-1744013600000,状态不错,1.卧推,1组,60kg,10次,2组,60kg,8次
2026-04-02,有氧,1.跑步,5km,300kcal,time:1800s,140bpm
2026-04-02,休息日
```

Use `YYYY-MM-DD` everywhere — compact `YYMMDD` is no longer accepted in either the `--date` argument or the leading segment of a line. The helpers convert these lines into the v2 structured payload (`datestr`, `localid`, `title`, `start`, `end`, `movements[].sets[]` with `metrics` for cardio) before sending.

## Normalization Rules

Prefer exact structured values such as:

- `1组`
- `60kg`
- `10次`
- `time:60s`
- cardio metrics such as `5km`, `300kcal`, `140bpm`

Key behavior:

- `12-15次` normalizes to `15次`.
- `20-25kg` normalizes to `25kg`.
- `60kg,3组,10次` expands to explicit per-set segments.
- `热身` or `热身组` is converted into ordinary writable set structure when safe.
- `10x3` is ambiguous and must be clarified before writing.
- Discarded content is not written into remarks unless the user explicitly asks for that and `--preserve-invalid-in-remarks` is used.

## Output

Default helper output is JSON with:

- `datestr`
- `cached`
- `fetched_at`
- `count`
- `res` — CSV-style training lines reconstructed from the v2 train objects
- `trains` — original v2 train objects returned by the API, for clients that want structured data

Use `--format lines` when the user wants line output. Local cache stores both `res` and `trains` so callers may consume either shape.

## Action Name Reference

When a Chinese movement `name` is not in the local action library, fall back to the canonical Xunji movement reference at `https://github.com/Foveluy/Xunji-movements` and pick the closest Chinese name from that table before retrying the write. Never send a `key` field — only the Chinese `name` is allowed.

## Configuration

The helpers read credentials from:

1. `XUNJI_API_KEY` environment variable

The API endpoints are hardcoded. Do not put API keys in this repository or normal responses unless the user explicitly asks to inspect configuration.

Always reply in the same language the user is currently using.
