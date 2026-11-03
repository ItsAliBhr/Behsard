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
python -m pytest                        # tests
```

More commands (`gen-synth`, `eval`, `process-video`, `serve`) are added per
task; run `behsard --help`.

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
