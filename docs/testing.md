# Testing

All tests: `python -m pytest` (about 6–8 minutes). Needs `tesseract` and, for
the network-camera tests, `ffmpeg` on PATH; browser tests need Playwright
(`pip install -e '.[dev]' && python -m playwright install chromium`).

| Test file | What | Notes |
|---|---|---|
| `tests/test_core.py`, `test_eval.py`, `test_recognition.py`, `test_review.py`, `test_reports_admin.py` | unit tests | fast |
| `tests/test_tracking.py`, `test_live.py` | offline pipeline, live service with paced video files | ~1.5 min |
| `tests/test_end_to_end.py` | synthetic 2-camera conveyor: exact per-stamp counts after review, 0 double counts, 0 wrong credits; live latency < 5 s | ~2 min |
| `tests/test_netcam.py` | simulated network cameras: HTTP (ffmpeg) 20 s outage → recovery < 10 s; RTSP via **MediaMTX** (2 synced cameras + drop) | RTSP test skipped if `mediamtx` is not installed |
| `tests/test_regression.py` | real fixtures: accuracy/top-1/error thresholds must not drop | `-m slow` |
| `tests/e2e/test_smoke.py` | Playwright: every page, add camera, ROI, shift, live counts, review, Excel | `-m e2e`; set `BEHSARD_URL` to test a running (packaged) app |
| `tests/test_soak.py` | 2 h, 2 cameras 3200×1800: memory growth < 10 %, CPU < 70 %, no lost events | only when `BEHSARD_SOAK_MINUTES` is set |

Selections: `python -m pytest -m "not slow and not e2e"` for a quick run.

Soak directly: `python scripts/soak.py --minutes 120`.
Replay check against a running app: `python scripts/replay_check.py http://127.0.0.1:8000 VIDEO_DIR`.

Simulated cameras for manual testing:
- add a camera with a video file path as URL (camera wizard → «آدرس دلخواه یا فایل ویدیو»);
- or serve a video as a network camera: `python -c "from behsard.sim.netcam import HttpCamera; import time; c=HttpCamera('left.mp4', port=8090).start(); print(c.url); time.sleep(1e9)"`.

CI: `.github/workflows/tests.yml` (Ubuntu, every push + nightly soak),
`.github/workflows/windows.yml` (Windows build, packaged-app acceptance, release on `v*` tags).
