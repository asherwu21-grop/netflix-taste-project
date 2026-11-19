# Submission note — Netflix viewing-history preparation

## Prompt excerpt (quoted verbatim)

> “Please create a reusable Python script called `clean_events.py` that cleans the viewing events, matches them against the catalog, and splits the data into training and holdout datasets.”

## Results and checks

- Input export: **5,000** viewing events; exact duplicate rows removed: **0**.
- Model-ready output: **4,602** events; excluded unique events: **398**. Every input row is accounted for in the model-ready or exclusion outputs, and `events_audit.csv` contains all 5,000 rows.
- Training: **4,265** events, **2026-08-17–2026-11-09**.
- Holdout: **337** events, **2026-11-10–2026-11-16**.
- The date cutoff is **2026-11-10**. No training event is on or after the cutoff; no holdout event is before it; event IDs do not overlap.
- All 4,602 model-ready events have a catalog match, usable runtime, valid watch duration, and recomputed completion value.
- Unmatched title IDs: **180** events. An additional **104** events match catalog titles whose runtime is missing, so they are retained in the audit but excluded because completion cannot be recomputed reliably.
- Completion values recomputed: **4,602**; values changed from the original flag: **1,817**.

The script ran successfully with Python's standard library only. Validation checks are recorded in `output/cleaning_report.txt` and `output/cleaning_report.json`.

## Cleaning assumptions

- Exclude missing watch durations and durations greater than 14,400 seconds.
- Recompute `completed` as true at 90% or more of catalog runtime, retaining the original value in `completed_original`.
- Do not guess unmatched titles. Exclude unmatched-title events and events with missing catalog runtime from model-ready output while preserving them in audit files.
- Split chronologically at November 10, 2026; do not randomly split or use holdout events for training.

## Suggested commit message

`Add reproducible Netflix viewing-event cleaning and chronological split`
