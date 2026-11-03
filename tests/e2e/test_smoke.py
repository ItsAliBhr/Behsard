"""Operator GUI smoke test (Task 8 acceptance).

Open every page, add a simulated camera, draw an ROI, open a shift, see live
counts increase, resolve a review item and export Excel.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
PAGES = ["/", "/cameras", "/cameras/new", "/shifts", "/review", "/reports", "/test", "/replay"]


def _num(text: str) -> int:
    return int(text.strip().translate(FA) or 0)


def test_operator_flow(app_url: str, browser, synth_video: Path, tmp_path: Path) -> None:
    truth = json.loads((synth_video / "truth.json").read_text())
    page = browser.new_page(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # every page opens
    for p in PAGES:
        r = page.goto(app_url + p)
        assert r is not None and r.status == 200, p
        assert page.locator("header").is_visible()

    # open a shift and assign the stamps that appear in the video
    page.goto(app_url + "/shifts")
    page.fill("input[name=name]", "صبح")
    page.click("#open-shift")
    page.wait_for_url("**/shifts/*")
    for i, code in enumerate(truth["codes"]):
        page.fill("#worker-input", f"کارگر {i + 1}")
        page.fill("input[name=stamp]", code)
        page.press("input[name=stamp]", "Enter")
        page.wait_for_selector(f"#assignments >> text={code}")

    # add a simulated camera (video file as RTSP stand-in)
    page.goto(app_url + "/cameras/new")
    page.fill("input[name=id]", "sim1L")
    page.select_option("#source", "url")
    page.fill("input[name=url]", str(synth_video / "left.mp4"))
    page.click("#save-camera")
    page.wait_for_url("**/cameras/sim1L/setup")

    # test connection -> snapshot + quality table
    page.click("#test-conn")
    page.wait_for_selector("#conn-ok", timeout=20000)
    page.wait_for_selector("#snap[src*='snapshots']")
    # draw the watch region with the mouse
    box = page.locator("#roi-canvas").bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] * 0.05, box["y"] + box["height"] * 0.1)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.95, box["y"] + box["height"] * 0.95, steps=8)
    page.mouse.up()
    x = float(page.locator("#roi-form input[name=x]").input_value())
    w = float(page.locator("#roi-form input[name=w]").input_value())
    assert 0.03 < x < 0.08 and 0.85 < w < 0.93
    page.click("#save-roi")
    page.wait_for_selector("text=ناحیه دید ذخیره شد")

    # live counts increase on the dashboard
    page.goto(app_url + "/")
    page.wait_for_selector("img[src='/live/sim1L.mjpg']")
    deadline = time.time() + 120
    seen: list[int] = []
    while time.time() < deadline:
        total = _num(page.locator("#total-count").inner_text())
        pending = _num(page.locator("#pending-count").inner_text())
        seen.append(total + pending)
        if total >= 1 and pending >= 1 and len(set(seen)) >= 3:
            break
        time.sleep(2)
    assert max(seen) >= 2 and seen[-1] > seen[0], seen
    assert _num(page.locator("#total-count").inner_text()) >= 1

    # resolve one review item with the keyboard
    page.goto(app_url + "/review")
    before = _num(page.locator(".pending b").inner_text())
    assert before >= 1
    page.keyboard.press("Digit1")
    page.wait_for_selector("text=ثبت شد")

    # Excel export
    page.goto(app_url + "/reports")
    with page.expect_download() as dl:
        page.click("text=خروجی اکسل")
    out = tmp_path / "report.xlsx"
    dl.value.save_as(out)
    assert out.stat().st_size > 1000 and out.read_bytes()[:2] == b"PK"

    assert not errors, errors
