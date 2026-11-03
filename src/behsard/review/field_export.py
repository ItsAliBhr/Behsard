""""Export field dataset": one zip to send back for tuning.

Contents::

    labeled/            eval-format dataset (labels.json + carton crops) of
                        every supervisor/test-mode decision
    events.jsonl        every carton event: time, conveyor, mode, status,
                        recognizer candidates/scores, final decision, media
    config.yaml         configuration with camera passwords removed
    summary.json        counts
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

from behsard.config import Config, to_dict
from behsard.core.repository import Repository
from behsard.review.service import export_labels


def export_field_dataset(repo: Repository, cfg: Config, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"behsard-field-{datetime.now():%Y%m%d-%H%M%S}"
    work = Path(tempfile.mkdtemp())
    n_labeled = export_labels(repo, cfg.media_dir, work / "labeled")
    reviews: dict[int, dict[str, object]] = {}
    for r in repo.list_manual_reviews():
        st = repo.get_stamp(r.stamp_id) if r.stamp_id else None
        reviews[r.carton_event_id] = {"decision": str(r.decision), "stamp": st.code if st else None}
    n_events = 0
    with (work / "events.jsonl").open("w", encoding="utf-8") as f:
        for ev in repo.list_carton_events(limit=10**7):
            st = repo.get_stamp(ev.stamp_id) if ev.stamp_id else None
            f.write(json.dumps({
                "id": ev.id, "ts": ev.ts.isoformat(), "conveyor": ev.conveyor_id, "shift": ev.shift_id,
                "mode": ev.mode, "status": str(ev.status), "final_stamp": st.code if st else None,
                "confidence": ev.confidence, "auto_stamp": ev.meta.get("stamp_code"),
                "best_guess": ev.meta.get("best_guess"), "scores": ev.meta.get("scores"),
                "candidates": ev.meta.get("candidates"), "sides": ev.meta.get("sides"),
                "review": reviews.get(ev.id or -1), "frame": ev.frame_path, "crop": ev.crop_path,
            }, ensure_ascii=False) + "\n")
            n_events += 1
    d = to_dict(cfg)
    for cam in d["cameras"]:
        if cam.get("password"):
            cam["password"] = "***"
        if isinstance(cam.get("url"), str) and "@" in cam["url"]:
            from behsard.live.reader import redact

            cam["url"] = redact(cam["url"])
    (work / "config.yaml").write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    summary = {"created": datetime.now().isoformat(timespec="seconds"), "events": n_events,
               "labeled": n_labeled, "by_status": repo.count_by_status(), "test_mode": repo.count_by_status("test")}
    (work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    zpath = out_dir / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                z.write(p, f"{name}/{p.relative_to(work).as_posix()}")
    shutil.rmtree(work, ignore_errors=True)
    return zpath
