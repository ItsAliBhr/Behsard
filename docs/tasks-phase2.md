# Phase 2: Field-ready pilot (live cameras + GUI + Windows package)

Goal: a product the operator can **download, double-click and run on a Windows laptop**, connect 1–2 Hikvision cameras, and test **in real time** on the real conveyor, while collecting the data needed for tuning.

Target machine: Windows 10/11 laptop, Core i5 class, no GPU, no internet at the site. Cameras: Hikvision DS-2CD1063G2-LIU (3200×1800, ≤20 fps, RTSP `/Streaming/Channels/101`, sub-stream `/102`, snapshot `/ISAPI/Streaming/channels/101/picture`).

Everything below must keep the hard constraints in CLAUDE.md: offline, on-site and pluggable recognizer.

---

## Task 7: Live capture service
- One reader process/thread per camera (OpenCV or PyAV over RTSP/TCP), with **automatic reconnect** (exponential backoff) and per-camera health status (connected, fps, last frame time, dropped frames).
- Frames feed the existing tracking pipeline in real time. Process at a configurable rate (default 10 fps). Downscale for tracking, but keep **full-resolution crops** for recognition.
- Persist every CartonEvent plus its best crops to disk/DB **immediately**. Nothing may be lost when the app is closed or a camera drops.
- Optional "record raw video" toggle per camera (segment files, e.g. 5 min each, with a disk-space guard).

**Acceptance:** with 2 simulated RTSP cameras at 3200×1800 and 10 fps, the app runs 2 hours with no memory growth >10%, recovers within 10 s from a killed stream, and CPU stays <70% on a 4-core i5.

## Task 8: Operator GUI (local web app at http://localhost:8000, opened automatically)
Persian RTL UI, Jalali dates, simple and large. Pages:

1. **Dashboard / live view:** live image per camera (MJPEG or WebSocket, reduced size), overlaid with the watch region, tracked carton boxes and the recognized stamp + confidence per carton. Big live counters per stamp/worker for the current shift. Camera health indicators.
2. **Camera setup wizard:**
   - Add a camera (IP, user, password, conveyor, side) → "Test connection" → snapshot.
   - Draw the **watch region (ROI)** and belt direction on the snapshot with the mouse.
   - **Image quality check** on the snapshot: sharpness score, brightness, and an estimate of stamp letter height in px when a stamped carton is held in view. Show pass/fail hints, e.g. "Image is blurry: check focus/distance" or "Too dark: add light".
   - **Motion blur check:** while a carton moves past, show a blur score with pass/fail.
   - Settings saved to config YAML. Passwords are stored locally, never logged.
3. **Shift page** (exists; integrate into the nav): open a shift, assign worker/stamp/conveyor fast.
4. **Review queue** (exists): link from the dashboard with a pending count badge.
5. **Reports** (exists).
6. **Test mode:** run without a shift. Every carton is recognized against a user-entered list of test stamps, and results are shown live with a running accuracy table. The operator presses keys to confirm/correct, which produces labeled field data.
7. **Replay mode:** select a recorded video file and run the same pipeline with the same overlays, for tuning offline.

**Acceptance:** Playwright smoke tests open every page, add a simulated camera, draw an ROI, open a shift, see live counts increase, resolve a review item and export Excel.

## Task 9: Field data collection + tuning loop
- Every carton event stores: full-res best frames from both sides, the crop, the recognizer candidates/scores and the final decision (auto or supervisor). Folder layout usable directly by `eval/`.
- "Export field dataset" button: zip of labeled crops + metadata to send back for tuning.
- Script: rebuild stamp templates from confirmed real crops, re-run eval and print the before/after accuracy table.
- Recognizer and tracker thresholds editable in the GUI (advanced page) with "restore defaults".

## Task 10: Windows packaging + one-click run
- GitHub Actions workflow on `windows-latest` that builds a **portable zip** (or PyInstaller one-folder build) containing Python runtime, all dependencies, a bundled **portable Tesseract** with its English data, ffmpeg, and `Behsard.exe` / `start.bat`. It must start the server and open the browser.
- Publish the zip as a **GitHub Release artifact** on each tag.
- First run creates the data folder and default config, and works fully **offline**.
- `README.fa.md`: install and run steps in Persian, with screenshots of each page.

**Acceptance:** the CI job downloads the built zip on a clean Windows runner, starts it, waits for `/health` = OK, runs the Playwright smoke test against it, and replays a synthetic video end-to-end with expected counts.

## Task 11: Test suite hardening
- **RTSP simulator:** use MediaMTX + ffmpeg in tests to serve synthetic conveyor videos as live RTSP cameras (looped), including a "camera drops for 20 s" scenario.
- **End-to-end tests:** synthetic 2-camera conveyor stream → expected per-stamp counts exactly, 0 double counts, 0 wrong credits.
- **Regression tests** on the real fixtures: the current accuracy numbers become minimum thresholds and must not drop.
- **Soak test** (optional/CI-nightly): 2 h simulated stream, checking memory, CPU and no lost events.
- **Performance test:** end-to-end latency from carton leaving the view to the event appearing in the GUI is under 5 s.
- All of the above run in CI. Docs list how to run them locally.

## Task 12: Field test protocol document (`docs/field-test.fa.md`, Persian)
Step-by-step for the operator: mount cameras (distance 45 cm, straight-on, light angle), connect laptop (static IP 192.168.1.100), camera web settings (Day mode, manual exposure 1/1000, WDR/anti-flicker/OSD off, smart light off, H.264, CBR max bitrate, 10–12 fps), run the setup wizard checks, run a 15-minute test-mode session with known stamps, record 2 min of raw video per camera, export the field dataset and send it back. Include a troubleshooting table (no image, blurry, dark, nothing tracked, many review items).

---

## Definition of done for Phase 2
1. Release zip downloads and runs on a clean Windows laptop with no internet.
2. Operator can add 2 cameras, see live overlays and counts, and run test mode.
3. All automated tests in CI are green, including the packaged-app smoke test.
4. `docs/field-test.fa.md` and `README.fa.md` are complete.
5. `docs/progress.md` updated with final numbers and known limitations.
