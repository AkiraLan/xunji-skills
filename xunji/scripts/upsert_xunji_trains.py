#!/usr/bin/env python3

import argparse
import gzip
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATE_FORMAT = "%Y-%m-%d"
BASE_URL = "https://trains.xunjiapp.cn"
API_PATH = "/api_upsert_trains_for_llm_v2"
SCHEMA_VERSION = "train_open_api_v2"
MAX_TRAINS_PER_CALL = 4
MAX_MOVEMENTS_PER_TRAIN = 15
MAX_SETS_PER_MOVEMENT = 20
ACTION_START_RE = re.compile(r"^\d+\.(?!\d).+")
SET_TOKEN_RE = re.compile(r"^\d+组$")
COMPRESSED_SET_TOKEN_RE = re.compile(r"^[xX](\d+)$")
VERBAL_SET_TOKEN_RE = re.compile(r"^(?:做|共)(\d+)组$")
AMBIGUOUS_COMPACT_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?[xX×]\d+(?:\.\d+)?$")
VALID_WEIGHT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:kg|g|lb|lbs|公斤|千克|磅)$", re.IGNORECASE
)
WEIGHT_PREFIX_RE = re.compile(
    r"^(\d+(?:\.\d+)?(?:kg|g|lb|lbs|公斤|千克|磅))(.+)$", re.IGNORECASE
)
WEIGHT_VALUE_UNIT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)(kg|g|lb|lbs|公斤|千克|磅)$", re.IGNORECASE
)
REPS_TOKEN_RE = re.compile(r"^\d+次$")
TIME_TOKEN_RE = re.compile(r"^time:\d+(?:s|m|h)$", re.IGNORECASE)
TIME_VALUE_UNIT_RE = re.compile(r"^time:(\d+)([smh])$", re.IGNORECASE)
CARDIO_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?(?:km|m|kcal|bpm)$", re.IGNORECASE)
CARDIO_VALUE_UNIT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)(km|m|kcal|bpm)$", re.IGNORECASE
)
SELFWEIGHT_TOKEN_RE = re.compile(r"^自重$")
RANGE_TOKEN_RE = re.compile(
    r"^\d+(?:\.\d+)?-\d+(?:\.\d+)?(?:次|kg|g|lb|lbs|公斤|千克|磅)$",
    re.IGNORECASE,
)
RANGE_VALUE_UNIT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(次|kg|g|lb|lbs|公斤|千克|磅)$",
    re.IGNORECASE,
)
EXPLANATORY_TOKENS = {"热身", "工作组", "RPE", "REST"}
WARMUP_TOKENS = {"热身", "热身组"}


def resolve_api_key() -> str:
    api_key = os.getenv("XUNJI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing API key. Set XUNJI_API_KEY.")
    return api_key


def validate_target_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, DATE_FORMAT)
    except ValueError as exc:
        raise RuntimeError(f"Invalid date '{date_str}'. Expected YYYY-MM-DD") from exc
    return date_str


def normalize_line_date(token: str) -> str:
    token = token.strip()
    try:
        return datetime.strptime(token, DATE_FORMAT).strftime(DATE_FORMAT)
    except ValueError as exc:
        raise RuntimeError(
            f"datestr invalid in line: {token}. Expected YYYY-MM-DD; compact YYMMDD is not accepted."
        ) from exc


def default_cache_dir() -> Path:
    root = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "opencode" / "xunji-reader"


def cache_file_for(date_str: str, cache_dir: Path) -> Path:
    return cache_dir / "trains" / f"{date_str}.json"


def action_library_file(cache_dir: Path) -> Path:
    return cache_dir / "action-library.json"


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_action_name(name: str) -> str:
    return "".join(name.split()).casefold()


def load_action_library(cache_dir: Path) -> dict:
    path = action_library_file(cache_dir)
    payload = read_cache(path)
    if payload is None:
        raise RuntimeError(f"Action library not found: {path}")
    actions = payload.get("actions")
    aliases = payload.get("aliases")
    if not isinstance(actions, list) or not isinstance(aliases, dict):
        raise RuntimeError(f"Action library is invalid: {path}")
    return payload


def reader_script_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "xunji" / "scripts" / "fetch_xunji_trains.py"


def latest_cached_train_date(cache_dir: Path) -> str | None:
    trains_dir = cache_dir / "trains"
    if not trains_dir.exists():
        return None

    latest_date: str | None = None
    for path in sorted(trains_dir.glob("*.json")):
        try:
            cached_date = validate_target_date(path.stem)
        except RuntimeError:
            continue
        latest_date = cached_date
    return latest_date


def ensure_action_library(cache_dir: Path, target_date: str) -> dict:
    path = action_library_file(cache_dir)
    if path.exists():
        return load_action_library(cache_dir)

    reader_script = reader_script_path()
    if not reader_script.exists():
        raise RuntimeError(
            f"Action library bootstrap reader script not found: {reader_script}"
        )

    bootstrap_date = latest_cached_train_date(cache_dir) or target_date
    result = subprocess.run(
        [
            sys.executable,
            str(reader_script),
            "--date",
            bootstrap_date,
            "--cache-dir",
            str(cache_dir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "Action library missing and bootstrap via fetch_xunji_trains.py failed"
            + (f": {details}" if details else ".")
        )

    if not path.exists():
        raise RuntimeError(
            f"Action library bootstrap completed but no library was written: {path}"
        )
    return load_action_library(cache_dir)


def align_action_name(name: str, aliases: dict[str, str]) -> str | None:
    return aliases.get(normalize_action_name(name))


def align_res_lines_with_action_library(
    res_lines: list[str], aliases: dict[str, str]
) -> tuple[list[str], list[str]]:
    aligned_lines: list[str] = []
    unaligned_names: list[str] = []

    for line in res_lines:
        parsed = parse_training_line(line)
        if parsed.get("rest_day"):
            aligned_lines.append(build_training_line(parsed))
            continue

        aligned_actions: list[list[str]] = []
        for action in parsed.get("actions", []):
            if not action:
                continue
            header = action[0].strip()
            if "." not in header:
                aligned_actions.append(action)
                continue
            index, raw_name = header.split(".", 1)
            canonical_name = align_action_name(raw_name, aliases)
            if canonical_name is None:
                unaligned_names.append(raw_name.strip())
                aligned_actions.append(action)
                continue
            aligned_actions.append([f"{index}.{canonical_name}", *action[1:]])

        parsed["actions"] = aligned_actions
        aligned_lines.append(build_training_line(parsed))

    return aligned_lines, dedupe_preserve_order(unaligned_names)


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


def parse_res_file(path_str: str) -> list[str]:
    path = Path(path_str).expanduser()
    if not path.exists():
        raise RuntimeError(f"res file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("res"), list):
        return payload["res"]
    raise RuntimeError(
        "res file must be a JSON array or an object containing a list in 'res'"
    )


def load_res(args: argparse.Namespace) -> list[str]:
    if args.line:
        return args.line
    if args.res_json:
        payload = json.loads(args.res_json)
        if not isinstance(payload, list):
            raise RuntimeError("--res-json must decode to a JSON array")
        return payload
    if args.res_file:
        return parse_res_file(args.res_file)
    raise RuntimeError("Provide training lines via --line, --res-json, or --res-file")


def validate_res_lines(target_date: str, res_lines: list[str]) -> list[str]:
    if not isinstance(res_lines, list) or not res_lines:
        raise RuntimeError("res must be a non-empty array")
    if len(res_lines) > MAX_TRAINS_PER_CALL:
        raise RuntimeError(
            f"res may contain at most {MAX_TRAINS_PER_CALL} training lines per v2 call"
        )

    validated: list[str] = []
    for index, line in enumerate(res_lines, start=1):
        if not isinstance(line, str):
            raise RuntimeError(f"res[{index}] must be a string")
        if not line.strip():
            raise RuntimeError(f"res[{index}] is empty")
        if len(line) > 1500:
            raise RuntimeError(f"res[{index}] exceeds 1500 characters")
        line_date = normalize_line_date(line.split(",", 1)[0])
        if line_date != target_date:
            raise RuntimeError("all train lines must be in the same datestr")
        validated.append(line)
    return validated


def is_action_segment(segment: str) -> bool:
    return bool(ACTION_START_RE.match(segment.strip()))


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


def parse_training_line(line: str) -> dict:
    segments = [segment.strip() for segment in line.split(",")]
    date = normalize_line_date(segments[0])
    if len(segments) < 2:
        raise RuntimeError(f"training line is missing required content: {line}")

    if segments[1] == "休息日":
        return {"date": date, "rest_day": True, "raw": line}

    index = 1
    train_id = None
    if index < len(segments) and segments[index].startswith("id:"):
        train_id = segments[index]
        index += 1

    if index >= len(segments):
        raise RuntimeError(f"training line is missing a title: {line}")
    title = segments[index]
    index += 1

    train_time = None
    if index < len(segments) and segments[index].startswith("train_time:"):
        train_time = segments[index]
        index += 1

    notes: list[str] = []
    while index < len(segments) and not is_action_segment(segments[index]):
        notes.append(segments[index])
        index += 1

    actions: list[list[str]] = []
    current_action: list[str] | None = None
    while index < len(segments):
        token = segments[index]
        if is_action_segment(token):
            current_action = [token]
            actions.append(current_action)
        else:
            if current_action is None:
                notes.append(token)
            else:
                current_action.append(token)
        index += 1

    return {
        "date": date,
        "rest_day": False,
        "id": train_id,
        "title": title,
        "train_time": train_time,
        "notes": dedupe_preserve_order(notes),
        "actions": actions,
    }


def renumber_actions(actions: list[list[str]]) -> list[list[str]]:
    renumbered: list[list[str]] = []
    for index, action in enumerate(actions, start=1):
        if not action:
            continue
        name = action[0].split(".", 1)[1] if "." in action[0] else action[0]
        renumbered.append([f"{index}.{name}", *action[1:]])
    return renumbered


def build_training_line(parsed: dict) -> str:
    if parsed.get("rest_day"):
        return f"{parsed['date']},休息日"

    segments = [parsed["date"]]
    if parsed.get("id"):
        segments.append(parsed["id"])
    segments.append(parsed["title"])
    if parsed.get("train_time"):
        segments.append(parsed["train_time"])
    segments.extend(parsed.get("notes", []))
    for action in renumber_actions(parsed.get("actions", [])):
        segments.extend(action)
    return ",".join(segments)


def is_exact_structured_token(token: str) -> bool:
    normalized = token.strip()
    return bool(
        SET_TOKEN_RE.fullmatch(normalized)
        or VALID_WEIGHT_RE.fullmatch(normalized)
        or REPS_TOKEN_RE.fullmatch(normalized)
        or TIME_TOKEN_RE.fullmatch(normalized)
        or CARDIO_TOKEN_RE.fullmatch(normalized)
        or SELFWEIGHT_TOKEN_RE.fullmatch(normalized)
    )


def normalize_set_token(token: str) -> tuple[str | None, str | None]:
    normalized = token.strip()
    if SET_TOKEN_RE.fullmatch(normalized):
        return normalized, None

    for pattern in (COMPRESSED_SET_TOKEN_RE, VERBAL_SET_TOKEN_RE):
        match = pattern.fullmatch(normalized)
        if match:
            count = int(match.group(1))
            return f"{count}组", normalized

    return None, None


def extract_set_count(token: str) -> int | None:
    normalized, _source = normalize_set_token(token)
    if not normalized:
        return None
    return int(normalized[:-1])


def normalize_weight_token(token: str) -> tuple[str | None, str | None]:
    normalized = token.strip()
    if VALID_WEIGHT_RE.fullmatch(normalized):
        return normalized, None

    match = WEIGHT_PREFIX_RE.fullmatch(normalized)
    if not match:
        return None, None

    structured_weight, invalid_suffix = match.groups()
    invalid_suffix = invalid_suffix.strip()
    if not invalid_suffix:
        return structured_weight, None
    return structured_weight, invalid_suffix


def normalize_range_token(token: str) -> str | None:
    normalized = token.strip()
    match = RANGE_VALUE_UNIT_RE.fullmatch(normalized)
    if not match:
        return None
    _lower, upper, unit = match.groups()
    if "." in upper:
        upper = upper.rstrip("0").rstrip(".")
    return f"{upper}{unit}"


def normalize_action_tokens(
    action: list[str], preserve_invalid_in_remarks: bool
) -> tuple[list[str], list[str], list[str]]:
    if not action:
        return [], [], []

    normalized_action = [action[0]]
    note_tokens: list[str] = []
    warnings: list[str] = []
    action_has_any_explicit_set = any(
        extract_set_count(token.strip()) is not None for token in action[1:]
    )

    for raw_token in action[1:]:
        token = raw_token.strip()
        if not token:
            continue
        if AMBIGUOUS_COMPACT_TOKEN_RE.fullmatch(token):
            raise RuntimeError(
                "clarification_needed: ambiguous compact token "
                f"'{token}' cannot be written safely. Ask the user what each number means "
                "and rewrite it with explicit units such as '10kg,3次' or '10次,3组' before writing."
            )
        normalized_set_token, set_source = normalize_set_token(token)
        if normalized_set_token:
            normalized_action.append(normalized_set_token)
            if set_source and set_source != normalized_set_token:
                warnings.append(
                    f"Normalized repeated-set token '{token}' to structured token '{normalized_set_token}'"
                )
            continue
        if token in WARMUP_TOKENS:
            has_set_token = any(
                extract_set_count(existing) is not None
                for existing in normalized_action[1:]
            )
            if not has_set_token and not action_has_any_explicit_set:
                normalized_action.append("1组")
                warnings.append(
                    f"Normalized warm-up marker '{token}' to formal set token '1组'"
                )
            continue
        if is_exact_structured_token(token):
            normalized_action.append(token)
            continue

        structured_weight, invalid_suffix = normalize_weight_token(token)
        if structured_weight:
            normalized_action.append(structured_weight)
            if invalid_suffix:
                warnings.append(
                    f"Discarded invalid weight attachment '{invalid_suffix}' from token '{token}'"
                )
            continue

        if RANGE_TOKEN_RE.fullmatch(token):
            normalized_range = normalize_range_token(token)
            if normalized_range:
                normalized_action.append(normalized_range)
                warnings.append(
                    f"Normalized range token '{token}' to upper-bound structured value '{normalized_range}'"
                )
                continue
            warnings.append(
                f"Discarded non-exact range token '{token}' instead of structured slots"
            )
            if preserve_invalid_in_remarks:
                note_tokens.append(token)
            continue

        if token.upper() in EXPLANATORY_TOKENS or token in EXPLANATORY_TOKENS:
            warnings.append(
                f"Discarded explanatory token '{token}' instead of structured slots"
            )
            if preserve_invalid_in_remarks:
                note_tokens.append(token)
            continue

        warnings.append(
            f"Discarded non-structured token '{token}' instead of structured slots"
        )
        if preserve_invalid_in_remarks:
            note_tokens.append(token)

    return normalized_action, note_tokens, warnings


def expand_repeated_set_action(action: list[str]) -> tuple[list[str], str | None]:
    if not action:
        return action, None

    set_positions = [
        (index, count)
        for index, token in enumerate(action[1:], start=1)
        if (count := extract_set_count(token)) is not None
    ]
    if len(set_positions) != 1:
        return action, None

    set_index, set_count = set_positions[0]
    if set_count <= 1:
        return action, None

    shared_payload = [
        token for index, token in enumerate(action[1:], start=1) if index != set_index
    ]
    if not shared_payload:
        return action, None

    expanded_action = [action[0]]
    for group_index in range(1, set_count + 1):
        expanded_action.append(f"{group_index}组")
        expanded_action.extend(shared_payload)

    return (
        expanded_action,
        f"Expanded repeated-set shorthand in action '{action[0]}' from '{action[set_index]}' into explicit per-set segments",
    )


def merge_same_plan_new_lines(parsed_lines: list[dict]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    merged: list[dict] = []
    grouped_indices: set[int] = set()

    for index, parsed in enumerate(parsed_lines):
        if index in grouped_indices:
            continue
        if parsed.get("rest_day") or parsed.get("id"):
            merged.append(parsed)
            continue

        group = [parsed]
        grouped_indices.add(index)
        for other_index in range(index + 1, len(parsed_lines)):
            if other_index in grouped_indices:
                continue
            other = parsed_lines[other_index]
            if other.get("rest_day") or other.get("id"):
                continue
            if other.get("title") == parsed.get("title") and other.get(
                "train_time"
            ) == parsed.get("train_time"):
                group.append(other)
                grouped_indices.add(other_index)

        if len(group) == 1:
            merged.append(parsed)
            continue

        combined = {
            "date": parsed["date"],
            "rest_day": False,
            "id": None,
            "title": parsed["title"],
            "train_time": parsed.get("train_time"),
            "notes": dedupe_preserve_order(
                [note for item in group for note in item.get("notes", [])]
            ),
            "actions": [action for item in group for action in item.get("actions", [])],
        }
        warnings.append(
            f"Merged {len(group)} new same-plan lines with title '{parsed['title']}' into one training line"
        )
        merged.append(combined)

    return merged, warnings


def normalize_res_lines(
    target_date: str, res_lines: list[str], preserve_invalid_in_remarks: bool
) -> tuple[list[str], list[str]]:
    parsed_lines = [parse_training_line(line) for line in res_lines]
    merged_lines, merge_warnings = merge_same_plan_new_lines(parsed_lines)
    warnings = list(merge_warnings)
    normalized_lines: list[str] = []

    for parsed in merged_lines:
        if parsed.get("date") != target_date:
            raise RuntimeError("all train lines must be in the same datestr")
        if parsed.get("rest_day"):
            normalized_lines.append(build_training_line(parsed))
            continue

        normalized_actions: list[list[str]] = []
        note_tokens = list(parsed.get("notes", []))
        for action in parsed.get("actions", []):
            normalized_action, extra_notes, action_warnings = normalize_action_tokens(
                action, preserve_invalid_in_remarks
            )
            if normalized_action:
                normalized_action, expansion_warning = expand_repeated_set_action(
                    normalized_action
                )
                normalized_actions.append(normalized_action)
                if expansion_warning:
                    warnings.append(expansion_warning)
            note_tokens.extend(extra_notes)
            warnings.extend(action_warnings)

        parsed["notes"] = dedupe_preserve_order(note_tokens)
        parsed["actions"] = normalized_actions
        normalized_lines.append(build_training_line(parsed))

    return normalized_lines, dedupe_preserve_order(warnings)


def second_segment(line: str) -> str | None:
    parts = line.split(",", 2)
    if len(parts) < 2:
        return None
    return parts[1]


def is_rest_day_line(line: str) -> bool:
    second = second_segment(line)
    return second == "休息日"


def line_has_id(line: str) -> bool:
    second = second_segment(line)
    return bool(second and second.startswith("id:"))


def extract_id(line: str) -> str | None:
    second = second_segment(line)
    if second and second.startswith("id:"):
        return second[3:]
    return None


def extract_train_time(line: str) -> str | None:
    for segment in line.split(","):
        if segment.startswith("train_time:"):
            return segment[len("train_time:") :]
    return None


def validate_create_intent(res_lines: list[str], allow_new_records: bool) -> None:
    missing_ids = [
        line
        for line in res_lines
        if not is_rest_day_line(line) and not line_has_id(line)
    ]
    if missing_ids and not allow_new_records:
        raise RuntimeError(
            "One or more non-rest-day lines do not include id:... . Pass --allow-new-records only when you intentionally want to create new records."
        )


def validate_train_time_preservation(
    cache_path: Path, res_lines: list[str], allow_train_time_drop: bool
) -> None:
    cached = read_cache(cache_path)
    if cached is None:
        return
    cached_res = cached.get("res")
    if not isinstance(cached_res, list):
        raise RuntimeError(
            f"Cached res is invalid for preservation check: {cache_path}"
        )

    existing_by_id = {
        train_id: line
        for line in cached_res
        if isinstance(line, str) and (train_id := extract_id(line))
    }

    for line in res_lines:
        train_id = extract_id(line)
        if not train_id:
            continue
        previous = existing_by_id.get(train_id)
        if previous is None:
            continue
        previous_train_time = extract_train_time(previous)
        current_train_time = extract_train_time(line)
        if (
            previous_train_time
            and current_train_time != previous_train_time
            and not allow_train_time_drop
        ):
            raise RuntimeError(
                f"Line with id:{train_id} must preserve existing train_time:{previous_train_time}. Preserve it exactly or pass --allow-train-time-drop explicitly."
            )


def is_valid_strength_set_value(token: str) -> bool:
    normalized = token.strip()
    return bool(
        VALID_WEIGHT_RE.fullmatch(normalized)
        or REPS_TOKEN_RE.fullmatch(normalized)
        or TIME_TOKEN_RE.fullmatch(normalized)
        or SELFWEIGHT_TOKEN_RE.fullmatch(normalized)
    )


def validate_strength_set_values(res_lines: list[str]) -> None:
    for line in res_lines:
        parsed = parse_training_line(line)
        if parsed.get("rest_day"):
            continue
        for action in parsed.get("actions", []):
            set_indices = [
                index
                for index, token in enumerate(action)
                if extract_set_count(token) is not None
            ]
            if not set_indices:
                continue
            for position, set_index in enumerate(set_indices):
                next_set_index = (
                    set_indices[position + 1]
                    if position + 1 < len(set_indices)
                    else len(action)
                )
                payload = action[set_index + 1 : next_set_index]
                if not any(is_valid_strength_set_value(token) for token in payload):
                    raise RuntimeError(
                        f"Set token '{action[set_index]}' in action '{action[0]}' must include at least one of weight, reps, or time."
                    )


def build_cache_payload(
    date_str: str, res: list[str], count: int | None = None, **extra: object
) -> dict:
    return {
        "datestr": date_str,
        "cached": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": count if count is not None else len(res),
        "res": res,
        **extra,
    }


def parse_set_payload_token(token: str) -> dict | None:
    normalized = token.strip()
    m = WEIGHT_VALUE_UNIT_RE.fullmatch(normalized)
    if m:
        value_str, unit = m.group(1), m.group(2).lower()
        return {"weight": value_str, "unit": unit}
    m = REPS_TOKEN_RE.fullmatch(normalized)
    if m:
        return {"reps": normalized[:-1]}
    m = TIME_VALUE_UNIT_RE.fullmatch(normalized)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = n if unit == "s" else n * 60 if unit == "m" else n * 3600
        return {"duration_s": secs}
    if SELFWEIGHT_TOKEN_RE.fullmatch(normalized):
        return {"selfWeight": True}
    m = CARDIO_VALUE_UNIT_RE.fullmatch(normalized)
    if m:
        raw = m.group(1)
        unit = m.group(2).lower()
        val: float | int = float(raw) if "." in raw else int(raw)
        if unit == "km":
            distance_m = val * 1000 if isinstance(val, float) else val * 1000
            if isinstance(distance_m, float):
                distance_m = int(distance_m) if distance_m.is_integer() else distance_m
            return {"metrics": {"distance": distance_m, "distance_unit": "m"}}
        if unit == "m":
            return {"metrics": {"distance": val, "distance_unit": "m"}}
        if unit == "kcal":
            return {"metrics": {"kcal": val}}
        if unit == "bpm":
            return {"metrics": {"bpm": val}}
    return None


def action_to_movement(action: list[str]) -> dict:
    if not action:
        return {"name": "", "sets": []}
    header = action[0].strip()
    name = header.split(".", 1)[1] if "." in header else header
    sets: list[dict] = []
    current: dict | None = None
    for raw_token in action[1:]:
        tok = raw_token.strip()
        if not tok:
            continue
        if SET_TOKEN_RE.fullmatch(tok):
            if current is not None:
                sets.append(current)
            current = {"done": True}
            continue
        partial = parse_set_payload_token(tok)
        if partial is None:
            continue
        if current is None:
            current = {"done": True}
        if "metrics" in partial:
            merged = dict(current.get("metrics") or {})
            merged.update(partial["metrics"])
            current["metrics"] = merged
        else:
            current.update(partial)
    if current is not None:
        sets.append(current)
    return {"name": name, "sets": sets}


def parsed_line_to_v2_train(parsed: dict) -> dict:
    if parsed.get("rest_day"):
        return {"datestr": parsed["date"], "rest_day": True}
    train: dict = {
        "datestr": parsed["date"],
        "title": parsed.get("title", ""),
    }
    train_id = parsed.get("id")
    if train_id and train_id.startswith("id:"):
        raw = train_id[3:]
        try:
            train["localid"] = int(raw)
        except ValueError:
            train["localid"] = raw
    train_time = parsed.get("train_time")
    if train_time and train_time.startswith("train_time:"):
        tt = train_time[len("train_time:") :]
        if "-" in tt:
            start_str, end_str = tt.split("-", 1)
            try:
                train["start"] = int(start_str)
                train["end"] = int(end_str)
            except ValueError:
                pass
    notes = parsed.get("notes") or []
    if notes:
        train["remarks"] = ",".join(notes)
    movements = [
        action_to_movement(action) for action in parsed.get("actions", [])
    ]
    train["movements"] = movements
    return train


def res_lines_to_v2_trains(res_lines: list[str]) -> list[dict]:
    trains: list[dict] = []
    for line in res_lines:
        parsed = parse_training_line(line)
        trains.append(parsed_line_to_v2_train(parsed))
    return trains


def enforce_v2_payload_limits(trains: list[dict]) -> None:
    if len(trains) > MAX_TRAINS_PER_CALL:
        raise RuntimeError(
            f"v2 API accepts at most {MAX_TRAINS_PER_CALL} trains per call (got {len(trains)})"
        )
    for train in trains:
        if train.get("rest_day"):
            continue
        movements = train.get("movements") or []
        if len(movements) > MAX_MOVEMENTS_PER_TRAIN:
            raise RuntimeError(
                f"Train '{train.get('title')}' has {len(movements)} movements; v2 API max is {MAX_MOVEMENTS_PER_TRAIN}"
            )
        for movement in movements:
            sets = movement.get("sets") or []
            if len(sets) > MAX_SETS_PER_MOVEMENT:
                raise RuntimeError(
                    f"Movement '{movement.get('name')}' has {len(sets)} sets; v2 API max is {MAX_SETS_PER_MOVEMENT}"
                )


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
            payload_tokens = v2_set_to_tokens(sets[0])
            segments.extend(payload_tokens)
            continue
        for set_idx, set_obj in enumerate(sets, start=1):
            segments.append(f"{set_idx}组")
            segments.extend(v2_set_to_tokens(set_obj))
    return ",".join(seg for seg in segments if seg is not None and seg != "")


def trains_response_to_lines(payload_res: object) -> list[str]:
    trains: list[dict] = []
    if isinstance(payload_res, dict):
        candidate = payload_res.get("trains")
        if isinstance(candidate, list):
            trains = [t for t in candidate if isinstance(t, dict)]
    elif isinstance(payload_res, list):
        for item in payload_res:
            if isinstance(item, dict):
                trains.append(item)
    return [v2_train_to_csv_line(train) for train in trains]


def fetch_remote(
    trains: list[dict],
    api_key: str,
    *,
    client_request_id: str,
    include_full_data: bool = False,
    server_dry_run: bool = False,
) -> dict:
    url = f"{BASE_URL}{API_PATH}"
    body = {
        "schema_version": SCHEMA_VERSION,
        "client_request_id": client_request_id,
        "dry_run": bool(server_dry_run),
        "include_full_data": bool(include_full_data),
        "res": trains,
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
        raise RuntimeError("Unexpected API response format")

    if payload.get("success") is False:
        raise RuntimeError(f"API returned success=false: {decoded_text}")

    res_payload = payload.get("res")
    response_lines = trains_response_to_lines(res_payload)
    response_trains: list[dict]
    if isinstance(res_payload, dict) and isinstance(res_payload.get("trains"), list):
        response_trains = [
            t for t in res_payload["trains"] if isinstance(t, dict)
        ]
    elif isinstance(res_payload, list):
        response_trains = [t for t in res_payload if isinstance(t, dict)]
    else:
        response_trains = []

    count = payload.get("count")
    if not response_lines:
        acknowledged_count = (
            count if isinstance(count, int) and count > 0 else len(trains)
        )
        return {
            "count": acknowledged_count,
            "res": [],
            "response_trains": [],
            "response_res_empty": True,
            "raw_response": decoded_text,
            "acknowledged_with_empty_res": True,
            "server_count": count if isinstance(count, int) else None,
        }

    if isinstance(count, int) and count <= 0:
        raise RuntimeError(
            f"API response 'count' must be > 0 after successful write: {decoded_text}"
        )

    return {
        "count": count if isinstance(count, int) else len(response_lines),
        "res": response_lines,
        "response_trains": response_trains,
        "response_res_empty": False,
        "raw_response": decoded_text,
        "acknowledged_with_empty_res": False,
        "server_count": count if isinstance(count, int) else None,
    }


def emit(payload: dict, output_format: str) -> None:
    if output_format == "lines":
        for item in payload.get("res", []):
            print(item)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upsert Xunji training data with same-day validation and cache sync"
    )
    parser.add_argument("--date", required=True, help="Training date in YYYY-MM-DD")
    parser.add_argument(
        "--format", choices=["json", "lines"], default="json", help="Output format"
    )
    parser.add_argument("--cache-dir", help="Override cache directory")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate inputs without calling the API"
    )
    parser.add_argument(
        "--allow-new-records",
        action="store_true",
        help="Required when any non-rest-day line intentionally omits id:... to create a new record.",
    )
    parser.add_argument(
        "--allow-train-time-drop",
        action="store_true",
        help="Allow dropping cached train_time metadata for an existing id:... line.",
    )
    parser.add_argument(
        "--preserve-invalid-in-remarks",
        action="store_true",
        help="Keep discarded invalid tokens in remarks instead of dropping them after warning.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Send include_full_data:true so the API echoes back unchecked sets, per-set RPE, and remarks.",
    )
    parser.add_argument(
        "--server-dry-run",
        action="store_true",
        help=(
            "Set dry_run:true in the v2 API body so the server is supposed to validate without writing. "
            "WARNING: live tests on 2026-05-19 showed the Xunji server still persists these calls, "
            "so prefer the local --dry-run flag for non-destructive validation."
        ),
    )
    parser.add_argument(
        "--client-request-id",
        help="Override the auto-generated client_request_id used for idempotency.",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--line",
        action="append",
        help="Training line to upsert. Repeat this flag for multiple lines.",
    )
    group.add_argument("--res-json", help="JSON array string containing training lines")
    group.add_argument(
        "--res-file",
        help="Path to a JSON file containing either a raw array or an object with 'res'",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        target_date = validate_target_date(args.date)
        res_lines = validate_res_lines(target_date, load_res(args))
        res_lines, normalization_warnings = normalize_res_lines(
            target_date, res_lines, args.preserve_invalid_in_remarks
        )
        cache_dir = (
            Path(args.cache_dir).expanduser() if args.cache_dir else default_cache_dir()
        )
        library = ensure_action_library(cache_dir, target_date)
        aliases = library.get("aliases")
        if not isinstance(aliases, dict):
            raise RuntimeError(
                f"Action library aliases are invalid: {action_library_file(cache_dir)}"
            )
        res_lines, unaligned_action_names = align_res_lines_with_action_library(
            res_lines, aliases
        )
        if unaligned_action_names:
            raise RuntimeError(
                "Unaligned action names: "
                f"{unaligned_action_names}. Modify these names to match existing Xunji action names, "
                "or add them in the Xunji app first, then retry."
            )
        res_lines = validate_res_lines(target_date, res_lines)
        validate_create_intent(res_lines, args.allow_new_records)
        cache_path = cache_file_for(target_date, cache_dir)
        validate_train_time_preservation(
            cache_path, res_lines, args.allow_train_time_drop
        )
        validate_strength_set_values(res_lines)

        v2_trains = res_lines_to_v2_trains(res_lines)
        enforce_v2_payload_limits(v2_trains)
        client_request_id = args.client_request_id or f"xunji-skill-{uuid.uuid4()}"

        if args.dry_run:
            emit(
                {
                    "datestr": target_date,
                    "cached": False,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(res_lines),
                    "res": res_lines,
                    "trains": v2_trains,
                    "client_request_id": client_request_id,
                    "include_full_data": bool(args.full),
                    "server_dry_run": bool(args.server_dry_run),
                    "dry_run": True,
                    "cache_path": str(cache_path),
                    "normalization_warnings": normalization_warnings,
                },
                args.format,
            )
            return 0

        api_key = resolve_api_key()
        remote_payload = fetch_remote(
            v2_trains,
            api_key,
            client_request_id=client_request_id,
            include_full_data=bool(args.full),
            server_dry_run=bool(args.server_dry_run),
        )
        if remote_payload.get("acknowledged_with_empty_res"):
            final_res = list(res_lines)
            final_trains = list(v2_trains)
        else:
            final_res = validate_res_lines(target_date, remote_payload["res"])
            final_trains = remote_payload.get("response_trains") or []
            server_count = remote_payload.get("server_count")
            if isinstance(server_count, int) and server_count != len(final_res):
                raise RuntimeError(
                    "API response count does not match the number of returned res lines: "
                    f"count={server_count} len(res)={len(final_res)}; "
                    f"raw_response={remote_payload['raw_response']}"
                )
        payload = build_cache_payload(
            target_date,
            final_res,
            count=remote_payload["count"],
            trains=final_trains,
            response_res_empty=remote_payload.get("response_res_empty"),
            normalization_warnings=normalization_warnings,
            client_request_id=client_request_id,
            include_full_data=bool(args.full),
            server_dry_run=bool(args.server_dry_run),
        )
        atomic_write_json(cache_path, payload)
        emit(payload, args.format)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
