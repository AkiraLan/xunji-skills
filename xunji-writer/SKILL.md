---
name: xunji-writer
description: Write finalized 训记 (Xunji) training records back to the private `/api_upsert_trains_for_llm` endpoint after the user has clearly decided to save, create, or update records in Xunji. Use this only for real write-back intent—such as saving a new plan, modifying an existing record, or persisting normalized workout entries—and not for read-only analysis, history lookup, or formatting help that stops short of an actual write.
---

# Xunji Writer

Use the bundled helper instead of calling the API manually. It already handles:

- Bearer-token authentication
- `POST /api_upsert_trains_for_llm`
- gzip JSON decoding
- same-day validation across the full `res` array
- normalization of partially structured plan/result lines before writing
- persistent per-date local cache updates using the upsert response itself as the source of truth

## Purpose and boundary

This skill is for writing finalized training records back into 训记.

- Use it when the user wants data persisted into Xunji.
- Do **not** use it as a general training-plan formatter, notes cleaner, or read-only analysis tool.
- If the user only wants to inspect, summarize, compare, or fetch training history, use the reader flow instead.

## When to use this skill

Use this skill when the user clearly wants to do one of these things:

- save a new training plan into Xunji
- update an existing training record
- create a rest day or a new training record in Xunji
- take already-decided workout content and write it back to the app

## When not to use this skill

Do **not** use this skill when the user is only trying to:

- read or inspect existing records
- summarize or analyze past training
- brainstorm or draft a plan without saving it yet
- normalize free text for discussion only, without actual write intent

If the task stops short of a real write-back, stay out of the write flow.

## Clarification required before write

Stop and ask the user before writing whenever the outgoing structure is not uniquely determined.

You must clarify before write when any of these are true:

- the user has not clearly said they want to save, create, update, or write back into Xunji
- the target date is missing or unclear
- it is unclear whether this is a modification of an existing record or a brand-new create
- an input token is ambiguous compact shorthand such as `10x3`
- a numeric token could mean weight, reps, or sets and the intended structure is not explicit
- action names cannot be aligned safely, even after refreshing the action library

General rule: if a structured interpretation would require guessing, do **not** guess. Ask first, then write.

## Reader dependency and action-library prerequisite

Before writing, treat the local action library as a prerequisite for action-name alignment.

The action library lives at:

- `~/.cache/opencode/xunji-reader/action-library.json`

The **reader skill** owns the historical fetch flow and maintains this artifact. The writer skill only consumes it as a prerequisite before write.

- Use `action-library.json` as the first lookup source when checking whether a user-entered 动作 name already exists.
- If the local action library is missing or incomplete, call the **read skill** first so it can refresh history and rebuild the library.
- If action names still cannot be aligned after refresh, stop the write and ask the user whether to extend the fetch window further, or rename those actions in the app first.

Do not pretend the writer can safely invent or guess canonical action names on its own.

## Two write workflows

Treat modify-existing writes and create-new writes as different workflows.

### Modify existing record

Use this path when the user is changing an existing training record.

- Start from exported `res` lines whenever possible.
- Preserve existing `id:...` exactly.
- Preserve existing `train_time:...` exactly.
- Use read-side data as comparison context, not as a license to freely rewrite structure.
- Make the smallest safe structural change needed for the requested update.

### Create new record

Use this path when the user is intentionally creating a new training record or rest day.

- Normalize the plan into finalized structured entries before writing.
- Within one plan, different actions must be written into the **same training line**, not split into multiple top-level training records.
- If the user originally split one plan across multiple new non-`id:...` lines, merge those same-plan actions into one line before writing.
- Do **not** rely on read-after-write confirmation for new creates. Unchecked or unticked records may be hidden from the read API.

## Hard rules that always apply

These rules are mandatory regardless of workflow.

1. Every outgoing line in `res` must belong to the same target day.
2. When updating existing records, preserve exported `id:...` exactly.
3. If a line already has `train_time:...`, preserve it exactly.
4. If a cached line for the same `id:...` contains `train_time:...`, preserve that exact value unless you intentionally pass `--allow-train-time-drop`.
5. If any non-rest-day line omits `id:...`, require explicit create intent such as `--allow-new-records` before writing.
6. For a newly created plan, combine same-plan actions into one top-level training line instead of splitting them across multiple new records.
7. If one weight/reps payload is repeated across multiple sets, expand it into explicit `1组 ... 2组 ...` segments instead of writing compressed shorthand like `60kg,3组`.
8. New creates do **not** use re-read confirmation as a success check.
9. A normal successful write may treat the upsert response as the best immediate truth source when it includes a valid integer `count` plus a valid non-empty `res` list.
10. If the server returns `{"res":[]}` after write, treat that as a successful acknowledgment rather than a failure.
11. When write success is acknowledged with empty `res`, use the normalized submitted lines as the immediate local result instead of forcing read-back verification.
12. If the server returns `too frequent, retry after Ns`, wait for that window, then retry carefully. Do not spam repeated writes.
13. Surface auth, membership, validation, and malformed API response errors clearly instead of guessing.

## Normalization rules

The write API is for finalized structured training entries, not raw free-text plans. Normalize first, then write.

Prefer exact structured values such as:

- `1组`
- `60kg`
- `10次`
- `time:60s`
- cardio-style direct metrics such as `5km`, `300kcal`, `140bpm`

Apply these normalization rules:

1. If a token can safely stay in a structured slot, keep it structured.
2. If a token contains a valid structured core plus invalid attachment text, keep the valid structured core, warn about the invalid remainder, and discard the remainder.
3. If weight or reps are given as a numeric range, use the **upper bound** as the structured value.
4. If one payload is repeated across multiple sets, expand it into explicit per-set structure before writing.
5. Warm-up markers such as `热身` / `热身组` are not discardable noise. Convert them into ordinary writable set structure. If no explicit set count exists, normalize them as `1组`.
6. If a token cannot safely fit into structured slots, do **not** force it into weight/reps/set fields.
7. Do **not** write discarded content into remarks or notes.
8. If a token is ambiguous compact shorthand such as `10x3`, stop and ask instead of discarding or guessing.

General rule for weights: if a would-be weight token contains anything beyond **number + legal unit**, only the pure weight stays structured. The extra attachment text is discarded after warning.

General rule for ranges: if weight or reps are given as a numeric range, use the **upper bound** as the structured value. Only extra non-range attachment text is considered invalid and discarded after warning.

## Illustrative examples

Use these as examples of the rules above. The examples do not override the hard rules.

- `20kg/手` -> keep `20kg`, discard `/手` after warning
- `20kg 热身` -> keep it as a formal set entry, typically normalize to `1组,20kg`
- `热身组,20kg,12次` -> normalize to a normal writable set such as `1组,20kg,12次`
- `12-15次` -> normalize to `15次`
- `20-25kg` -> normalize to `25kg`
- `60kg,3组,10次` -> expand to `1组,60kg,10次,2组,60kg,10次,3组,60kg,10次`
- `10x3` -> do not discard or guess; ask the user what each number means and rewrite it with explicit units first, such as `10kg,3次` or `10次,3组`
- `RPE8` or `工作组` -> discard after warning

## Server-side constraints

Preserve these server constraints exactly:

- `res` must be a non-empty string array
- all lines in one write must belong to the same date
- one write may contain at most `12` lines
- each line may be at most `1500` characters
- one calendar day may contain at most **4** training records

If the server returns `too many trains in one day, max 4`:

- tell the user to clean up or handle it in the app
- do **not** silently compress, merge, or rewrite unrelated records just to bypass the limit

## Primary commands

For clearly new records with explicit lines:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --allow-new-records \
  --line '2026-04-02,胸部训练,状态不错,1.卧推,1组,60kg,10次,2组,60kg,8次' \
  --line '2026-04-02,有氧,2.跑步,5km,300kcal,time:1800s,140bpm'
```

With a JSON file containing either a raw array or an object with `res`:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --res-file /tmp/xunji-res.json
```

## Dry-run validation

Before a real write, prefer validating the payload without sending it:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" \
  --date 2026-04-02 \
  --res-file /tmp/xunji-res.json \
  --dry-run
```

Use dry-run whenever the user is creating new records, normalizing messy plan text, or asking for a cautious write path.

## Output behavior

Always reply in the same language the user is currently using. If the user writes in Chinese, reply in Chinese. If the user writes in English, reply in English. Only switch languages if the user explicitly asks for that.

Default output is JSON with:

- `datestr`
- `cached`
- `fetched_at`
- `count`
- `res`

Use line output when the user wants the normalized training text lines directly:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/upsert_xunji_trains.py" --date 2026-04-02 --res-file /tmp/xunji-res.json --format lines
```

## Success and failure interpretation

Interpret write results carefully:

- `response_res_empty: true` or an empty returned `res` is not failure by itself.
- After a normal successful write, refresh the local cache from the upsert response `res` when available.
- If the server acknowledges success with empty `res`, keep the normalized submitted lines as the immediate cached result.
- For new plan creation, do **not** perform read-back verification after write. The server may acknowledge success with empty `res`, and unchecked records may also be hidden from the read API.

## Configuration

The bundled helper reads credentials from:

1. `XUNJI_API_KEY` environment variable

The API endpoint is hardcoded in the helper. It does not depend on `config.json`.

Keep secrets out of normal responses unless the user explicitly asks to inspect configuration.
