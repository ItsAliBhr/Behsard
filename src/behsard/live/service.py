"""LiveService: cameras -> tracking -> L/R merge -> recognition -> DB, in real time.

Threads:
* one :class:`CameraReader` per camera (network/file I/O, reconnects);
* one :class:`CameraWorker` per camera (tracking, bounded queue);
* one recognition thread (Tesseract is the slow part; cartons arrive every
  1.5-3 s per conveyor, so one thread keeps up);
* one housekeeping thread that releases unpaired sightings after the pairing
  window and checks recordings' disk space.

Every carton is written to the DB and disk by ``Pipeline.process_carton``
as soon as it is recognised, so closing the app or losing a camera loses at
most the carton currently in view.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from behsard.config import Config
from behsard.core.models import CartonEvent, CartonStatus
from behsard.core.repository import Repository
from behsard.core.services import ShiftService, normalize_stamp_code
from behsard.live.reader import CameraReader, redact, rtsp_url
from behsard.live.recorder import Recorder
from behsard.live.worker import CameraWorker
from behsard.recognition.base import Recognizer
from behsard.tracking.pipeline import Pipeline
from behsard.tracking.tracker import CartonSighting, TrackerParams

log = logging.getLogger(__name__)


def camera_url(cam: dict[str, Any]) -> str:
    """URL for a camera entry: explicit ``url`` (RTSP or video file) or host."""
    if cam.get("url"):
        return str(cam["url"])
    return rtsp_url(
        str(cam.get("host", "")),
        str(cam.get("user", "")),
        str(cam.get("password", "")),
        str(cam.get("channel", "101")),
        int(cam.get("port", 554)),
    )


def tracker_params(cfg: Config, cam: dict[str, Any]) -> TrackerParams:
    t = cfg.tracking
    roi = cam.get("roi") or t.roi
    return TrackerParams(
        roi=tuple(float(v) for v in roi),  # type: ignore[arg-type]
        work_width=t.work_width,
        edge_k=t.edge_k,
        edge_rel=t.edge_rel,
        min_carton_frac=t.min_carton_frac,
        max_carton_frac=t.max_carton_frac,
        frames_per_sighting=t.frames_per_sighting,
        store_max_side=t.store_max_side,
    )


@dataclass
class _Held:
    sighting: CartonSighting
    arrived: float  # wall clock


@dataclass
class LiveCamera:
    cfg: dict[str, Any]
    reader: CameraReader
    worker: CameraWorker
    replay: bool = False
    recorder: Recorder | None = None

    @property
    def id(self) -> str:
        return str(self.cfg["id"])


@dataclass
class RecentEvent:
    event_id: int
    camera_ids: list[str]
    conveyor_id: int
    label: str
    confidence: float
    status: str
    mode: str
    at: float
    latency_s: float  # carton left the counting line -> event stored


@dataclass
class LiveStats:
    cartons: int = 0
    by_stamp: Counter[str] = field(default_factory=Counter)
    pending: int = 0


class LiveService:
    def __init__(
        self,
        cfg: Config,
        repo: Repository,
        recognizer: Recognizer,
        on_event: Callable[[CartonEvent], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.pipeline = Pipeline(
            repo,
            recognizer,
            cfg.media_dir,
            pair_window_s=cfg.tracking.pair_window_s,
        )
        self.shifts = ShiftService(repo)
        self.cameras: dict[str, LiveCamera] = {}
        self.on_event = on_event
        self.recent: deque[RecentEvent] = deque(maxlen=200)
        self.session = LiveStats()  # since start (all modes)
        self._held: dict[int, list[_Held]] = {}
        self._held_lock = threading.Lock()
        self._rec_q: queue.Queue[tuple[int, CartonSighting | None, CartonSighting | None]] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started = False

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self.started:
            return
        self._stop.clear()
        for target, name in ((self._recognition_loop, "recognition"), (self._housekeeping, "housekeeping")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        for cam in self.cfg.cameras:
            if cam.get("enabled", True):
                self.start_camera(cam)
        self.started = True

    def stop(self) -> None:
        for cid in list(self.cameras):
            self.stop_camera(cid)
        # flush cartons that are waiting for their partner, then let the
        # recognition thread finish the queue so nothing is lost on exit
        self._flush_held(force=True)
        deadline = time.time() + 30
        while not self._rec_q.empty() and time.time() < deadline:
            time.sleep(0.05)
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        self.started = False

    def start_camera(self, cam: dict[str, Any], replay: bool = False) -> LiveCamera:
        cid = str(cam["id"])
        if cid in self.cameras:
            self.stop_camera(cid)
        worker = CameraWorker(
            cid, str(cam.get("side", "L")).upper(), int(cam.get("conveyor_id", 1)),
            tracker_params(self.cfg, cam), self._on_sighting,
        )
        url = camera_url(cam)
        reader = CameraReader(
            cid, url, worker.submit,
            process_fps=float(cam.get("process_fps", self.cfg.tracking.process_fps)),
            loop=bool(cam.get("loop", not replay)),
            realtime=bool(cam.get("realtime", True)),
            speed=float(cam.get("speed", 1.0)),
        )
        lc = LiveCamera(cam, reader, worker, replay=replay)
        if cam.get("record") and url.startswith("rtsp"):
            lc.recorder = Recorder(
                cid, url, self.cfg.resolve(self.cfg.live.record_dir),
                self.cfg.live.segment_minutes, self.cfg.live.min_free_gb,
            )
            lc.recorder.start()
        worker.start()
        reader.start()
        self.cameras[cid] = lc
        log.info("camera %s started: %s", cid, redact(url))
        return lc

    def stop_camera(self, camera_id: str) -> None:
        lc = self.cameras.pop(camera_id, None)
        if lc is None:
            return
        lc.reader.stop()
        lc.reader.join(timeout=10)
        # let the worker drain its queue before stopping
        deadline = time.time() + 5
        while not lc.worker.q.empty() and time.time() < deadline:
            time.sleep(0.02)
        lc.worker.stop()
        lc.worker.join(timeout=5)
        if lc.recorder:
            lc.recorder.stop()

    def set_recording(self, camera_id: str, on: bool) -> bool:
        lc = self.cameras.get(camera_id)
        if lc is None:
            return False
        url = camera_url(lc.cfg)
        if on and lc.recorder is None and url.startswith("rtsp"):
            lc.recorder = Recorder(
                camera_id, url, self.cfg.resolve(self.cfg.live.record_dir),
                self.cfg.live.segment_minutes, self.cfg.live.min_free_gb,
            )
            lc.recorder.start()
        elif not on and lc.recorder is not None:
            lc.recorder.stop()
            lc.recorder = None
        lc.cfg["record"] = on
        return True

    # ---- sightings -> cartons -----------------------------------------
    def _on_sighting(self, s: CartonSighting) -> None:
        conveyor = int(s.meta.get("conveyor_id", 1))
        side = s.meta.get("side")
        window = self.cfg.tracking.pair_window_s
        with self._held_lock:
            held = self._held.setdefault(conveyor, [])
            partner = None
            for h in held:
                if h.sighting.meta.get("side") != side and abs(
                    (h.sighting.ts - s.ts).total_seconds()
                ) <= window:
                    if partner is None or abs((h.sighting.ts - s.ts).total_seconds()) < abs(
                        (partner.sighting.ts - s.ts).total_seconds()
                    ):
                        partner = h
            if partner is not None:
                held.remove(partner)
                pair = (partner.sighting, s) if side == "R" else (s, partner.sighting)
                self._rec_q.put((conveyor, pair[0], pair[1]))
                return
            # single-camera conveyor: no partner will ever come
            sides = {
                str(c.cfg.get("side", "L")).upper()
                for c in self.cameras.values()
                if int(c.cfg.get("conveyor_id", 1)) == conveyor
            }
            if len(sides) < 2:
                self._rec_q.put((conveyor, s, None) if side != "R" else (conveyor, None, s))
                return
            held.append(_Held(s, time.time()))

    def _flush_held(self, force: bool = False) -> None:
        window = self.cfg.tracking.pair_window_s
        now = time.time()
        with self._held_lock:
            for conveyor, held in self._held.items():
                for h in list(held):
                    if force or now - h.arrived > window + 0.5:
                        held.remove(h)
                        s = h.sighting
                        item = (conveyor, s, None) if s.meta.get("side") != "R" else (conveyor, None, s)
                        self._rec_q.put(item)

    def _housekeeping(self) -> None:
        while not self._stop.wait(0.2):
            self._flush_held()
            for lc in list(self.cameras.values()):
                if lc.recorder:
                    lc.recorder.check_disk()

    def _mode_for(self, sightings: list[CartonSighting]) -> tuple[str, dict[str, int] | None]:
        tm = self.cfg.test_mode
        if tm.enabled and tm.stamps:
            ids = {}
            for code in tm.stamps:
                try:
                    st = self.shifts.ensure_stamp(code)
                except ValueError:
                    continue
                assert st.id is not None
                ids[st.code] = st.id
            return "test", ids
        replay = any(self.cameras.get(s.camera_id) and self.cameras[s.camera_id].replay for s in sightings)
        return ("replay" if replay else "prod"), None

    def _recognition_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conveyor, left, right = self._rec_q.get(timeout=0.2)
            except queue.Empty:
                continue
            seen = [s for s in (left, right) if s is not None]
            try:
                mode, test_ids = self._mode_for(seen)
                ev = self.pipeline.process_carton(conveyor, left, right, mode=mode, test_stamps=test_ids)
            except Exception:
                log.exception("recognition failed for a carton on conveyor %s", conveyor)
                continue
            self._after_event(ev, seen)
            self._rec_q.task_done()

    def _after_event(self, ev: CartonEvent, seen: list[CartonSighting]) -> None:
        assert ev.id is not None
        code = ev.meta.get("stamp_code")
        label = str(code) if code else "؟"
        left_line = max(s.ts for s in seen)  # carton centre crossed the line
        latency = max(0.0, time.time() - left_line.timestamp())
        self.recent.appendleft(
            RecentEvent(
                ev.id, [s.camera_id for s in seen], ev.conveyor_id, label, ev.confidence,
                str(ev.status), ev.mode, time.time(), round(latency, 2),
            )
        )
        self.session.cartons += 1
        if ev.status == CartonStatus.RECOGNIZED and code:
            self.session.by_stamp[str(code)] += 1
        else:
            self.session.pending += 1
        for s in seen:
            lc = self.cameras.get(s.camera_id)
            if lc:
                lc.worker.last_label = (label, ev.confidence, time.time())
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                log.exception("on_event callback failed")

    # ---- status --------------------------------------------------------
    def health(self) -> dict[str, dict[str, Any]]:
        out = {}
        for cid, lc in self.cameras.items():
            h = lc.reader.health.as_dict()
            h.update(
                {
                    "conveyor_id": lc.cfg.get("conveyor_id"),
                    "side": lc.cfg.get("side"),
                    "replay": lc.replay,
                    "dropped_processing": lc.worker.dropped,
                    "track_ms": round(lc.worker.proc_ms, 1),
                    "sightings": lc.worker.tracker.count,
                    "recording": bool(lc.recorder and lc.recorder.running),
                    "record_error": lc.recorder.error if lc.recorder else "",
                }
            )
            out[cid] = h
        return out

    def pending_recognition(self) -> int:
        return self._rec_q.qsize()

    def set_test_mode(self, enabled: bool, stamps: list[str]) -> list[str]:
        clean = []
        for s in stamps:
            try:
                clean.append(normalize_stamp_code(s))
            except ValueError:
                continue
        self.cfg.test_mode.enabled = enabled
        self.cfg.test_mode.stamps = sorted(set(clean))
        return self.cfg.test_mode.stamps
