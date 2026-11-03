"""Soak test wrapper: runs only when BEHSARD_SOAK_MINUTES is set (CI nightly)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.soak


@pytest.mark.skipif(not os.environ.get("BEHSARD_SOAK_MINUTES"), reason="set BEHSARD_SOAK_MINUTES to run")
def test_soak() -> None:
    script = Path(__file__).parents[1] / "scripts" / "soak.py"
    minutes = os.environ["BEHSARD_SOAK_MINUTES"]
    warm = str(min(10.0, float(minutes) / 4))
    r = subprocess.run([sys.executable, str(script), "--minutes", minutes, "--warmup", warm])
    assert r.returncode == 0
