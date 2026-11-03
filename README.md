# Behsard carton counter

Automatic per-worker carton counting for the Behsard cold-storage sorting line,
by reading each worker's ink stamp (letter + two digits, e.g. `A12`) on cartons
with cameras. Runs fully on-site (no cloud, no GPU). See `CLAUDE.md` and
`docs/problem-statement.fa.md` for the full problem; `docs/progress.md` for the
current state.

## Install (offline-capable)

```bash
sudo apt-get install tesseract-ocr     # system binary
pip install -e '.[dev]'
```

## Usage

```bash
behsard init-db                         # create DB, sync conveyors/cameras from config
behsard serve                           # web UI: /shifts, /review, /reports (Persian, RTL)
behsard process-video --video left.mp4 --camera cam1L --video right.mp4 --camera cam1R \
    --start 2026-11-01T09:00:00         # offline pipeline -> DB, prints per-stamp counts
behsard gen-synth --out /tmp/synth --n 300 --seed 1      # synthetic stamp images
behsard eval --data tests/fixtures/frames --by background # evaluate a recognizer
behsard gen-synth-video --out /tmp/video --seed 0        # synthetic conveyor videos
behsard export-labels --out /tmp/labels # supervisor decisions as an eval dataset
python -m pytest                        # tests (~1 min)
```

Daily flow: open a shift and assign worker ↔ stamp ↔ conveyor on `/shifts`,
the pipeline credits recognised cartons automatically, the supervisor clears
the `/review` queue by keyboard, `/reports` shows counts (Excel export).

## Layout

```
config/behsard.yaml      single configuration file
src/behsard/core/        domain model, SQLite repository, shift service
src/behsard/eval/        synthetic data generator + evaluation harness
src/behsard/recognition/ Recognizer interface + StampRecognizer
src/behsard/tracking/    carton tracking in video, offline pipeline
src/behsard/review/      manual review logic
src/behsard/reports/     counts + Excel export
src/behsard/web/         FastAPI + HTMX UI (Persian, RTL)
tests/                   pytest; small fixtures in tests/fixtures/
```
