---
name: xunji
description: Read, analyze, create, and update 训记 (Xunji) training records through the private `/api_trains_for_llm` and `/api_upsert_trains_for_llm` endpoints with local caching, action-name alignment, dry-run validation, and safe upsert handling.
---

# Xunji Training Skill

Use the bundled helpers instead of calling the API manually. This single skill covers both read and write workflows for 训记 training data.

It handles:

- Bearer-token authentication from `XUNJI_API_KEY`
- `POST /api_trains_for_llm` for reading records
- `POST /api_upsert_trains_for_llm` for writing records
- gzip JSON decoding and `res` array parsing
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

Use line output when the user wants raw training text directly:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --format lines
```

Refresh only when the user explicitly asks for fresh data:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --refresh
```

## Read Rules

1. Always pass read dates as `YYYY-MM-DD`.
2. Send the API key in `Authorization: Bearer ...`; the helper reads it from `XUNJI_API_KEY`.
3. Preserve returned training lines exactly when they may later be written back.
4. Keep `id:...` and any `train_time:...` token unchanged.
5. Treat `id:...` as the training record local ID for later upsert writes.
6. Prefer cached data for repeated analysis of the same date.
7. Do not retry the same training day with compact `YYMMDD` query syntax; the read endpoint request should use `YYYY-MM-DD`.
8. If the API returns an auth or frequency error such as `apikey missing`, `apikey invalid`, or `too frequent, retry after 90s`, surface it clearly.
9. A read response with `res: []` does not prove there are no unfinished or unchecked training items that day.
10. Use read data as comparison context for modifying existing records.

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
- action names cannot be aligned safely, even after refreshing the action library

Mandatory write rules:

1. Every outgoing line in `res` must belong to the same target day.
2. When updating existing records, preserve exported `id:...` exactly.
3. If a line has `train_time:...`, preserve it exactly.
4. If cached data for the same `id:...` contains `train_time:...`, preserve that exact value unless you intentionally pass `--allow-train-time-drop`.
5. If any non-rest-day line omits `id:...`, require explicit create intent with `--allow-new-records`.
6. The upsert endpoint writes by `id:...`; old same-day records omitted from the request are not automatically deleted.
7. For a new plan, combine same-plan actions into one top-level training line instead of splitting them across multiple new records.
8. If one weight/reps payload is repeated across multiple sets, expand it into explicit `1组 ... 2组 ...` segments instead of compressed shorthand like `60kg,3组`.
9. For ordinary strength actions, every explicit set such as `1组` must include at least one valid weight, reps, or time token.
10. Treat the upsert response `res` as the final standardized result and cache it locally.
11. If the server acknowledges success with empty `res`, use the normalized submitted lines as the immediate local result instead of forcing read-back verification.
12. Surface auth, membership, validation, and malformed API response errors clearly.

## Training Line Format

Examples:

```text
2026-04-02,id:123456,胸部训练,train_time:1744010000000-1744013600000,状态不错,1.卧推,1组,60kg,10次,2组,60kg,8次
2026-04-02,有氧,1.跑步,5km,300kcal,time:1800s,140bpm
2026-04-02,休息日
```

Dates may be `YYYY-MM-DD` or compact `YYMMDD` in write lines, but use `YYYY-MM-DD` for the `--date` argument.

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
- `res`

Use `--format lines` when the user wants line output.

## Configuration

The helpers read credentials from:

1. `XUNJI_API_KEY` environment variable

The API endpoints are hardcoded. Do not put API keys in this repository or normal responses unless the user explicitly asks to inspect configuration.

Always reply in the same language the user is currently using.
