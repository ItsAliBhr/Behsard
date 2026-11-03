"""CLI: process-video, gen-synth-video."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from behsard.cli import command
from behsard.config import load_config


def _pv_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--video", action="append", required=True, help="video file (repeat per camera)")
    p.add_argument("--camera", action="append", required=True, help="camera id per --video")
    p.add_argument("--start", required=True, help="recording start time, ISO (site local time)")
    p.add_argument("--conveyor", type=int, default=None, help="if the camera is not in config")
    p.add_argument("--side", action="append", default=None, help="L/R per camera if not in config")
    p.add_argument("--recognizer", default="stamp")
    p.add_argument("--fps", type=float, default=None, help="processing fps (default: config)")
    p.add_argument("--db", default=None, help="override DB path")
    p.add_argument("--media", default=None, help="override media dir")


@command("process-video", _pv_args)
def process_video(args: argparse.Namespace) -> int:
    from behsard.core.repository import SqliteRepository
    from behsard.core.services import sync_config
    from behsard.recognition.registry import get_recognizer
    from behsard.tracking.pipeline import CameraInput, Pipeline
    from behsard.tracking.tracker import TrackerParams

    cfg = load_config(args.config)
    if len(args.video) != len(args.camera):
        raise SystemExit("give one --camera per --video")
    repo = SqliteRepository(args.db or cfg.db_path)
    sync_config(repo, cfg)
    inputs = []
    for i, (video, cam_id) in enumerate(zip(args.video, args.camera)):
        cam = repo.get_camera(cam_id)
        conveyor = args.conveyor if args.conveyor is not None else (cam.conveyor_id if cam else None)
        side = args.side[i] if args.side and i < len(args.side) else (cam.side if cam else None)
        if conveyor is None or side is None:
            raise SystemExit(f"camera {cam_id}: unknown; pass --conveyor and --side")
        inputs.append(CameraInput(Path(video), cam_id, int(conveyor), side.upper()))
    t = cfg.tracking
    params = TrackerParams(roi=t.roi, frames_per_sighting=t.frames_per_sighting)
    pipe = Pipeline(
        repo,
        get_recognizer(args.recognizer, cfg.recognition),
        args.media or cfg.media_dir,
        params=params,
        process_fps=args.fps or t.process_fps,
        pair_window_s=t.pair_window_s,
        log=print,
    )
    res = pipe.run(inputs, datetime.fromisoformat(args.start))
    print(json.dumps(res.summary(), indent=1, ensure_ascii=False))
    return 0


def _gsv_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cartons", type=int, default=8)


@command("gen-synth-video", _gsv_args)
def gen_synth_video(args: argparse.Namespace) -> int:
    from behsard.eval.synth_video import VideoConfig, generate_video_pair

    truth = generate_video_pair(args.out, args.seed, VideoConfig(n_cartons=args.cartons))
    print(json.dumps(truth, indent=1))
    return 0
