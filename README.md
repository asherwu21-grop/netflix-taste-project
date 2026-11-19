# Netflix Taste Project — Viewing History Preparation

This repository prepares the August 17–November 16, 2026 viewing-events export for a chronological taste-model experiment. It joins events to the November 2026 title catalog, applies documented quality rules, and creates training and holdout files.

## Run the pipeline

Requires Python 3.10 or newer. The script uses only the Python standard library; no package installation is required.

From this repository's root directory:

```bash
python clean_events.py
```

Optional settings:

```bash
python clean_events.py \
  --events data/viewing_events_raw.csv \
  --catalog data/title_catalog.csv \
  --output-dir output \
  --cutoff-date 2026-11-10 \
  --max-watch-seconds 14400 \
  --completion-threshold 0.90
```

The script never modifies either input file.

## Cleaning rules

- Exact duplicate rows are removed after retaining the first occurrence. Conflicting records that reuse an `event_id` are excluded rather than choosing one arbitrarily.
- Event IDs and title IDs are trimmed; title IDs are uppercased before catalog matching. Event dates are normalized to ISO format.
- Events with missing, invalid, or non-positive watch duration are excluded from model-ready data and retained in the audit output.
- Events with `watch_seconds` strictly greater than 14,400 (four hours) are excluded from model-ready data and retained in the audit output.
- The original completion value is retained in `completed_original`. For eligible events, `completed` is recomputed as `True` when `watch_seconds` is at least 90% of the matched catalog runtime, and `False` otherwise. This 90% threshold is a project assumption, not a fact supplied by Netflix.
- Unmatched title IDs are not guessed. Those events are excluded from model-ready data and saved in `unmatched_titles.csv`.
- **Additional catalog issue discovered during implementation:** 10 catalog titles have blank or unusable `runtime_minutes`; 104 events refer to those titles. Since `completed` is being recomputed from runtime, those events are excluded from model-ready data and preserved in the audit. This avoids silently retaining un-recomputed completion labels.
- Missing `device_type` and `country` values are represented as `Unknown`. Other original event fields are preserved.
- The date split is chronological: dates before November 10, 2026 go to training; November 10 onward goes to holdout.

## Outputs

| File | Purpose |
|---|---|
| `output/train.csv` | Eligible events before the cutoff |
| `output/holdout.csv` | Eligible events on/after the cutoff |
| `output/excluded_events.csv` | Each excluded unique event once, with one or more exclusion reasons |
| `output/unmatched_titles.csv` | All events whose title IDs were not found in the catalog |
| `output/events_audit.csv` | One audit row for every original input row, including duplicate disposition |
| `output/duplicates_removed.csv` | Exact duplicate rows removed (header-only when none exist) |
| `output/cleaning_report.txt` | Human-readable summary and validation results |
| `output/cleaning_report.json` | Machine-readable report |

Exclusion-reason counts in the report are **non-exclusive**: an event with more than one issue appears under each applicable reason, but appears only once in `excluded_events.csv`.

## Results from the supplied files

- Original viewing events: **5,000**
- Exact duplicate rows removed: **0**
- Unique events excluded from model-ready data: **398**
- Model-ready events: **4,602**
- Training events: **4,265**, dated **2026-08-17 through 2026-11-09**
- Holdout events: **337**, dated **2026-11-10 through 2026-11-16**
- Unmatched-title events: **180** (`TT-0347`: 61; `TT-0412`: 65; `TT-0588`: 54)
- Completion labels recomputed: **4,602**; labels differing from the original value: **1,817**

The raw date-only partition was 4,622 events before the cutoff and 378 events on/after the cutoff. The smaller train/holdout counts above are after excluding ineligible events.

The run report records the exact exclusions and validation results. All 5,000 input rows are accounted for as 4,602 model-ready events plus 398 excluded unique events; the audit file also retains all 5,000 original rows.

## Privacy

The raw export contains member identifiers and viewing-history information. Keep this repository **private** unless you have explicit authorization to publish the data. Do not put these files in a public repository.
