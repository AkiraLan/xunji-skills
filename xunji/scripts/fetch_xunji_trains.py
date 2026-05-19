#!/usr/bin/env python3

import argparse
import gzip
import json
import os
import re
import ssl
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATE_FORMAT = "%Y-%m-%d"
BASE_URL = "https://trains.xunjiapp.cn"
API_PATH = "/api_trains_for_llm_v2"
SCHEMA_VERSION = "train_open_api_v2"
ACTION_START_RE = re.compile(r"^\d+\.(?!\d+(?:\.\d+)?(?:km|kg)$)(.+)$")
ACTION_LIBRARY_HISTORY_DAYS = 92
ACTION_LIBRARY_RETRY_DELAY_SECONDS = 95


def format_numeric(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _nonempty_numeric(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def v2_set_to_tokens(set_obj: dict) -> list[str]:
    tokens: list[str] = []
    raw_weight = set_obj.get("weight")
    raw_weight_kg = set_obj.get("weight_kg")
    weight_value = _nonempty_numeric(raw_weight)
    if weight_value is None:
        weight_value = _nonempty_numeric(raw_weight_kg)
    if weight_value is not None and weight_value > 0:
        unit = (set_obj.get("unit") or "").strip() or (
            "kg" if raw_weight_kg is not None else "kg"
        )
        tokens.append(f"{format_numeric(weight_value)}{unit}")
    reps_value = _nonempty_numeric(set_obj.get("reps"))
    if reps_value is not None and reps_value > 0:
        tokens.append(f"{format_numeric(reps_value)}次")
    duration_value = _nonempty_numeric(set_obj.get("duration_s"))
    if duration_value is None:
        duration_value = _nonempty_numeric(set_obj.get("time"))
    if duration_value is not None and duration_value > 0:
        tokens.append(f"time:{format_numeric(duration_value)}s")
    if set_obj.get("selfWeight"):
        tokens.append("自重")
    metrics = set_obj.get("metrics") or {}
    distance = metrics.get("distance")
    if distance is not None:
        unit = metrics.get("distance_unit", "m")
        try:
            d_val = float(distance)
            if unit == "m" and d_val >= 1000 and d_val % 1000 == 0:
                tokens.append(f"{int(d_val / 1000)}km")
            elif unit == "m":
                tokens.append(f"{format_numeric(d_val)}m")
            elif unit == "km":
                tokens.append(f"{format_numeric(d_val)}km")
            else:
                tokens.append(f"{format_numeric(d_val)}{unit}")
        except (TypeError, ValueError):
            tokens.append(str(distance))
    kcal = metrics.get("kcal")
    if kcal is not None:
        tokens.append(f"{format_numeric(kcal)}kcal")
    bpm = metrics.get("bpm")
    if bpm is not None:
        tokens.append(f"{format_numeric(bpm)}bpm")
    steps = metrics.get("steps")
    if steps is not None:
        tokens.append(f"{format_numeric(steps)}steps")
    return tokens


def v2_train_to_csv_line(train: dict) -> str:
    datestr = str(train.get("datestr") or "")
    if train.get("rest_day"):
        return f"{datestr},休息日"
    movements = train.get("movements") or []
    if not movements and (train.get("title") in {None, "", "休息日"}):
        return f"{datestr},休息日"
    segments: list[str] = [datestr]
    localid = train.get("localid")
    if localid is not None:
        segments.append(f"id:{localid}")
    segments.append(str(train.get("title") or ""))
    start = train.get("start")
    end = train.get("end")
    if (
        start is not None
        and end is not None
        and not (isinstance(start, int) and start <= 0)
        and not (isinstance(end, int) and end <= 0)
    ):
        segments.append(f"train_time:{start}-{end}")
    remarks = train.get("remarks")
    if remarks:
        for note in str(remarks).split(","):
            stripped = note.strip()
            if stripped:
                segments.append(stripped)
    for idx, movement in enumerate(movements, start=1):
        name = str(movement.get("name") or "")
        segments.append(f"{idx}.{name}")
        sets = movement.get("sets") or []
        if not sets:
            continue
        if len(sets) == 1:
            segments.extend(v2_set_to_tokens(sets[0]))
            continue
        for set_idx, set_obj in enumerate(sets, start=1):
            segments.append(f"{set_idx}组")
            segments.extend(v2_set_to_tokens(set_obj))
    return ",".join(seg for seg in segments if seg is not None and seg != "")


def extract_trains_from_payload(payload_res: object) -> list[dict]:
    trains: list[dict] = []
    if isinstance(payload_res, dict):
        candidate = payload_res.get("trains")
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    trains.append(item)
    elif isinstance(payload_res, list):
        for item in payload_res:
            if isinstance(item, dict):
                trains.append(item)
    return trains


def resolve_api_key() -> str:
    api_key = os.getenv("XUNJI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing API key. Set XUNJI_API_KEY.")
    return api_key


def validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, DATE_FORMAT)
    except ValueError as exc:
        raise RuntimeError(f"Invalid date '{date_str}'. Expected YYYY-MM-DD") from exc
    return date_str


def default_cache_dir() -> Path:
    root = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "opencode" / "xunji-reader"


def cache_file_for(date_str: str, cache_dir: Path) -> Path:
    return cache_dir / "trains" / f"{date_str}.json"


def action_library_file(cache_dir: Path) -> Path:
    return cache_dir / "action-library.json"


def action_library_retry_file(cache_dir: Path) -> Path:
    return cache_dir / "action-library-retry.json"


def read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["cached"] = True
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def normalize_action_name(name: str) -> str:
    return "".join(name.split()).casefold()


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        token = item.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def extract_action_names_from_res_lines(res_lines: list[str]) -> list[str]:
    actions: list[str] = []
    for line in res_lines:
        if not isinstance(line, str):
            continue
        for segment in line.split(","):
            match = ACTION_START_RE.fullmatch(segment.strip())
            if not match:
                continue
            action_name = match.group(1).strip()
            if action_name:
                actions.append(action_name)
    return dedupe_preserve_order(actions)


def build_action_library_payload(cache_dir: Path) -> dict:
    trains_dir = cache_dir / "trains"
    action_names: list[str] = []
    if trains_dir.exists():
        for path in sorted(trains_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            res_lines = payload.get("res")
            if not isinstance(res_lines, list):
                continue
            action_names.extend(extract_action_names_from_res_lines(res_lines))

    actions = dedupe_preserve_order(action_names)
    aliases = {normalize_action_name(action): action for action in actions}
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(actions),
        "actions": actions,
        "aliases": aliases,
    }


def refresh_action_library(cache_dir: Path) -> None:
    atomic_write_json(
        action_library_file(cache_dir), build_action_library_payload(cache_dir)
    )


def is_retryable_rate_limit_error(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.casefold()
    return "too frequent" in lowered and "retry after 90s" in lowered


def read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return None
    return payload


def retryable_cache_payload(path: Path) -> dict | None:
    payload = read_json_file(path)
    if payload is None:
        return None
    fallback_error = payload.get("fallback_error")
    if isinstance(fallback_error, str) and is_retryable_rate_limit_error(
        fallback_error
    ):
        return payload
    return None


def write_action_library_retry_state(
    cache_dir: Path,
    failed_dates: list[str],
    retry_scheduled_at: str | None = None,
    retried_dates: list[str] | None = None,
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "failed_dates": failed_dates,
        "failed_count": len(failed_dates),
    }
    if retry_scheduled_at is not None:
        payload["retry_scheduled_at"] = retry_scheduled_at
    if retried_dates:
        payload["retried_dates"] = retried_dates
        payload["retried_count"] = len(retried_dates)
    atomic_write_json(action_library_retry_file(cache_dir), payload)


def action_library_history_dates(target_date: str) -> list[str]:
    target = datetime.strptime(target_date, DATE_FORMAT)
    return [
        (target - timedelta(days=offset)).strftime(DATE_FORMAT)
        for offset in range(ACTION_LIBRARY_HISTORY_DAYS - 1, -1, -1)
    ]


def ensure_action_library_history_cache(
    target_date: str,
    cache_dir: Path,
    api_key: str | None,
    target_payload: dict | None = None,
) -> None:
    target_cache_path = cache_file_for(target_date, cache_dir)
    if target_payload is not None:
        atomic_write_json(target_cache_path, target_payload)

    failed_dates: list[str] = []
    for history_date in action_library_history_dates(target_date):
        cache_path = cache_file_for(history_date, cache_dir)
        if cache_path.exists() and retryable_cache_payload(cache_path) is None:
            continue
        if api_key is None:
            continue
        history_payload = fetch_remote(history_date, api_key)
        atomic_write_json(cache_path, history_payload)

        fallback_error = history_payload.get("fallback_error")
        if isinstance(fallback_error, str) and is_retryable_rate_limit_error(
            fallback_error
        ):
            failed_dates.append(history_date)

    if failed_dates and api_key is not None:
        retry_scheduled_at = datetime.now(timezone.utc).isoformat()
        write_action_library_retry_state(
            cache_dir, failed_dates, retry_scheduled_at=retry_scheduled_at
        )
        time.sleep(ACTION_LIBRARY_RETRY_DELAY_SECONDS)

        retried_dates: list[str] = []
        still_failed_dates: list[str] = []
        for failed_date in failed_dates:
            retry_payload = fetch_remote(failed_date, api_key)
            atomic_write_json(cache_file_for(failed_date, cache_dir), retry_payload)
            retried_dates.append(failed_date)
            fallback_error = retry_payload.get("fallback_error")
            if isinstance(fallback_error, str) and is_retryable_rate_limit_error(
                fallback_error
            ):
                still_failed_dates.append(failed_date)

        write_action_library_retry_state(
            cache_dir,
            still_failed_dates,
            retry_scheduled_at=retry_scheduled_at,
            retried_dates=retried_dates,
        )


def decode_body(raw: bytes, encoding: str | None) -> bytes:
    if encoding and "gzip" in encoding.lower():
        return gzip.decompress(raw)
    return raw


def build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_once(
    query_date: str, api_key: str, *, include_full_data: bool = False
) -> list[dict]:
    url = f"{BASE_URL}{API_PATH}"
    body = {
        "schema_version": SCHEMA_VERSION,
        "datestr": query_date,
        "include_full_data": bool(include_full_data),
    }
    request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=request_body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )

    try:
        with urlopen(request, timeout=30, context=build_ssl_context()) as response:
            raw = response.read()
            decoded = decode_body(raw, response.headers.get("Content-Encoding"))
            decoded_text = decoded.decode("utf-8")
            payload = json.loads(decoded_text)
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to decode API response as JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Unexpected API response format for datestr={query_date}: {decoded_text}"
        )

    if payload.get("success") is False:
        raise RuntimeError(
            f"API returned success=false for datestr={query_date}: {decoded_text}"
        )

    res = payload.get("res")
    trains = extract_trains_from_payload(res)
    return trains


def fetch_remote(
    date_str: str, api_key: str, *, include_full_data: bool = False
) -> dict:
    trains = fetch_once(date_str, api_key, include_full_data=include_full_data)
    lines = [v2_train_to_csv_line(train) for train in trains]

    payload = {
        "datestr": date_str,
        "queried_datestr": date_str,
        "cached": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "include_full_data": bool(include_full_data),
        "count": len(trains),
        "res": lines,
        "trains": trains,
    }
    return payload


def emit(payload: dict, output_format: str) -> None:
    if output_format == "lines":
        for item in payload.get("res", []):
            print(item)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Xunji training data via v2 Open API with local caching"
    )
    parser.add_argument("--date", required=True, help="Training date in YYYY-MM-DD")
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore cache and fetch from API again"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Send include_full_data:true to return unchecked sets, per-set RPE, and remarks.",
    )
    parser.add_argument(
        "--format", choices=["json", "lines"], default="json", help="Output format"
    )
    parser.add_argument("--cache-dir", help="Override cache directory")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        date_str = validate_date(args.date)
        cache_dir = (
            Path(args.cache_dir).expanduser() if args.cache_dir else default_cache_dir()
        )
        cache_path = cache_file_for(date_str, cache_dir)
        target_payload = None

        if not args.refresh:
            cached = read_cache(cache_path)
            if cached is not None:
                try:
                    api_key = resolve_api_key()
                except RuntimeError:
                    api_key = None
                ensure_action_library_history_cache(date_str, cache_dir, api_key)
                refresh_action_library(cache_dir)
                emit(cached, args.format)
                return 0

        api_key = resolve_api_key()
        target_payload = fetch_remote(
            date_str, api_key, include_full_data=bool(args.full)
        )
        ensure_action_library_history_cache(
            date_str, cache_dir, api_key, target_payload=target_payload
        )
        refresh_action_library(cache_dir)
        emit(target_payload, args.format)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
