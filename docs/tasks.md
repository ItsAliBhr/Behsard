# Task plan for Claude Code (in priority order)

Run each task in a **separate session**. Start each one in plan mode, review the plan, then let it implement. Commit after each task.

## 1. Scaffold + domain model  (small)
> Read CLAUDE.md and docs/problem-statement.fa.md. Scaffold the Python project (pyproject, src layout, pytest, config YAML, README). Implement `core/`: SQLite schema + repository layer for Worker, Stamp, Shift, Conveyor, Camera, ShiftAssignment, CartonEvent, RecognitionResult, ManualReview. Add a service for opening a shift and assigning worker↔stamp↔conveyor, plus a function that returns the active stamps for a conveyor at a timestamp. Tests included.

## 2. Synthetic data generator + evaluation harness  (medium)
> Implement `eval/`: a generator that renders stamps (letter + 2 digits, blue/navy ink, rubber-stamp look) on carton-like backgrounds (plain kraft, kraft with dark/yellow print, white with red print), with random rotation, blur, motion blur, faint/partial ink, and lighting variation. Write images + JSON labels. Add an evaluation script that runs any `Recognizer` over a labeled folder and reports accuracy, rejection rate and a confusion table.

## 3. StampRecognizer v1  (medium)
> Implement `recognition/` with the `Recognizer` interface and `StampRecognizer` as described in CLAUDE.md (blue HSV mask → regions → rotation search → Tesseract → fuzzy match to active stamps → confidence). Tune on synthetic data, then also run on the real sample images in tests/fixtures. Report results from the eval harness.

## 4. Offline video pipeline  (medium–large)
> Implement `tracking/` and a CLI that takes a video file + camera metadata, detects and tracks cartons (cartons touch, conveyor is stop-and-go), dedups, selects best frames, runs the recognizer and writes CartonEvents to the DB. Test on the sample videos (downscaled copies in fixtures). Output a per-stamp count summary.

## 5. Manual review UI  (medium)
> Implement `review/` with FastAPI + HTMX, Persian RTL UI: queue of unresolved CartonEvents showing the crop + full frame, keyboard-driven resolution against the active stamps of that shift. Corrections are saved as labels.

## 6. Shift admin + reports  (medium)
> Web pages to open/close a shift and assign worker/stamp/conveyor quickly; reports per worker/shift/day/conveyor with Jalali dates and Excel export.

## 7. Live capture service  (later — after camera tests)
> `capture/`: RTSP readers per camera with reconnect, on-disk queue, and a worker that feeds the pipeline.
