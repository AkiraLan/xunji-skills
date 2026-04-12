---
name: xunji-reader
description: Read 训记 (Xunji) training records from the private `/api_trains_for_llm` endpoint with built-in local caching. Use this whenever the user wants to read, inspect, summarize, analyze, or reuse their Xunji workout data by date, especially when the request involves 训记, 训记app, Xunji, training logs, workout records, or per-day exercise history.
---

# Xunji Training Reader

Use the bundled helper instead of calling the API manually. It already handles:

- Bearer-token authentication
- `POST /api_trains_for_llm`
- gzip JSON decoding
- parsing the `res` array
- persistent per-date local caching so the same date is not requested again unless the user explicitly asks to refresh

## Primary command

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02
```

## Output behavior

Always reply in the same language the user is currently using. If the user writes in Chinese, reply in Chinese. If the user writes in English, reply in English. Only switch languages if the user explicitly asks for that.

Default output is JSON with:

- `datestr`
- `cached`
- `fetched_at`
- `count`
- `res`

Use line output when the user wants the raw training text lines directly:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --format lines
```

## Action library artifact

The reader also maintains the local action library at:

- `~/.cache/opencode/xunji-reader/action-library.json`

Treat this file as the primary cached action index for downstream Xunji skills.

- It is rebuilt from cached historical training records.
- It is the fastest local source for checking whether an action name already exists.
- When the user asks to find, align, or validate动作 names, prefer checking this file first instead of scanning many day-level training files manually.
- If the file is missing or clearly incomplete, use the reader helper to refresh history and rebuild it before doing deeper action-name analysis.

## Refresh behavior

The cache is persistent by default. Do **not** re-request the same date unless the user explicitly wants fresh data.

For the same training date, do **not** send another read request within 90 seconds. Within that window, reuse the existing cached result instead of repeatedly hitting the API for the same day.

When a refresh is explicitly requested:

```bash
python3 "$CLAUDE_SKILL_DIR/scripts/fetch_xunji_trains.py" --date 2026-04-02 --refresh
```

## Important handling rules

1. Always pass the date as `YYYY-MM-DD`.
2. Preserve each returned training line exactly as provided when downstream work may need to write back later.
3. Keep `id:...` and any `train_time:...` token unchanged.
4. Prefer cached data for repeated analysis of the same date.
5. If the API returns an auth or frequency error, surface that clearly instead of guessing.
6. A read response with `res: []` does **not** prove that the user had no unfinished or unchecked training items that day.
7. The server may omit unchecked / uncompleted training history from the read endpoint, so "read empty" and "write-side still says the day is full" can coexist.
8. Newly created plan records that were not checked in the app may also be absent from read results, so read-after-write is not a reliable confirmation path for new creates.
9. Use read data as comparison context for modifying existing records, not as proof that a new create definitely succeeded or definitely failed.

## Configuration

The bundled helper reads credentials from:

1. `XUNJI_API_KEY` environment variable

The API endpoint is hardcoded in the helper. It does not depend on `config.json`.

Keep secrets out of normal responses unless the user explicitly asks to inspect configuration.
