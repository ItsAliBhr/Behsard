# Behsard Carton Counter

Automatic per-worker carton counting for the Behsard cold-storage apple sorting line, by reading each worker's ink stamp on cartons with cameras. Full problem statement (Persian): `docs/problem-statement.fa.md` — read it before any design work.

## The problem in brief

- A sorting machine feeds **4 output conveyors**. On each conveyor, 6–8 workers per side fill cartons.
- Each worker gets a rubber stamp per shift: **one uppercase letter + two digits** (e.g. `A12`, `B07`). After filling a carton, the worker stamps it **on the carton face on their own side**.
- Goal: count cartons per worker per shift/day/conveyor automatically, with reports.
- Workers change daily. At shift start, a supervisor assigns **worker ↔ stamp ↔ conveyor**. The recognizer only has to choose among the stamps **active on that conveyor in that shift** (closed-set classification, not free OCR).

## Hard constraints

- No extra action for workers (no buttons, scanning, labels).
- No new staff. Unrecognized cartons go to a **manual review queue** handled by the existing supervisor, so the review UI must be very fast (keyboard-driven).
- **Everything runs on-site.** No cloud, no external APIs, no network calls at runtime. Images are queued and processed locally.
- Low hardware cost: one ordinary PC, no GPU assumed.
- The identification method must be **pluggable** (stamp today, maybe printed label later) without changing the data model, counting or reports.

## Physical setup (pilot)

- **2 cameras per conveyor**, one per side, mounted straight-on to the carton face, ~45 cm away, on a straight roller section.
- Camera: Hikvision DS-2CD1063G2-LIU, 2.8 mm lens, 3200×1800, up to 20 fps, RTSP: `rtsp://<user>:<pass>@<ip>:554/Streaming/Channels/101`. Snapshot: `http://<ip>/ISAPI/Streaming/channels/101/picture`.
- Planned settings: fixed Day (color) mode, manual exposure ~1/1000 s, WDR/anti-flicker off, own flicker-free LED light, ~10–12 fps.
- Conveyor speed ≈ 0.2–0.3 m/s, roughly one carton every 1.5–3 s, **stop-and-go**. **Cartons touch each other** (no gaps), so a simple photo-eye cannot separate them, and a stopped carton stays in view for many seconds. Counting must be done by **tracking cartons in video and deduplicating**, not by counting frames.
- Each carton is seen by both side cameras, but its stamp is normally on one side only. The two views must be merged into **one carton event** so nothing is counted twice.

## Visual facts from sample footage

- Stamp ink is **blue/navy**. Carton prints are brown/black, yellow and red, never blue. **A blue-ink color mask (HSV) is the primary way to locate the stamp.**
- Stamps appear at any rotation (0/90/180/270 and oblique), sometimes faint or partial, and sometimes overlapping printed graphics.
- Carton types: plain brown kraft, brown with print ("Golden Apple"), white with red print ("Red Apple"), white plastic crates with a paper liner (hardest case, out of scope for v1).

## Architecture (target)

Python 3.11+. Modules:

1. `capture/`: one RTSP reader per camera, saving frames plus metadata (camera_id, conveyor_id, side, timestamp) to a local on-disk queue. Must survive restarts and camera disconnects without losing data.
2. `tracking/`: carton passage detection and tracking on one camera's stream. Emits one `CartonSighting` per physical carton with its best candidate frames. Pairs sightings from the two sides of the same conveyor into one `CartonEvent`.
3. `recognition/`: a `Recognizer` interface (`recognize(images, active_stamps) -> Result(stamp|None, confidence, crop)`). First implementation, `StampRecognizer`:
   blue mask → candidate regions → crop → rotation search → Tesseract (whitelist `A-Z0-9`, single line) → fuzzy match against the active stamps for that conveyor and shift → confidence. Below a threshold, the carton goes to manual review.
4. `core/`: domain model and DB (SQLite for the pilot, behind a repository layer so it can move to Postgres or Oracle). Entities: Worker, Stamp, Shift, Conveyor (with per-conveyor coefficient), Camera, ShiftAssignment, CartonEvent, RecognitionResult, ManualReview.
5. `review/`: web UI listing unresolved cartons with the stamp crop and full image. The supervisor picks the stamp by keyboard (number keys or typing), or marks "no stamp" or "not a carton". Every correction is stored and becomes labeled training/evaluation data.
6. `reports/`: counts per worker / shift / day / conveyor, with Excel export.
7. `eval/`: offline evaluation over labeled images and videos (accuracy, rejection rate, confusion), plus a **synthetic data generator** that renders stamps onto carton textures with rotation, blur, faint ink, partial prints and printed backgrounds.

Web stack: FastAPI + server-rendered templates (HTMX). Keep the frontend simple.

## Conventions

- Code, identifiers and comments in English. UI text in **Persian (RTL)**, with Jalali dates in the UI.
- Type hints everywhere. pytest for tests. Keep tests fast and do not commit large media; put small fixtures in `tests/fixtures/`.
- No runtime network access. All dependencies must install offline-capably via pip (`opencv-python`, `pytesseract`, `numpy`, `fastapi`, ...). Tesseract is a system binary.
- Configuration (cameras, thresholds, paths) in one YAML file. No secrets in the repo.
- Small, reviewable commits.
