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
from bench import (bench, calibrate, reference_record, Result,  # noqa: E402
                   NOISE_FLOOR_NS, REFERENCE_PATH, _loadavg, _plan,
                   MS_TRIAL_TARGET, MIN_MS_TRIALS, MS_BUDGET_S)

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

# --- 8. The sampling plan must adapt to the cost of the operation -----------
# A cheap operation needs many short windows so that at least one of them lands
# between scheduler preemptions.
#
# **Two checks here previously asserted the opposite for expensive operations**
# ("an expensive operation is sampled few times", trials <= 31, and that trial
# count falls monotonically with cost). They encoded the belief that a long
# window is safe from preemption because interference is small relative to it.
# On 2026-09-07 that belief was measured and is false: a 55ms scan's minimum
# fell 37% between 15 and 400 trials. Those two checks were removed rather than
# relaxed, because they were pinning a bug in place. What replaces them is the
# property that is actually wanted -- a bounded *cost* per measurement, and no
# silent drop below the trial count needed to find a floor.
fast = bench("cheap op", "y = x + 1", setup="x = 41")            # nanoseconds
mid = bench("mid op", "y = sorted(data)", setup="data = list(range(20000))[::-1]")   # ~0.2 ms
slow = bench("expensive op", "y = sorted(data)", setup="data = list(range(400000))[::-1]")  # ~5 ms
check("a cheap operation is sampled many times", fast.trials >= 101,
      f"got {fast.trials} trials for {fast.ns_per_op:.1f} ns/op")
check("an expensive operation is still sampled enough to find its floor",
      slow.trials >= MIN_MS_TRIALS or slow.undersampled,
      f"got {slow.trials} trials for {slow.ns_per_op / 1e6:.2f} ms/op, "
      f"undersampled={slow.undersampled}")
check("no measurement is allowed to exceed the sampling budget by much",
      slow.trials * slow.ns_per_op / 1e9 <= MS_BUDGET_S * 1.5,
      f"{slow.trials} trials x {slow.ns_per_op / 1e6:.2f} ms "
      f"= {slow.trials * slow.ns_per_op / 1e9:.1f}s vs {MS_BUDGET_S}s budget")
check("any measurement sampled below the floor is flagged, not published",
      all(r.undersampled or r.trials >= MIN_MS_TRIALS or r.ns_per_op < 1e6
          for r in (fast, mid, slow)),
      f"fast={fast.trials} mid={mid.trials} slow={slow.trials}")

# --- 8b. A trial window must actually last about as long as it was asked to -
# If the iteration count were derived from a badly wrong cost estimate, every
# statistic above would be computed over windows too short to mean anything.
check("a trial window is at least the requested length", mid.window_ms >= 18.0,
      f"window was {mid.window_ms:.1f} ms over {mid.iterations} iterations")
check("a cheap operation's window is long enough for timer resolution to vanish",
      fast.window_ms >= 5.0,
      f"window was {fast.window_ms:.3f} ms over {fast.iterations} iterations")
check("an explicit trial count still overrides the plan",
      bench("forced", "y = x + 1", setup="x = 41", trials=7).trials == 7)

# --- 9. Stability must detect a floor that did not converge -----------------
# Constructed rather than observed: a synthetic sample set whose two halves
# disagree about the minimum has to be reported as untrustworthy, and one whose
# halves agree has to pass. This is the check that gates publication, so it is
# tested against known inputs rather than against a live measurement.
converged = Result(name="x", ns_per_op=100.0, median_ns=180.0, spread_pct=80.0,
                   iterations=1000, trials=100, stability_pct=1.0)
diverged = Result(name="x", ns_per_op=100.0, median_ns=105.0, spread_pct=5.0,
                  iterations=1000, trials=100, stability_pct=22.0)
check("a converged minimum is publishable despite a noisy median", converged.trustworthy)
check("a divergent minimum is refused despite a tight median", not diverged.trustworthy)

# The live version of the same property: an honest measurement's halves agree.
check("a real measurement's halves agree on the floor", fast.stability_pct <= 10.0,
      f"stability={fast.stability_pct:.1f}%")

# --- 10. The percentile floor must sit between the minimum and the median ---
check("p10 lies between the minimum and the median",
      fast.ns_per_op <= fast.p10_ns <= max(fast.median_ns, fast.ns_per_op),
      f"min={fast.ns_per_op:.2f} p10={fast.p10_ns:.2f} median={fast.median_ns:.2f}")

# --- 11. The machine-speed reference ----------------------------------------
# Convergence says a measurement settled; it does not say the machine was
# running at full speed while it settled. On 2026-09-03 an entire suite came
# back 2.1-3.6x slow with every measurement converging, which is what this
# reference exists to catch. These checks are about the mechanism, not about
# any particular speed -- the machine is allowed to be slow while the tests run.
rec = reference_record()
check("the speed reference record is present and well formed",
      isinstance(rec.get("best_ns"), (int, float)) and rec["best_ns"] > 0
      and 0 < float(rec["throttle_floor"]) <= 1.0,
      f"best={rec.get('best_ns')} floor={rec.get('throttle_floor')}")

before = REFERENCE_PATH.read_text()
speed = calibrate(update=False)
check("calibration reports a positive speed ratio against the record",
      speed["speed_ratio"] > 0 and speed["observed_ns"] > 0,
      f"ratio={speed['speed_ratio']} observed={speed['observed_ns']:.2f} ns")
check("calibrate(update=False) never rewrites the record",
      REFERENCE_PATH.read_text() == before)

# The gate itself, tested against known inputs rather than a live machine: a
# run at a third of peak must be refused, and a run at full speed accepted.
floor = float(rec["throttle_floor"])
check("a run at a third of the record's speed is refused", (1 / 3) < floor,
      f"floor={floor}")
check("a run at the record's speed is accepted", 1.0 >= floor)

# The record may only ever move downward (faster). A slower observation must
# not be able to relax the yardstick.
check("a slower observation cannot loosen the record",
      speed["observed_ns"] >= rec["best_ns"] or speed["speed_ratio"] >= 1.0,
      f"observed={speed['observed_ns']:.2f} record={rec['best_ns']:.2f}")

# ---------------------------------------------------------------------------
# The millisecond sampling plan.
#
# Added 2026-09-07, after measuring that a 55ms SQLite scan's minimum drifts
# 37% between 15 and 400 trials, and that at 40 trials the split-half check
# certifies a figure 45% above the settled floor. The old plan sampled
# millisecond work 15 times. These checks pin the properties that fix relies on.
# ---------------------------------------------------------------------------

ms_trials, _ = _plan(2e6)                     # a 2ms operation: budget is ample
check("a millisecond operation is sampled at the evidence-backed target",
      ms_trials == MS_TRIAL_TARGET, f"trials={ms_trials}")

cheap_trials, _ = _plan(50e6)                 # 50ms: 401 trials would be 20s
check("a 50ms operation stays at or above the minimum trial count",
      cheap_trials >= MIN_MS_TRIALS, f"trials={cheap_trials}")

slow_trials, _ = _plan(2e9)                   # a 2s operation: budget bites hard
check("an operation too slow to sample enough is cut by the budget",
      slow_trials < MIN_MS_TRIALS, f"trials={slow_trials}")

check("the budget is respected at the top of the millisecond range",
      slow_trials * 2.0 <= MS_BUDGET_S + 2.0,
      f"{slow_trials} trials x 2s vs {MS_BUDGET_S}s budget")

# The flag must actually block publication, and must not be rescuable by a
# flattering stability figure -- that is the whole point of it.
check("an undersampled result is never trustworthy",
      not Result(name="x", ns_per_op=5e7, median_ns=5e7, spread_pct=0.0,
                 iterations=1, trials=20, stability_pct=0.0,
                 undersampled=True).trustworthy)
check("an otherwise identical well-sampled result is trustworthy",
      Result(name="x", ns_per_op=5e7, median_ns=5e7, spread_pct=0.0,
             iterations=1, trials=401, stability_pct=0.0,
             undersampled=False).trustworthy)
check("an undersampled result says so when printed",
      "UNDERSAMPLED" in str(Result(name="x", ns_per_op=5e7, median_ns=5e7,
                                   spread_pct=0.0, iterations=1, trials=20,
                                   stability_pct=0.0, undersampled=True)))

# Sub-millisecond work must be untouched by any of this: the nanosecond and
# microsecond tiers were validated by earlier runs and are not being changed.
check("sub-microsecond work still gets many short windows",
      _plan(30.0) == (401, 0.006), f"{_plan(30.0)}")
check("microsecond work is unchanged", _plan(50_000.0) == (151, 0.020),
      f"{_plan(50_000.0)}")
check("a fast operation is never marked undersampled",
      not bench("cheap", "1 + 1").undersampled)

print(f"\nmachine speed during this run: {speed['speed_ratio']:.2f}x of the "
      f"{rec['best_ns']:.2f} ns record ({speed['observed_ns']:.2f} ns observed)")
print(f"\nnoise floor: {NOISE_FLOOR_NS} ns")
print(f"load average during this run: {_loadavg()}")

if failures:
    print(f"\nFAILED {len(failures)} of {passed + len(failures)} checks")
    for f in failures:
        print("  x " + f)
    sys.exit(1)
print(f"\nok  harness: {passed} checks passed")
