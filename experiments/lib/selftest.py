#!/usr/bin/env python3
"""Validates the benchmark harness itself.

A harness that reports confident wrong numbers is worse than no harness, and
every claim on Overhead rests on this one. These checks are deliberately about
properties that must hold if the timing is sound, rather than about specific
figures, which are hardware-dependent.

Run: python3 experiments/lib/selftest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import bench, NOISE_FLOOR_NS  # noqa: E402

failures: list[str] = []
passed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL {name}  {detail}")


print("harness self-test\n")

# --- 1. An empty statement must register as free -----------------------------
# If `pass` shows a real cost, the empty-loop subtraction is broken.
r = bench("pass", "pass")
check("empty statement is below the noise floor", r.below_noise_floor,
      f"got {r.ns_per_op:.3f} ns")

# --- 2. Linearity: N copies of a statement must cost about N times one -------
# This is the strongest available check that we are measuring the loop body
# rather than an artefact of the loop itself.
one = bench("1x", "y = x + 1", setup="x = 41")
ten = bench("10x", "\n".join(["y = x + 1"] * 10), setup="x = 41")

ratio = ten.ns_per_op / one.ns_per_op if one.ns_per_op > 0 else 0
check("10 copies cost 7-13x one copy", 7.0 <= ratio <= 13.0,
      f"one={one.ns_per_op:.2f}ns ten={ten.ns_per_op:.2f}ns ratio={ratio:.2f}x")

# --- 3. A known-expensive operation must dominate a cheap one ---------------
cheap = bench("int add", "y = x + 1", setup="x = 41")
costly = bench("string format", 'y = "%s-%s" % (x, x)', setup="x = 41")
check("string formatting costs more than an int add", costly.ns_per_op > cheap.ns_per_op * 3,
      f"cheap={cheap.ns_per_op:.2f}ns costly={costly.ns_per_op:.2f}ns")

# --- 4. A function call must cost more than an inlined equivalent ------------
inlined = bench("inlined", "y = 1")
called = bench("via call", "y = f()", setup="def f(): return 1")
check("a function call costs more than the work it wraps", called.ns_per_op > inlined.ns_per_op + 3,
      f"inlined={inlined.ns_per_op:.2f}ns called={called.ns_per_op:.2f}ns")

# --- 5. Results must be repeatable across independent runs ------------------
a = bench("run a", "y = x * 2 + 1", setup="x = 41")
b = bench("run b", "y = x * 2 + 1", setup="x = 41")
drift = abs(a.ns_per_op - b.ns_per_op) / max(a.ns_per_op, b.ns_per_op, 1e-9) * 100
check("two runs of the same statement agree within 25%", drift < 25.0,
      f"a={a.ns_per_op:.2f}ns b={b.ns_per_op:.2f}ns drift={drift:.1f}%")

# --- 6. Calibration must reach a statistically useful iteration count -------
check("calibration picks a large iteration count for a cheap op", one.iterations >= 10_000,
      f"got {one.iterations} iterations")

# --- 7. Setup must not be counted in the measurement ------------------------
# The setup here is deliberately slow; if it leaked into the timing, the
# per-iteration cost would be enormous.
slow_setup = bench("slow setup", "y = len(data)", setup="data = list(range(200000))")
check("setup cost is excluded from per-op timing", slow_setup.ns_per_op < 200,
      f"got {slow_setup.ns_per_op:.2f} ns/op")

print(f"\nnoise floor: {NOISE_FLOOR_NS} ns")

if failures:
    print(f"\nFAILED {len(failures)} of {passed + len(failures)} checks")
    for f in failures:
        print("  x " + f)
    sys.exit(1)
print(f"\nok  harness: {passed} checks passed")
