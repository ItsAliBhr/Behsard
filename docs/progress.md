# Progress

Working through `docs/tasks.md` tasks 1–6 (task 7, live capture, skipped for now).
A new session should read this file first, then `CLAUDE.md`.

## Status

| # | Task | Status |
|---|------|--------|
| 1 | Scaffold + domain model | done |
| 2 | Synthetic data + eval harness | todo |
| 3 | StampRecognizer v1 | todo |
| 4 | Offline video pipeline | todo |
| 5 | Manual review UI | todo |
| 6 | Shift admin + reports | todo |
| 7 | Live capture | skipped (after camera tests) |

## Environment notes

- Tesseract is a system binary: `apt-get install tesseract-ocr` (5.3.4 used).
- `pip install -e '.[dev]'`; tests: `python -m pytest`.
- Real samples: `tests/fixtures/images/` (3 video-player screenshots of A12 on
  kraft + 1 CCTV photo of A12 on a printed box) and `tests/fixtures/videos/`
  (two phone videos, re-encoded to 640x352 @10 fps, no audio, ~2 MB total).

## Task 1 — Scaffold + domain model

Plan:
- `pyproject.toml` (src layout, setuptools, pytest config), `config/behsard.yaml`,
  `behsard.config` loader, `behsard.cli` entry point.
- `core/models.py` dataclasses; `core/schema.sql`; `core/repository.py`
  (`Repository` protocol + `SqliteRepository`, all SQL in one place);
  `core/services.py` (`ShiftService`: open/close shift, assign
  worker↔stamp↔conveyor, copy assignments, `active_stamps(conveyor, ts)`).

Done: all of the above, 8 tests in `tests/test_core.py`.

## Decisions

- **Timestamps** are naive local site time stored as ISO strings. Single site,
  single time zone; avoids tz bookkeeping. Jalali conversion only in the UI.
- **Shift is global** (not per conveyor). Assignments carry the conveyor.
  A stamp and a worker may appear at most once per shift (DB unique keys).
  Moving a worker mid-shift = delete assignment and re-add.
- **Stamp code** is the generic worker token (`stamp.code`); a future printed
  label would reuse the same table, so counting/reports don't change.
- **CartonEvent.status** ∈ recognized / pending_review / resolved / no_stamp /
  not_carton. Only recognized and resolved are credited to workers.
- **Conveyors and cameras** are defined in YAML and synced into the DB
  (`behsard init-db`); workers, stamps and shifts live only in the DB.
- Camera credentials are not stored in config; they come from env vars.
- The originally committed `tests/fixtures/videos/IMG_5796.MOV` was a 2-byte
  placeholder; the user later uploaded the real videos, which replaced it as
  downscaled `.mp4` copies.

## Recognition accuracy

(not measured yet)
