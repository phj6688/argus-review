"""Exact anchors across deterministic edits near flagged lines."""

import difflib
import random

from argus import replies


def test_nearby_edits_preserve_exact_flagged_span():
    rng = random.Random(7)
    old = [f"line {n}" for n in range(1, 201)]
    total = missed = wider = disjoint = 0
    for _ in range(5000):
        start = rng.randint(60, 120)
        span = rng.choice([0, 0, 1, 2, 4, 6, 10])
        end = start + span
        pos = rng.randint(start - 6, end + 6)
        kind = rng.choice(["insert", "delete", "modify"])
        new = list(old)
        if kind == "insert":
            new[pos:pos] = [f"added {k}" for k in range(rng.randint(1, 5))]
        elif kind == "delete":
            if start - 1 <= pos <= end - 1:
                continue
            del new[pos]
        else:
            if start - 1 <= pos <= end - 1:
                continue
            new[pos] = f"changed {pos}"
        patch = "\n".join(list(difflib.unified_diff(old, new, lineterm="", n=3))[2:])
        true_start = new.index(old[start - 1]) + 1
        true_end = new.index(old[end - 1]) + 1
        first, last = replies._anchor(dict(line=start, end_line=end), dict(patch=patch))
        total += 1
        if first > true_start or last < true_end:
            missed += 1
        if first < true_start or last > true_end:
            wider += 1
        if last < true_start or first > true_end:
            disjoint += 1
    assert total == 4209
    assert (missed, wider, disjoint) == (0, 0, 0)
