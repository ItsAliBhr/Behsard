"""Replay a synthetic conveyor video through a running Behsard app and
check the counts (packaged-app acceptance test).

Usage: python scripts/replay_check.py http://127.0.0.1:8000 VIDEO_DIR
VIDEO_DIR holds left.mp4 + truth.json from ``behsard gen-synth-video``.
Exits non-zero on failure.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def post(url: str, data: dict[str, object]) -> None:
    body = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def main() -> int:
    base, vdir = sys.argv[1].rstrip("/"), Path(sys.argv[2]).resolve()
    truth = json.loads((vdir / "truth.json").read_text())
    complete = [c for c in truth["cartons"] if c["complete"]]
    # a shift with every stamp of the video on conveyor 1
    post(base + "/shifts/open", {"name": "replay-check"})
    shift_id = max(int(s.rsplit("/", 1)[1]) for s in _shift_links(base))
    for i, code in enumerate(truth["codes"]):
        post(f"{base}/shifts/{shift_id}/assign", {"worker": f"replay worker {i}", "stamp": code, "conveyor": 1})
    last = max([e["id"] for e in get_json(base + "/api/events?limit=100000")] or [0])  # type: ignore[union-attr]
    post(base + "/replay/start", {"path": str(vdir / "left.mp4"), "conveyor_id": 1, "side": "L", "speed": 2})
    deadline = time.time() + 300
    while time.time() < deadline:
        st = get_json(base + "/api/status")
        cam = st["cameras"].get("replay")  # type: ignore[index]
        if cam is None or cam["state"] == "finished":
            break
        time.sleep(2)
    time.sleep(8)  # let the last cartons be released and recognised
    events = [e for e in get_json(f"{base}/api/events?after={last}&limit=100000") if e["mode"] == "replay"]  # type: ignore[union-attr]
    # stamps mode (default): the left camera reports the stamps on the left
    # side; each at most once, never a wrong code
    from collections import Counter

    expected = Counter(c["stamp"] for c in complete if c["stamp"] and c["side"] == "L")
    credited = Counter(e["stamp"] for e in events if e["stamp"])
    wrong = sum(n for k, n in credited.items() if k not in expected)
    extra = sum(max(0, n - expected.get(k, 0)) for k, n in credited.items())
    print(f"left-side stamps expected={dict(expected)} credited={dict(credited)} events={len(events)} wrong={wrong} extra={extra}")
    print(json.dumps(events, ensure_ascii=False)[:2000])
    ok = wrong == 0 and extra == 0 and sum(credited.values()) >= 1
    print("REPLAY CHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


def _shift_links(base: str) -> list[str]:
    import re

    with urllib.request.urlopen(base + "/shifts", timeout=30) as r:
        html = r.read().decode()
    return re.findall(r'href="(/shifts/\d+)"', html)


if __name__ == "__main__":
    sys.exit(main())
