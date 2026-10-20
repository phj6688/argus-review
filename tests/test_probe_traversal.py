"""E4-a: scan_contents must confine every write to its temp root, so a crafted
review-diff filename (parsed verbatim from the `+++ b/<path>` header) cannot
write outside the tree as the container user. Benign nested paths still work."""
import os

from argus.jarvis.probes import INCIDENT_PROBES, scan_contents


def _snapshot(root):
    return {os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs}


def test_traversal_and_absolute_paths_are_contained(tmp_path):
    # scan_contents receives an already-parsed {path: content} dict, so a
    # `rename to ../x` diff header (diff.py) reaches this layer as a `../x` key —
    # the same containment property covers both. A canary tree we assert is never
    # touched by a materialised probe file.
    canary_root = tmp_path / "outside"
    canary_root.mkdir()
    before = _snapshot(str(canary_root))

    malicious = {
        "../../pwned.py": "x = 1",
        "../evil.py": "x = 1",
        os.path.join(str(canary_root), "abs_pwned.py"): "x = 1",  # absolute escape
    }
    # Must not raise, and must not create any file under the canary root.
    scan_contents(INCIDENT_PROBES, malicious)
    assert _snapshot(str(canary_root)) == before, "a probe write escaped the temp root"


def test_benign_nested_path_still_materialises(monkeypatch):
    # Prove containment did not break normal operation: a safe nested file is
    # written and visible to the probe engine (whole-file read succeeds).
    seen = {}
    import argus.jarvis.probes as probes_mod

    real_run = probes_mod.run_probes

    def _spy(probes, root):
        # capture what got materialised under root
        seen["files"] = {
            os.path.relpath(os.path.join(dp, f), root)
            for dp, _, fs in os.walk(root) for f in fs
        }
        return real_run(probes, root)

    monkeypatch.setattr(probes_mod, "run_probes", _spy)
    scan_contents(INCIDENT_PROBES, {"a/b.py": "print(1)"})
    assert "a/b.py" in seen.get("files", set())
