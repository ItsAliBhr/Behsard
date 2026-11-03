# Progress

Phase 1: `docs/tasks.md` tasks 1–6 done. Phase 2: `docs/tasks-phase2.md`
tasks 7–12 (order of work: 7, 8, 10, then 11, 12, 9).
A new session should read this file first, then `CLAUDE.md`.

## Status

| # | Task | Status |
|---|------|--------|
| 1 | Scaffold + domain model | done |
| 2 | Synthetic data + eval harness | done |
| 3 | StampRecognizer v1 | done |
| 4 | Offline video pipeline | done (synthetic-validated; real handheld clips not trackable) |
| 5 | Manual review UI | done |
| 6 | Shift admin + reports | done |
| 7 | Live capture | done (phase 2) |
| 8 | Operator GUI | done |
| 9 | Field data + tuning loop | todo |
| 10 | Windows packaging | todo |
| 11 | Test suite hardening | todo |
| 12 | Field test protocol (fa) | todo |

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

## Task 5 — Manual review UI

Plan: FastAPI + HTMX, Persian RTL, queue of pending cartons with stamp crop +
full frame, keyboard-driven resolution against the active stamps of the
carton's conveyor and shift; every decision stored as a label.

Done:
- `review/service.py`: `ReviewService` (queue, `candidates(ev)` = active stamps
  of that conveyor at that time ordered by recognizer score, `resolve`),
  `reopen` (undo), `export_labels` → eval-format dataset (`behsard
  export-labels --out DIR`) using the exact carton crops the recognizer saw
  (`*_carton{i}.jpg`, now saved by the pipeline).
- `web/app.py` (`create_app`), `web/review_routes.py`, templates `base.html`,
  `review.html`, `_review_card.html`; `/media` serves saved frames;
  `htmx.min.js` 1.9.12 vendored in `web/static` (no CDN at runtime).
- Keyboard: `1`–`9` pick the n-th candidate, type a code (`A12`) + `Enter`,
  `0` = no stamp, `.` = not a carton, `Space` = skip, `Backspace`/`Esc` edit;
  uses `event.code` so it works with the Persian keyboard layout; Persian
  digits typed are normalised. After each decision the next card is swapped
  in with an undo button.
- Jalali dates and Persian digits via `web/fa.py` (`jdatetime`).
- `behsard serve` starts the UI (config `review.host/port`).
- Verified in headless Chromium on a demo DB built from a synthetic video
  (screenshot: `docs/review-ui.png`); tests in `tests/test_review.py`.

## Task 6 — Shift admin + reports

Plan: pages to open/close a shift and assign worker/stamp/conveyor quickly;
reports per worker/shift/day/conveyor with Jalali dates and Excel export.

Done:
- `/shifts`: open a shift (name preset صبح/عصر/شب, optional copy of a previous
  shift's assignments), list, close. `/shifts/{id}`: one-line keyboard form
  (worker with autocomplete — new names create the worker —, stamp code,
  conveyor; Enter adds; focus returns to the worker field; conveyor kept),
  assignments grouped per conveyor with coefficient, delete, copy-from-shift.
  Errors in Persian (duplicate stamp/worker, bad code, closed shift).
- `reports/counts.py`: `build_report(repo, start, end)` over credited cartons
  (recognized + resolved), groupings worker / shift / day / conveyor / detail,
  `adjusted` = count × conveyor coefficient, pending-review count shown as a
  warning (not yet counted).
- `reports/excel.py`: RTL workbook with sheets per grouping, per-carton detail
  and an info sheet; Jalali dates.
- `/reports` with Jalali date inputs (Persian or ASCII digits), presets
  (today, yesterday, 7/30 days), grouping select, Excel download.
- Verified in headless Chromium (screenshots `docs/shift-ui.png`,
  `docs/reports-ui.png`); tests in `tests/test_reports_admin.py`.

## What is left / next steps

1. **Task 7 (live capture)** — skipped as instructed: RTSP readers with
   reconnect, on-disk frame queue, worker feeding `Pipeline`.
2. **Tune tracking on real mounted-camera footage** (see Task 4 results).
3. **Recognition:** learn templates from supervisor-confirmed crops
   (`behsard export-labels`), per-camera ROI, then re-run `behsard eval`.
4. Plastic crates with paper liner (out of v1 scope).
5. Login/roles for the web UI if more than one supervisor uses it.

## Task 7 — Live capture service (phase 2)

Plan: reader thread per camera (OpenCV/FFmpeg, RTSP over TCP) with reconnect
and health; worker thread per camera running the tracker; online L/R merge;
single recognition thread persisting each carton immediately; optional
ffmpeg stream-copy recording with a disk guard.

Done (`src/behsard/live/`):
- `reader.py` `CameraReader`: RTSP or video file (simulated camera / replay,
  paced in real time, optional `speed` and `loop`); exponential backoff
  0.5→4 s (a returning camera is picked up in ≤ ~5 s); throttles to
  `process_fps` by grabbing and dropping extra frames; health (state, fps in/out,
  last frame age, reconnects, resolution). `redact()` hides passwords.
- `worker.py` `CameraWorker`: queue of 2 (newest wins, drops counted), runs
  `CartonTracker`, keeps the latest frame + overlay state, renders the
  annotated live JPEG (ROI, counting line, edges, last stamp + confidence).
- `service.py` `LiveService`: starts cameras from config, pairs L/R sightings
  online (held up to `pair_window_s` + 0.5 s, then released single), one
  recognition thread → `Pipeline.process_carton` (DB row + media written at
  once), recent-events ring buffer with latency, session counters, test mode
  (`mode="test"` events vs a stamp list), replay cameras (`mode="replay"`).
  `stop()` flushes held sightings and drains the recognition queue.
- `recorder.py`: ffmpeg `-c copy` segment recording (5 min files), stops
  when free disk < `live.min_free_gb`.
- Tracker frame buffer now stores **JPEG-encoded** frames (≤48, full
  resolution by default): raw 3200×1800 frames are 17 MB each.
- `SqliteRepository` is now guarded by an `RLock` (web + live threads).
- `carton_event.mode` column (prod/test/replay, auto-migrated): reports,
  review queue and status counters only use `prod`.
- Recognizer stops early once confident (≥0.8); pipeline interleaves L/R
  frames so the stamped side is read first.
- Config gains `live`, `test_mode`, more `tracking` keys, per-camera `url`
  or `host/user/password/channel`, `roi`, `record`; `save_config()`;
  relative paths resolve against the data home (folder above `config/`).
- `runtime.py`: finds bundled `tools/tesseract` and `tools/ffmpeg` (Windows).
- Tests: `tests/test_live.py` (reconnect, throttling, 2-camera end-to-end,
  test mode isolation, disk guard, config round-trip).

Measured here (4-core container, 2 simulated cameras 3200×1800 H.264 @10 fps,
5 min): RSS plateaus at 1.1–1.2 GB after warm-up (no upward trend), CPU ≈ 35 %
of the machine, 10 fps processed per camera, tracking ≈ 20 ms/frame, 53
cartons. Synthetic live run: 8/8 cartons, all L+R merged, 0 wrong credits;
latency from carton centre on the line to stored event 1–4.7 s at 2× speed.
The 2-hour / i5 acceptance run is part of Task 11's soak test.

## Task 8 — Operator GUI

Plan: extend the FastAPI/HTMX app into the operator console: dashboard with
live MJPEG + overlays + counters + health, camera wizard (connect, snapshot,
quality checks, ROI drawing, motion blur), test mode, replay; launcher that
opens the browser; Playwright smoke test.

Done:
- `/` dashboard: MJPEG per camera (`/live/{id}.mjpg`, 4 fps, 960 px) with ROI,
  counting line, carton edges (green = confirmed), last stamp + confidence;
  HTMX panel every 2 s: open-shift counters per stamp/worker/conveyor, pending
  count, camera health table, last cartons with latency. Nav badge shows
  pending reviews on every page.
- `/cameras` wizard: add/edit (Hikvision IP+user+password+channel 101/102, or
  any RTSP URL / video file as a simulated camera), test connection →
  snapshot + checks (sharpness, brightness, stamp letter height with Persian
  pass/fail hints), ROI drawn with the mouse on the snapshot + belt direction,
  motion-blur check from frames seen while moving, per-camera recording
  toggle, delete. Saved to the YAML config; camera restarts with new settings.
  Password is never sent back to the browser or logged.
- `/test`: test stamp list + on/off; cartons recognised against the list
  (`mode=test`), keyboard confirm/correct card (Enter on empty input = accept
  the system's guess), live accuracy table (accuracy / rejection / error, per
  stamp, unstamped).
- `/replay`: upload or pick a video, conveyor/side/speed → runs as a replay
  camera through the same live pipeline and dashboard (`mode=replay`).
- `/health`, `/api/status`, `/api/events` (JSON for launcher, CI and tests).
- `behsard.launcher` (`behsard-app`): data home, default config on first run,
  rotating log `data/logs/behsard.log`, server + browser; reuses a running
  instance. `behsard serve --open`.
- Keyboard JS moved to `web/static/review.js` (shared by review and test).
- Tests: `tests/e2e/test_smoke.py` (Playwright, Python): every page, add a
  simulated camera, test connection, draw ROI with the mouse, open shift and
  assign, live counts increase, resolve a review item by keyboard, Excel
  download. `scripts/screenshots.py` regenerates `docs/screenshots/`.

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
- **Review candidates are restricted** to stamps active on the carton's
  conveyor at its time (closed set); a stamp not assigned there cannot be
  chosen — the supervisor fixes the shift assignment first, or marks no stamp.
- No login in the pilot: the reviewer is recorded as "supervisor".
- Async route handlers share the single SQLite connection on the event loop
  thread (single supervisor, small queries) — no extra locking needed.
- **Live timestamps are wall-clock** at frame read time (also for simulated
  and replayed files), so pairing and latency work the same everywhere.
- **Buffered frames are JPEG (q92)**: memory bound with full-res crops kept.
- **One recognition thread**: cartons arrive every 1.5–3 s per conveyor and
  recognition stops early when confident; Tesseract runs single-threaded.
- **Recording = ffmpeg stream copy** (no re-encode, negligible CPU); only for
  RTSP cameras.
- **Test-mode/replay events are stored with `mode`** instead of in a separate
  DB, so the same review/label tools work, but production numbers stay clean.
- Camera passwords are stored in the local config file (data folder on the
  site laptop, not the repo) and never logged (`redact`).
- **Live view = MJPEG** (plain `<img>`, no JS client) at 4 fps / 960 px.
- **Simulated camera = a video file path in the camera URL** (paced in real
  time, looped); same code path as RTSP.
- Unpaired sightings wait `pair_window_s` + 3 s (wall clock) for the other
  camera before being stored alone, so a lagging camera under CPU load does
  not split one carton into two events.
- Browser tests use Python Playwright so the same test runs on the Windows CI
  runner against the packaged app (`BEHSARD_URL`).
- **Work day = shift start date**, so a night shift crossing midnight is
  reported on one day; cartons without a shift use their own date.
- Reports count only credited cartons (recognized + resolved); pending ones
  are shown as a warning so incomplete numbers are visible.
- Per-conveyor coefficient is applied as a separate "adjusted" column; raw
  counts are never altered.
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
