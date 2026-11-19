import os
import subprocess
import sys

import pytest


def test_profile_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("nonblocking FIFOs unavailable")
    fifo = tmp_path / "profile.json"
    os.mkfifo(fifo)
    code = (
        "from pathlib import Path; "
        "from ozagia.core.deep_review.analyzers.profiles import _load_report; "
        "import sys; "
        "_load_report(Path(sys.argv[1]), Path(sys.argv[2]))"
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(fifo)],
                            capture_output=True, text=True, timeout=3, check=False)
    assert result.returncode != 0
    assert "not a regular file" in result.stderr
