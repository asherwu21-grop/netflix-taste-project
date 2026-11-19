#!/usr/bin/env python3
"""Clean Netflix viewing events, enrich them from a title catalog, and split by date.

Uses only the Python standard library. The original input files are never modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

EVENT_REQUIRED = [
    "event_id", "member_id", "title_id", "event_datetime", "watch_seconds",
    "completed", "device_type", "country", "profile_type",
]
CATALOG_REQUIRED = [
    "title_id", "title_name", "title_type", "primary_genre", "secondary_genre",
    "release_date", "maturity_rating", "runtime_minutes", "is_new_series",
]
CATALOG_FIELDS_TO_ADD = [
    "title_name", "title_type", "primary_genre", "secondary_genre",
    "runtime_minutes", "maturity_rating", "release_date", "is_new_series",
]


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header row: {path}")
        fields = [str(name).strip() for name in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized = {}
            for key, value in row.items():
                if key is not None:
                    normalized[str(key).strip()] = "" if value is None else str(value)
            rows.append(normalized)
    return fields, rows


def require_columns(actual: list[str], required: list[str], label: str) -> None:
    missing = [column for column in required if column not in actual]
    if missing:
        raise ValueError(f"{label} is missing required columns: {', '.join(missing)}")


def parse_event_date(value: str) -> tuple[str, date] | tuple[None, None]:
    """Return canonical ISO text and calendar date. Supports date-only or ISO datetime."""
    value = (value or "").strip()
    if not value:
        return None, None
    try:
        if "T" in value or " " in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.isoformat(), parsed.date()
        parsed_date = date.fromisoformat(value)
        return parsed_date.isoformat(), parsed_date
    except ValueError:
        return None, None


def parse_number(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_bool(value: str) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def bool_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "True" if value else "False"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def load_catalog(path: Path) -> tuple[list[str], dict[str, dict[str, str]], list[str]]:
    fields, rows = read_csv(path)
    require_columns(fields, CATALOG_REQUIRED, "Title catalog")
    catalog: dict[str, dict[str, str]] = {}
    duplicate_ids: list[str] = []
    for row in rows:
        title_id = row.get("title_id", "").strip().upper()
        if not title_id:
            continue
        if title_id in catalog:
            duplicate_ids.append(title_id)
            continue
        item = dict(row)
        item["title_id"] = title_id
        catalog[title_id] = item
    if duplicate_ids:
        raise ValueError(
            "Title catalog contains duplicate title_id values; refusing ambiguous joins: "
            + ", ".join(sorted(set(duplicate_ids)))
        )
    return fields, catalog, rows


def clean_events(
    events_path: Path,
    catalog_path: Path,
    output_dir: Path,
    cutoff: date,
    max_watch_seconds: int,
    completion_threshold: float,
) -> dict[str, Any]:
    if max_watch_seconds <= 0:
        raise ValueError("max_watch_seconds must be positive")
    if not 0 < completion_threshold <= 1:
        raise ValueError("completion_threshold must be greater than 0 and at most 1")

    event_fields, raw_events = read_csv(events_path)
    require_columns(event_fields, EVENT_REQUIRED, "Viewing events export")
    catalog_fields, catalog, catalog_rows = load_catalog(catalog_path)

    # Detect exact duplicates using the original CSV field values; keep the first occurrence.
    seen_rows: set[tuple[str, ...]] = set()
    unique_events: list[tuple[int, dict[str, str]]] = []
    duplicate_rows: list[dict[str, Any]] = []
    duplicate_indices: set[int] = set()
    for row_index, row in enumerate(raw_events):
        signature = tuple(row.get(field, "") for field in event_fields)
        if signature in seen_rows:
            duplicate_indices.add(row_index)
            duplicate_rows.append({**row, "exclusion_reason": "exact_duplicate_row"})
        else:
            seen_rows.add(signature)
            unique_events.append((row_index, row))

    # Conflicting records that reuse an event_id are all excluded, rather than arbitrarily choosing one.
    id_to_indices: dict[str, list[int]] = defaultdict(list)
    for unique_position, (_, row) in enumerate(unique_events):
        event_id = row.get("event_id", "").strip()
        if event_id:
            id_to_indices[event_id].append(unique_position)
    conflicting_positions = {
        position
        for positions in id_to_indices.values() if len(positions) > 1
        for position in positions
    }

    all_audit: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    model_ready: list[dict[str, Any]] = []
    reasons_counter: Counter[str] = Counter()
    completion_changed = 0
    completed_original_counts: Counter[str] = Counter()
    completion_recomputed_counts: Counter[str] = Counter()

    # Map original input row index to its final audit record for preserving the input order.
    audit_by_input_index: dict[int, dict[str, Any]] = {}

    output_fields = list(event_fields)
    extra_fields = [
        "title_id_original", "completed_original", "completion_recomputed",
        "completion_changed", "completion_threshold", "runtime_seconds",
        *CATALOG_FIELDS_TO_ADD, "exclusion_reasons", "disposition", "dataset_split",
    ]
    # Don't duplicate catalog fields if a source export happens to contain any.
    output_fields.extend(field for field in extra_fields if field not in output_fields)

    for unique_position, (input_index, original) in enumerate(unique_events):
        row = {field: (original.get(field, "") or "").strip() for field in event_fields}
        reasons: list[str] = []

        # Normalize categorical fields and make missing categories explicit.
        row["device_type"] = row.get("device_type", "").strip().lower() or "Unknown"
        row["country"] = row.get("country", "").strip().upper() or "Unknown"

        original_title_id = row.get("title_id", "")
        title_id = original_title_id.upper()
        row["title_id_original"] = original_title_id
        row["title_id"] = title_id

        original_completed = row.get("completed", "")
        original_completed_bool = parse_bool(original_completed)
        completed_original_counts[original_completed.strip().lower() or "blank"] += 1
        row["completed_original"] = original_completed

        canonical_date, event_day = parse_event_date(row.get("event_datetime", ""))
        if canonical_date is None or event_day is None:
            reasons.append("invalid_event_datetime")
        else:
            row["event_datetime"] = canonical_date

        duration_value = parse_number(row.get("watch_seconds", ""))
        if not row.get("watch_seconds", "").strip():
            reasons.append("missing_watch_seconds")
        elif duration_value is None or duration_value <= 0:
            reasons.append("invalid_watch_seconds")
        else:
            # Keep whole seconds as an integer; reject fractional seconds as malformed.
            if not duration_value.is_integer():
                reasons.append("invalid_watch_seconds")
            else:
                row["watch_seconds"] = str(int(duration_value))
                if duration_value > max_watch_seconds:
                    reasons.append("watch_seconds_over_threshold")

        catalog_row = catalog.get(title_id)
        if catalog_row is None:
            reasons.append("unmatched_title_id")
            for field in CATALOG_FIELDS_TO_ADD:
                row[field] = ""
            row["runtime_seconds"] = ""
        else:
            for field in CATALOG_FIELDS_TO_ADD:
                row[field] = (catalog_row.get(field, "") or "").strip()
            runtime_minutes = parse_number(catalog_row.get("runtime_minutes", ""))
            if runtime_minutes is None or runtime_minutes <= 0:
                reasons.append("missing_catalog_runtime")
                row["runtime_seconds"] = ""
            else:
                runtime_seconds = int(round(runtime_minutes * 60))
                row["runtime_seconds"] = str(runtime_seconds)

        if original_completed_bool is None:
            reasons.append("invalid_completed_value")

        if not row.get("event_id", "").strip():
            reasons.append("missing_event_id")
        if unique_position in conflicting_positions:
            reasons.append("duplicate_event_id_conflict")

        # De-duplicate reason names while preserving a stable order.
        reasons = list(dict.fromkeys(reasons))
        row["completion_threshold"] = str(completion_threshold)
        row["completion_changed"] = ""
        row["completion_recomputed"] = "False"

        if "unmatched_title_id" in reasons:
            unmatched.append({**row, "exclusion_reasons": ";".join(reasons)})

        if reasons:
            row["exclusion_reasons"] = ";".join(reasons)
            row["disposition"] = "excluded"
            row["dataset_split"] = ""
            excluded.append(row)
            for reason in reasons:
                reasons_counter[reason] += 1
        else:
            # All values required for a recomputed completion label are valid here.
            assert duration_value is not None and row.get("runtime_seconds")
            runtime_seconds_int = int(row["runtime_seconds"])
            derived_completed = duration_value >= runtime_seconds_int * completion_threshold
            row["completed"] = bool_text(derived_completed)
            row["completion_recomputed"] = "True"
            changed = original_completed_bool != derived_completed
            row["completion_changed"] = bool_text(changed)
            completion_recomputed_counts[bool_text(derived_completed)] += 1
            if changed:
                completion_changed += 1
            row["exclusion_reasons"] = ""
            row["disposition"] = "model_ready"
            assert event_day is not None
            split_name = "train" if event_day < cutoff else "holdout"
            row["dataset_split"] = split_name
            model_ready.append(row)

        audit_by_input_index[input_index] = row

    # Add exact duplicate records to audit and duplicate log.
    for duplicate_index, duplicate in enumerate(raw_events):
        if duplicate_index not in duplicate_indices:
            continue
        audit_record = {field: (duplicate.get(field, "") or "").strip() for field in event_fields}
        audit_record.update({field: "" for field in output_fields if field not in audit_record})
        audit_record["title_id_original"] = audit_record.get("title_id", "")
        audit_record["completed_original"] = audit_record.get("completed", "")
        audit_record["exclusion_reasons"] = "exact_duplicate_row"
        audit_record["disposition"] = "duplicate_removed"
        audit_record["dataset_split"] = ""
        audit_by_input_index[duplicate_index] = audit_record

    all_audit = [audit_by_input_index[index] for index in range(len(raw_events))]

    # Stable chronological ordering improves readability and reproducibility.
    model_ready.sort(key=lambda r: (r.get("event_datetime", ""), r.get("event_id", "")))
    train = [row for row in model_ready if row["dataset_split"] == "train"]
    holdout = [row for row in model_ready if row["dataset_split"] == "holdout"]
    excluded.sort(key=lambda r: (r.get("event_datetime", ""), r.get("event_id", "")))
    unmatched.sort(key=lambda r: (r.get("event_datetime", ""), r.get("event_id", "")))

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "train.csv", output_fields, train)
    write_csv(output_dir / "holdout.csv", output_fields, holdout)
    write_csv(output_dir / "excluded_events.csv", output_fields, excluded)
    write_csv(output_dir / "unmatched_titles.csv", output_fields, unmatched)
    write_csv(output_dir / "events_audit.csv", output_fields, all_audit)
    duplicate_fields = list(event_fields) + ["exclusion_reason"]
    write_csv(output_dir / "duplicates_removed.csv", duplicate_fields, duplicate_rows)

    # Validations over the actual saved datasets.
    input_count = len(raw_events)
    duplicate_count = len(duplicate_rows)
    unique_count = len(unique_events)
    excluded_count = len(excluded)
    model_ready_count = len(model_ready)
    train_count = len(train)
    holdout_count = len(holdout)

    train_ids = {r["event_id"] for r in train}
    holdout_ids = {r["event_id"] for r in holdout}
    assert not (train_ids & holdout_ids), "Train and holdout event IDs overlap"
    assert input_count == duplicate_count + excluded_count + model_ready_count, (
        "Event accounting failed: input rows must equal duplicate rows removed + excluded + model-ready"
    )
    assert unique_count == excluded_count + model_ready_count, "Unique-row accounting failed"
    assert len(all_audit) == input_count, "Audit file does not account for every input row"
    assert all(r["title_name"] and r["runtime_seconds"] for r in model_ready), "Model-ready row lacks catalog runtime"
    assert all(parse_number(r["watch_seconds"]) is not None for r in model_ready), "Model-ready row lacks valid duration"
    assert all(parse_bool(r["completed"]) is not None for r in model_ready), "Model-ready row lacks valid completion"
    assert all(parse_bool(r["completion_recomputed"]) is not None and r["completion_recomputed"] == "True" for r in model_ready)

    def date_range(rows: list[dict[str, Any]]) -> tuple[str, str]:
        values = [r["event_datetime"] for r in rows if r.get("event_datetime")]
        return (min(values), max(values)) if values else ("N/A", "N/A")

    train_range = date_range(train)
    holdout_range = date_range(holdout)
    assert all(date.fromisoformat(r["event_datetime"][:10]) < cutoff for r in train)
    assert all(date.fromisoformat(r["event_datetime"][:10]) >= cutoff for r in holdout)

    raw_train_count = sum(
        (parse_event_date(r.get("event_datetime", ""))[1] or date.min) < cutoff
        for r in raw_events
    )
    raw_holdout_count = input_count - raw_train_count

    report = {
        "input_events": input_count,
        "exact_duplicate_rows_removed": duplicate_count,
        "unique_events_after_exact_deduplication": unique_count,
        "model_ready_events": model_ready_count,
        "excluded_unique_events": excluded_count,
        "train_events": train_count,
        "holdout_events": holdout_count,
        "cutoff_date": cutoff.isoformat(),
        "raw_train_date_partition_count_before_cleaning": raw_train_count,
        "raw_holdout_date_partition_count_before_cleaning": raw_holdout_count,
        "train_date_range": train_range,
        "holdout_date_range": holdout_range,
        "unmatched_title_events": len(unmatched),
        "unmatched_title_id_counts": dict(Counter(r["title_id"] for r in unmatched)),
        "catalog_titles": len(catalog_rows),
        "catalog_titles_missing_usable_runtime": sum(
            parse_number(r.get("runtime_minutes", "")) is None or parse_number(r.get("runtime_minutes", "")) <= 0
            for r in catalog_rows
        ),
        "completion_flags_recomputed": model_ready_count,
        "completion_flags_changed": completion_changed,
        "completion_recomputed_counts": dict(completion_recomputed_counts),
        "original_completed_counts": dict(completed_original_counts),
        "exclusion_reason_counts_nonexclusive": dict(reasons_counter),
        "validation": {
            "all_input_rows_accounted_for": input_count == duplicate_count + excluded_count + model_ready_count,
            "no_train_holdout_event_id_overlap": not bool(train_ids & holdout_ids),
            "train_ends_before_cutoff": all(date.fromisoformat(r["event_datetime"][:10]) < cutoff for r in train),
            "holdout_starts_on_or_after_cutoff": all(date.fromisoformat(r["event_datetime"][:10]) >= cutoff for r in holdout),
            "all_model_ready_events_have_catalog_runtime": all(bool(r["runtime_seconds"]) for r in model_ready),
            "all_model_ready_events_have_valid_watch_seconds": all(parse_number(r["watch_seconds"]) is not None for r in model_ready),
            "all_model_ready_events_have_recomputed_completion": all(r["completion_recomputed"] == "True" for r in model_ready),
        },
        "assumptions": {
            "cutoff_date": cutoff.isoformat(),
            "max_watch_seconds_exclusive_upper_bound": max_watch_seconds,
            "completion_threshold": completion_threshold,
            "missing_watch_seconds": "exclude from model-ready data and retain in excluded_events.csv",
            "excessive_watch_seconds": "exclude values strictly greater than max_watch_seconds",
            "unmatched_title_ids": "exclude from model-ready data and retain in unmatched_titles.csv",
            "missing_catalog_runtime": "exclude because completed cannot be recomputed consistently from runtime",
            "completion_rule": "completed=True when watch_seconds >= runtime_seconds * completion_threshold",
        },
    }
    (output_dir / "cleaning_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_report_text(output_dir / "cleaning_report.txt", report)
    return report


def write_report_text(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "NETFLIX VIEWING EVENTS CLEANING REPORT",
        "======================================",
        f"Original input rows: {report['input_events']}",
        f"Exact duplicate rows removed: {report['exact_duplicate_rows_removed']}",
        f"Unique events after exact deduplication: {report['unique_events_after_exact_deduplication']}",
        f"Excluded unique events: {report['excluded_unique_events']}",
        f"Model-ready events: {report['model_ready_events']}",
        f"  Training events: {report['train_events']}",
        f"  Holdout events: {report['holdout_events']}",
        "",
        f"Cutoff date: {report['cutoff_date']}",
        f"Raw date partition before cleaning: train={report['raw_train_date_partition_count_before_cleaning']}, holdout={report['raw_holdout_date_partition_count_before_cleaning']}",
        f"Training date range: {report['train_date_range'][0]} to {report['train_date_range'][1]}",
        f"Holdout date range: {report['holdout_date_range'][0]} to {report['holdout_date_range'][1]}",
        "",
        f"Unmatched-title events: {report['unmatched_title_events']}",
        f"Unmatched title ID counts: {json.dumps(report['unmatched_title_id_counts'], sort_keys=True)}",
        f"Catalog titles missing usable runtime: {report['catalog_titles_missing_usable_runtime']}",
        f"Completion flags recomputed: {report['completion_flags_recomputed']}",
        f"Completion flags changed: {report['completion_flags_changed']}",
        "",
        "Exclusion reason counts (non-exclusive; one event may have multiple reasons):",
    ]
    for reason, count in sorted(report["exclusion_reason_counts_nonexclusive"].items()):
        lines.append(f"  {reason}: {count}")
    lines.extend(["", "Validation checks:"])
    for name, passed in report["validation"].items():
        lines.append(f"  {'PASS' if passed else 'FAIL'}: {name}")
    lines.extend([
        "",
        "Assumptions:",
        f"  - Exclude watch_seconds > {report['assumptions']['max_watch_seconds_exclusive_upper_bound']}.",
        f"  - Recompute completion at {report['assumptions']['completion_threshold']:.0%} of catalog runtime.",
        "  - Exclude events with missing duration, unmatched title IDs, or missing usable catalog runtime from model-ready data.",
        "  - Preserve excluded events in audit outputs; do not modify raw inputs.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=Path("data/viewing_events_raw.csv"))
    parser.add_argument("--catalog", type=Path, default=Path("data/title_catalog.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--cutoff-date", type=date.fromisoformat, default=date(2026, 11, 10))
    parser.add_argument("--max-watch-seconds", type=int, default=14_400)
    parser.add_argument("--completion-threshold", type=float, default=0.90)
    args = parser.parse_args()

    try:
        report = clean_events(
            events_path=args.events,
            catalog_path=args.catalog,
            output_dir=args.output_dir,
            cutoff=args.cutoff_date,
            max_watch_seconds=args.max_watch_seconds,
            completion_threshold=args.completion_threshold,
        )
    except (OSError, ValueError, AssertionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Cleaning completed successfully.")
    print(f"Input rows: {report['input_events']}")
    print(f"Model-ready rows: {report['model_ready_events']}")
    print(f"Excluded rows: {report['excluded_unique_events']}")
    print(f"Train: {report['train_events']} ({report['train_date_range'][0]} to {report['train_date_range'][1]})")
    print(f"Holdout: {report['holdout_events']} ({report['holdout_date_range'][0]} to {report['holdout_date_range'][1]})")
    print(f"Completion flags changed: {report['completion_flags_changed']}")
    print(f"Report: {args.output_dir / 'cleaning_report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
