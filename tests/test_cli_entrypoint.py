"""E2-e: `python -m argus.cli` must run the CLI, not silently no-op (it lacked a
`__main__` guard, so the module form exited 0 without doing anything)."""
import subprocess
import sys


def test_module_form_prints_usage_and_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "argus.cli", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout + proc.stderr).lower()
    assert proc.returncode == 0, f"exit {proc.returncode}; output: {out[:400]}"
    assert "usage" in out or "argus" in out, f"no usage text: {out[:400]}"
