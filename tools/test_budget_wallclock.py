# -*- coding: utf-8 -*-
"""Invariant test: every disk deadline stays inside the hard boundary,
and wall-clock elapsed never exceeds the round budget.

slice_s (quota) MAY sum above the budget: it means "at most this long",
not "will use this long". The binding constraint is the wall clock.
"""
import contextlib
import io
import os
import sys
import time
import types

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
os.environ.setdefault("YD2_WORK", "/tmp/yd2work_test")
os.environ["PYTHONIOENCODING"] = "utf-8"

for name, cls in (("ty_cloud_personal", "TyPersonal"), ("ty139", "Ty139")):
    mod = types.ModuleType(name)
    setattr(mod, cls, type(cls, (), {"__init__": lambda s, *a, **k: None,
                                     "login": lambda s: True}))
    sys.modules[name] = mod

import yd2_teldrive as m  # noqa: E402


class FakeSrc(object):
    def __init__(self, name, work):
        self.name = name
        self.prefix = name
        self.work = work

    def root(self):
        return "/"

    def list(self, node):
        return []

    def link(self, it):
        return "http://x"


m.collect = lambda src, want_video_only=True: ([], 1)
m.collect_dst = lambda base: {}
m.fetch = lambda *a, **k: None
m.upload = lambda *a, **k: (True, "")

rec = []


def spy(src, a, t0, t_deadline=None, slice_s=None):
    rec.append({"disk": src.name,
                "slice_min": round((slice_s or 0) / 60.0, 1),
                "offset_min": round((t_deadline - t0) / 60.0, 1)})
    time.sleep(src.work)
    return {"mode": "real", "src_files": 0, "src_bytes": 0, "dst_files": 0,
            "dst_bytes": 0, "todo_files": 0, "todo_bytes": 0,
            "new_files": 0, "new_bytes": 0, "fail_files": 0}


m.run_one = spy

BUDGET = 270
RESERVE = 15
CAP = BUDGET - RESERVE

cases = ((0.05, "disk1 exits almost immediately"), (3.0, "disk1 uses 3s"))

for work, label in cases:
    rec[:] = []
    t_start = time.time()
    m.SrcFamily = lambda w=work: FakeSrc("family", w)
    m.SrcPersonal = lambda: FakeSrc("personal", 0.05)
    sys.argv = ["x", "--disk", "both", "--apply", "--budget-min", str(BUDGET),
                "--reserve-min", str(RESERVE), "--rotate", "family"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main()
    print("\n--- %s ---" % label)
    for r in rec:
        print("  %-9s quota=%6.1f min  deadline_offset=%6.1f min"
              % (r["disk"], r["slice_min"], r["offset_min"]))
        assert r["offset_min"] <= CAP + 1, (
            "disk %s deadline offset %.1f exceeds hard cap %d"
            % (r["disk"], r["offset_min"], CAP))
        assert r["slice_min"] <= CAP + 1, (
            "disk %s quota %.1f exceeds cap %d"
            % (r["disk"], r["slice_min"], CAP))
    elapsed = (time.time() - t_start) / 60.0
    print("  wall-clock elapsed %.2f min <= %d min : %s"
          % (elapsed, BUDGET, elapsed <= BUDGET))
    assert elapsed <= BUDGET, "wall clock exceeded budget"
    print("  PASS")

print("\nall invariants hold")
