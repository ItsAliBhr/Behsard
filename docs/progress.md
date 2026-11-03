# Progress

Working through `docs/tasks.md` tasks 1–6 (task 7, live capture, skipped for now).
A new session should read this file first, then `CLAUDE.md`.

## Status

| # | Task | Status |
|---|------|--------|
| 1 | Scaffold + domain model | done |
| 2 | Synthetic data + eval harness | done |
| 3 | StampRecognizer v1 | done |
| 4 | Offline video pipeline | done (synthetic-validated; real handheld clips not trackable) |
| 5 | Manual review UI | todo |
| 6 | Shift admin + reports | todo |
| 7 | Live capture | skipped (after camera tests) |

## Environment notes

- Tesseract is a system binary: `apt-get install tesseract-ocr` (5.3.4 used).
- `pip install -e '.[dev]'`; tests: `python -m pytest`.
- Real samples: `tests/fixtures/images/` (3 video-player screenshots of A12 on
  kraft + 1 CCTV photo of A12 on a printed box), `tests/fixtures/videos/`
  (two phone videos, re-encoded to 640x352 @10 fps, no audio, ~2 MB total) and
  `tests/fixtures/frames/` (55 frames, 1 per second, hand-labeled: 28 with a
  visible A12, 27 without; `meta.background` = kraft / kraft_print / crate).
- Synthetic sets used for tuning/testing (not committed, regenerate):
  `behsard gen-synth --out /tmp/dev --n 300 --seed 100` (dev),
  `behsard gen-synth --out /tmp/test --n 500 --seed 200` (held-out test).
- Evaluate: `behsard eval --data <folder> --recognizer stamp --by background`.

## Task 1 — Scaffold + domain model

Plan:
- `pyproject.toml` (src layout, setuptools, pytest config), `config/behsard.yaml`,
  `behsard.config` loader, `behsard.cli` entry point.
- `core/models.py` dataclasses; `core/schema.sql`; `core/repository.py`
  (`Repository` protocol + `SqliteRepository`, all SQL in one place);
  `core/services.py` (`ShiftService`: open/close shift, assign
  worker↔stamp↔conveyor, copy assignments, `active_stamps(conveyor, ts)`).

Done: all of the above, 8 tests in `tests/test_core.py`.

## Task 2 — Synthetic data + eval harness

Plan:
- `eval/synth.py`: backgrounds (kraft, kraft with dark/yellow print, white
  with red print) + distractors (blue belt strip, carton seams); stamp text
  rendered with system bold sans fonts (PIL), rubber texture (speckle holes,
  uneven pressure), faint and partial variants, rotation (60% near
  0/90/180/270, 40% any), shear, multiply-blend navy/blue ink; camera effects
  (gain, colour cast, gradient light, gamma, blur, motion blur, noise, JPEG).
  Labels in `labels.json` with the active set (12–16 codes incl. one-char
  "confusers" such as A12/A17/B12) and meta (background, angle, faint, ...).
- `eval/harness.py`: `load_samples`, `evaluate(recognizer, samples)`,
  metrics + confusion + per-meta breakdown; `behsard gen-synth`, `behsard eval`.
- `recognition/base.py`: `Recognizer` protocol + `Result` (needed by eval).
- Real-image labels: `tests/fixtures/images/labels.json`.

Done: all of the above; tests in `tests/test_eval.py`.

Metric definitions (used for all numbers below):
- accuracy = stamped cartons auto-credited to the right stamp / stamped cartons
- rejection rate = cartons sent to manual review / all cartons
- error rate = wrongly auto-credited cartons / auto-accepted cartons
- top-1 = best candidate correct, ignoring the reject decision

## Task 3 — StampRecognizer v1

Plan: blue/ink mask → candidate regions → crop → rotation search → Tesseract →
fuzzy match to active stamps → confidence; tune on synthetic, check on real.

Done (`src/behsard/recognition/`):
- `ink.py`: ink map = pixel clearly darker **and** less yellow (Lab b*) than the
  local carton background (grey closing), OR saturated blue in HSV; long
  straight lines (seams, edges) removed. Candidates = collinear, similar-size,
  evenly spaced **triples** of character components (+ pairs / elongated
  singles as fallbacks), top `max_regions` (4).
- `stamp.py`: for each region, crop along the main axis (minAreaRect), try
  angle and angle+180 (and ±90 for squarish regions), binarise at ~40 px char
  height. Three evidence sources per variant, equal weights:
  Tesseract (psm 7, whitelist A-Z0-9, only on the 4 best variants) + fuzzy
  match, per-glyph template correlation (letter/digit by position), and
  whole-code template correlation (renders each active code). Per-stamp score
  = best over variants and images (+0.05 per extra agreeing image);
  confidence = 0.75·best + 0.75·margin; accept if ≥ `accept_threshold` (0.45)
  and margin ≥ `min_margin` (0.05).
- `match.py`: weighted Levenshtein with cheap OCR-confusion substitutions
  (0/O/D, 1/I/L/7, 2/Z, 5/S, 8/B, ...) and cheap deletion of thin glyphs
  ("1"/"I"), position coercion (letter + 2 digits), 3-char windows.
- `glyphs.py`: glyph and whole-code templates from system bold sans fonts.
- Registry names: `stamp` (default), `stamp-tesseract`, `stamp-glyphs`,
  plus weight ablations `stamp-w-*`.

Ideas not done yet (next steps for recognition):
- Learn templates from real stamp crops confirmed in manual review (the
  supervisor's corrections are stored as labels) — the real stamp font is the
  biggest unknown; this should lift accuracy a lot.
- Use more frames per carton (tracking gives 3); agreement bonus already exists.
- Restrict the search to a per-camera ROI (carton face) to cut clutter.
- Plastic crates with paper liner: out of scope for v1 (tagged `crate`).

## Task 4 — Offline video pipeline

Plan: per camera, track cartons by belt displacement + carton seams (not by
frames), pick best frames, pair L/R sightings into one CartonEvent, recognise
against the active stamps, write events + media; CLI with per-stamp summary.

Done:
- `tracking/tracker.py` `CartonTracker`:
  - belt shift per frame = phase correlation restricted to the columns that
    changed (so a partly visible carton is not swamped by static background);
    integrated into `offset`; conveyor coordinate `u = x - offset`.
  - carton edges = column peaks of |dI/dx| (40th percentile over the band, so
    stamps/prints don't count); tracked in `u`; **confirmed** only after ≥2
    consecutive-frame steps whose image motion equals the belt step (rejects
    static background and aliasing on periodic rollers/posts).
  - a confirmed edge crossing the counting line = boundary; consecutive
    boundaries with a plausible width (25%–160% of the view) = one carton.
  - frame buffer keyed by belt offset (≥2 px apart; while stopped only the
    sharpest is kept). Best frames = carton most centred, then sharpest;
    **cropped to the carton's own extent** (neighbouring cartons are in view
    and their stamps must not leak in). Full frames kept for review.
  - event time = moment the belt offset put the carton centre on the line
    (correct under stop-and-go).
  - `pair_sightings`: greedy time matching of L and R sightings within
    `pair_window_s`; unmatched sightings stay single (never double counted).
- `tracking/pipeline.py` `Pipeline.run(inputs, start)`: track → pair →
  `ShiftService.active_stamps` at event time → recognizer on all frames of
  both sides → `CartonEvent` (recognized / pending_review), media saved under
  `media/YYYYMMDD/conveyorN/` (paths stored relative to media dir),
  `RecognitionResult` for audit. No open shift → pending_review.
- `eval/synth_video.py` + `behsard gen-synth-video`: touching cartons,
  stop-and-go with ramps, motion blur on cartons, periodic static background,
  stamps on one side, mirrored right camera, `truth.json`.
- CLI: `behsard process-video --video V --camera ID [--video V2 --camera ID2]
  --start ISO [--conveyor N --side L]` prints the per-stamp summary.
- Tests: `tests/test_tracking.py`.

Results:
- Synthetic video pairs (seeds 0,1,2; 8 cartons each, stop-and-go): 24/24
  cartons counted, every event merged from both sides; one extra event
  (single spurious left-side sighting, seed 1). Recognition: 18/20 stamped
  cartons auto-credited, 2 to review, **0 wrong credits**; unstamped → review.
- Real sample videos (`IMG_5796`, `IMG_5797`): **0 cartons tracked.** These are
  handheld phone clips; the camera moves and pans, IMG_5797 looks along the
  conveyor in perspective, and kraft-to-kraft seams are weak/blurred. They do
  not match the planned fixed straight-on mounting, so they cannot validate
  the tracker; tried looser edge thresholds without success. **Next step:**
  record 1–2 minutes with the real mounted camera (fixed, manual exposure)
  and tune `TrackerParams` (`roi`, `edge_k`, `edge_rel`, `min_carton_frac`).
  The recognizer was evaluated on frames from these clips instead (Task 3).

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

- Negative (unstamped) samples are labeled `stamp: null`; 10% of synthetic data.
- Synthetic stamps are never drawn over the blue belt strip (a stamp is on
  the carton), but the belt is always a distractor for the blue mask.

- **Ink detection is relative, not just HSV-blue.** In the real phone footage
  the navy ink measures as neutral dark grey (HSV S≈30–55) on kraft, and the
  CCTV box has a navy *printed* graphic, so a strict blue mask both misses
  stamps and catches print. The conveyor belt is also blue. Relative
  darkness+blueness w.r.t. the local background, plus HSV blue, plus
  size/shape filtering and the closed-set reader, handles all of these.
- **Tesseract is not enough on its own** (it drops the thin "1": `A2`, `AIZ`),
  so two cheap closed-set template scorers were added; Tesseract stays as one
  of three evidence sources, limited to 4 calls per image for speed.
- `OMP_THREAD_LIMIT=1` for Tesseract (otherwise parallel workers thrash).
- Accept threshold 0.45 chosen from sweeps: keeps error ≤ 3% on synthetic dev
  and 0% on real frames. Wrong credits are worse than manual reviews.
- **Counting by seams in conveyor coordinates**, not by photo-eye or frame
  counting: cartons touch and stop, so only travelled distance + seam
  crossings are reliable. Carton width limits assume one carton is 25–160% of
  the view (40–50 cm carton in a ~1 m field of view at 45 cm).
- Default processing at 10 fps (all frames of 10 fps video); the edge
  confirmation needs small frame-to-frame motion.
- The real-frame test active set deliberately contains one-character
  confusers of A12 (A17, B12, K12, R12, H11, M21) to be pessimistic.

## Recognition accuracy

Metrics as defined in Task 2. `stamp` recognizer, accept_threshold 0.45.

| Dataset | n (stamped/none) | accuracy | rejection | error | top-1 | time/img |
|---|---|---|---|---|---|---|
| Real screenshots `fixtures/images` | 4 (4/0) | 100% | 0% | 0% | 100% | 1.5 s (1600 px) |
| Real video frames `fixtures/frames` | 55 (28/27) | 42.9% | 78.2% | 0% | 71.4% | 0.74 s |
| Synthetic test (seed 200) | 500 (450/50) | 58.0% | 47.6% | 0.4% | 72.0% | 0.82 s |

Synthetic test by background: kraft 73.8% acc, kraft_print 49.0%, white_red
49.6%; partial stamps 26.2%. Real frames by background: kraft 83% (5/6),
kraft_print 35% (6/17), crate 20% (1/5; out of scope).
No unstamped carton was ever auto-credited (0 false accepts on 77 negatives).
Real video frames are 640x352 phone footage (stamp chars ~12–20 px), much
lower resolution than the planned 3200x1800 cameras at 45 cm.
