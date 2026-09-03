"""Microbenchmark harness shared by every experiment on Overhead.

The methodology is the product here, so it is written down:

* **Statements are inlined into the timing loop**, the way ``timeit`` does it,
  rather than being passed as callables. This matters enormously. If the
  harness measured ``lambda: x.y``, every result would silently include one
  Python function call -- around 15ns on current hardware -- which is larger
  than many of the things worth measuring. Inlining means the loop body is the
  operation and nothing else.

* **The empty loop is measured and subtracted.** A ``for`` loop that does
  nothing still costs something. We time ``pass`` under identical conditions
  and subtract it, so a reported number is the marginal cost of the operation.

* **Min is the headline, not mean.** Timing noise on a general-purpose OS is
  additive: a scheduler preemption or an interrupt can only make a run slower,
  never faster. The minimum across trials is the best available estimate of
  true cost. Median and spread are reported alongside so a reader can see when
  a benchmark was noisy and discount it.

* **Sub-nanosecond results are reported as such, not as zero.** When an
  operation costs less than the noise floor, that is the finding.

* **Sampling adapts to how fast the operation is.** A 30ns operation and a
  30ms one are ruined by different things. Fast operations are sampled many
  times over short windows, because what threatens them is a scheduler
  preemption landing inside the one long trial you took; slow operations are
  sampled few times over long windows, because what threatens them is timer
  granularity and one-off setup effects.

* **Convergence of the minimum is measured, not assumed.** Under machine load
  the median is polluted while the minimum is not, so median-based spread
  stops being a measure of *measurement* quality and becomes a measure of how
  busy the machine was. ``stability_pct`` splits the samples in half and
  compares the two minima: if independent halves of the run agree on the
  floor, the floor is real. That is the number to gate a published claim on.
  ``spread_pct`` is retained and reports machine noise.

* **The speed of the machine itself is measured, not assumed.** Convergence
  is a claim about *precision*: it says independent halves of a run agree on
  the floor. It says nothing about *accuracy*. A thread pinned to an
  efficiency core runs every window uniformly slowly, so the two halves agree
  perfectly on a floor that is three times too high. Each suite therefore
  times a fixed reference kernel and reports what fraction of this machine's
  best-ever observed speed it is getting. See ``reference.json`` for the
  measurements that forced this.

* **Everything is recorded**, including the load average during the run, so a
  published number can always be traced back to the conditions that produced
  it.
"""

from __future__ import annotations

import gc
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Environment capture
# ---------------------------------------------------------------------------

def _sysctl(key: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _loadavg() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


def environment() -> dict[str, Any]:
    """Everything needed to judge whether a number transfers to your machine."""
    env: dict[str, Any] = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
    }
    if sys.platform == "darwin":
        env["cpu"] = _sysctl("machdep.cpu.brand_string")
        env["model"] = _sysctl("hw.model")
        env["cores"] = _sysctl("hw.ncpu")
        mem = _sysctl("hw.memsize")
        if mem:
            env["memory_gb"] = round(int(mem) / 1024 ** 3)
        env["os_version"] = platform.mac_ver()[0]
    return {k: v for k, v in env.items() if v}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    ns_per_op: float          # min across trials: best estimate of true cost
    median_ns: float
    spread_pct: float         # (median - min) / min: how noisy the machine was
    iterations: int
    trials: int
    stability_pct: float = 0.0  # disagreement between the minima of two halves
    p10_ns: float = 0.0         # 10th percentile, a less brittle floor estimate
    window_ms: float = 0.0      # length of one timed trial at the measured cost
    below_noise_floor: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def trustworthy(self) -> bool:
        """Whether the minimum converged well enough to publish a claim from.

        Independent halves of the run must agree on the floor to within 3%, or
        to within twice the noise floor in absolute terms -- whichever is
        looser. The absolute clause is not a loophole: the harness already
        declares that it will not claim a difference smaller than
        NOISE_FLOOR_NS, so demanding that two halves agree *more* tightly than
        that would be asking for precision it has said it does not have. At
        30 ns a one-nanosecond disagreement reads as 3%, and no amount of
        sampling removes it.

        This is deliberately not a check on `spread_pct`: on a loaded machine
        the median can be twice the minimum while the minimum itself is
        perfectly reproducible, and refusing that measurement would mean
        refusing to measure anything on a machine doing other work.
        """
        if self.below_noise_floor:
            return True
        absolute = self.stability_pct / 100 * self.ns_per_op
        return self.stability_pct <= 3.0 or absolute <= 2 * NOISE_FLOOR_NS

    def format_ns(self) -> str:
        if self.below_noise_floor:
            return "<0.5"
        if self.ns_per_op < 10:
            return f"{self.ns_per_op:.2f}"
        return f"{self.ns_per_op:.1f}"

    def __str__(self) -> str:
        flag = "  (below noise floor)" if self.below_noise_floor else ""
        if not self.trustworthy:
            flag += "  ** UNSTABLE, do not publish **"
        return (f"{self.name:<42} {self.format_ns():>9} ns"
                f"   (stability {self.stability_pct:>5.1f}%,"
                f" median {self.median_ns:>8.2f}, spread {self.spread_pct:>5.1f}%){flag}")


# ---------------------------------------------------------------------------
# Core timing
# ---------------------------------------------------------------------------

# The statement is compiled directly into the loop body, so nothing but the
# loop machinery sits between the timer calls and the operation itself.
_TEMPLATE = """
def _inner(_it, _timer, _g):
{setup}
    _t0 = _timer()
    for _i in _it:
{stmt}
    _t1 = _timer()
    return _t1 - _t0
"""


def _indent(src: str, spaces: int) -> str:
    pad = " " * spaces
    lines = [pad + line if line.strip() else line for line in src.strip("\n").split("\n")]
    return "\n".join(lines) or pad + "pass"


def _compile(stmt: str, setup: str, globals_: dict[str, Any]):
    src = _TEMPLATE.format(setup=_indent(setup or "pass", 4), stmt=_indent(stmt, 8))
    scope: dict[str, Any] = dict(globals_)
    exec(compile(src, "<bench>", "exec"), scope)
    return scope["_inner"], scope


# The floor below which we will not claim a specific figure. An empty loop
# iteration is a few nanoseconds; differences smaller than this are not
# distinguishable from measurement error on this harness.
NOISE_FLOOR_NS = 0.5


# Sampling plans, chosen by how expensive one operation turns out to be.
#
# The threat model differs by scale. For a 30ns operation the danger is that a
# scheduler preemption lands inside the single long trial you took, so the
# answer is many short windows: it only takes one clean window to see the
# floor. For a 30ms operation a single trial already dwarfs any preemption,
# and the danger is instead per-trial setup and timer effects, so fewer,
# longer trials are better and cheaper.
#
# (trials, min_trial_s), selected by the calibrated cost of one operation.
# Trial counts were chosen by measuring convergence, not guessed: on this
# machine under load, a sub-microsecond operation's minimum was still drifting
# at 101 trials and had settled by ~400, while a 150-microsecond operation was
# settled by ~200. The cost is roughly five seconds per measurement, which is
# the right trade for a suite that runs once a week and is published from.
_PLANS = [
    (1_000.0,      (401, 0.006)),   # sub-microsecond: many short windows
    (1_000_000.0,  (151, 0.020)),   # microseconds
    (float("inf"), (15,  0.050)),   # milliseconds and up
]


def _plan(ns_estimate: float) -> tuple[int, float]:
    for ceiling, plan in _PLANS:
        if ns_estimate < ceiling:
            return plan
    return _PLANS[-1][1]


def bench(
    name: str,
    stmt: str,
    *,
    setup: str = "",
    globals: dict[str, Any] | None = None,
    trials: int | None = None,
    min_trial_s: float | None = None,
    subtract_loop: bool = True,
    note: str = "",
    **extra: Any,
) -> Result:
    """Measure the marginal per-iteration cost of `stmt` in nanoseconds.

    `stmt` and `setup` are Python source, inlined into a timing loop. Names
    referenced by `stmt` must be defined in `setup` or supplied via `globals`.

    `trials` and `min_trial_s` default to a plan chosen from how expensive the
    operation turns out to be; pass either to override that choice.
    """
    g = globals if globals is not None else {}
    inner, _ = _compile(stmt, setup, g)
    empty, _ = _compile("pass", setup, g)
    timer = time.perf_counter_ns

    # Probe: grow the iteration count until one run is long enough to estimate
    # the per-operation cost, which is what selects the sampling plan.
    PROBE_S = 0.02
    iterations = 1
    while iterations < 100_000_000:
        elapsed = inner(range(iterations), timer, g)
        if elapsed >= PROBE_S * 1e9:
            break
        growth = max(2.0, min(10.0, (PROBE_S * 1e9) / max(elapsed, 1)))
        iterations = int(iterations * growth) + 1
    ns_estimate = elapsed / iterations

    planned_trials, planned_window = _plan(ns_estimate)
    trials = planned_trials if trials is None else trials
    min_trial_s = planned_window if min_trial_s is None else min_trial_s

    # Convert the cost estimate into an iteration count. A probe that ran during
    # a burst of load over-estimates the cost, which *under*-counts iterations
    # and yields a window shorter than the one that was asked for -- the
    # opposite of the safe direction, and the self-test caught it doing exactly
    # that. No single observation can rule it out either, since an observation
    # taken under load is inflated in the same way.
    #
    # So the window is verified after the fact instead of predicted. Once the
    # trials are in, `ns_per_op` is a far better estimate of the true cost than
    # the probe was; if it says the window was short, recompute the iteration
    # count from it and measure again. One correction is always enough, because
    # the second estimate is not a guess.
    target_ns = min_trial_s * 1e9
    iterations = max(1, int(target_ns / max(ns_estimate, 1e-3)))

    def measure(iterations: int) -> tuple[list[float], float]:
        """One full set of trials at a given iteration count.

        The statement and the empty loop are sampled the same number of times
        and interleaved. Both matter. The minimum of many samples is lower than
        the minimum of few, so sampling the baseline less than the statement
        biases the subtraction upward and can drive a cheap statement to zero --
        the self-test caught exactly that. Interleaving further means a burst of
        load is charged to both sides rather than to whichever ran during it.

        GC is disabled throughout: a collection pause mid-trial is real cost in
        production but pure noise here, where the allocation rate is
        unrepresentative of any real workload.
        """
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            gc.collect()
            it = range(iterations)
            samples: list[float] = []
            base: list[float] = []
            for _ in range(trials):
                samples.append(inner(it, timer, g) / iterations)
                if subtract_loop:
                    base.append(empty(it, timer, g) / iterations)
            return samples, (min(base) if base else 0.0)
        finally:
            if gc_was_enabled:
                gc.enable()

    samples, overhead = measure(iterations)
    true_cost = min(samples)
    if iterations * true_cost < target_ns * 0.9 and iterations < 500_000_000:
        iterations = min(500_000_000, max(iterations + 1, int(target_ns / max(true_cost, 1e-3))))
        samples, overhead = measure(iterations)

    lo = min(samples) - overhead
    med = statistics.median(samples) - overhead
    below = lo < NOISE_FLOOR_NS
    lo = max(0.0, lo)
    med = max(0.0, med)
    spread = ((med - lo) / lo * 100) if lo > NOISE_FLOOR_NS else 0.0

    # Split-half agreement of the minimum. Interleaved rather than sequential
    # halves, so a burst of load partway through the run lands in both halves
    # instead of ruining one of them and being invisible in the comparison.
    if len(samples) >= 4 and lo > NOISE_FLOOR_NS:
        a = min(samples[0::2]) - overhead
        b = min(samples[1::2]) - overhead
        stability = abs(a - b) / max(min(a, b), NOISE_FLOOR_NS) * 100
    else:
        stability = 0.0

    ordered = sorted(samples)
    p10 = max(0.0, ordered[max(0, int(len(ordered) * 0.10))] - overhead)

    return Result(
        name=name, ns_per_op=lo, median_ns=med, spread_pct=spread,
        iterations=iterations, trials=trials, stability_pct=stability,
        p10_ns=p10, window_ms=iterations * min(samples) / 1e6,
        below_noise_floor=below, note=note, extra=extra,
    )


# ---------------------------------------------------------------------------
# Machine speed reference
# ---------------------------------------------------------------------------

# The convergence check above answers "did this measurement settle?". It cannot
# answer "was the machine running at full speed while it settled?", and on
# 2026-09-03 that gap published itself: an entire suite came back 2.1-3.6x
# slower than the same suite eleven days earlier, with all 32 measurements
# converging and none flagged. Benchmarks here run at QOS_CLASS_BACKGROUND,
# which on Apple silicon means the efficiency cores, and their delivered
# throughput depends on what else the machine is doing.
#
# The fix is an external yardstick: one fixed kernel, timed every run, compared
# against the fastest this machine has ever been seen to run it.

REFERENCE_NAME = "reference: zero-argument function call"
REFERENCE_STMT = "plain()"
REFERENCE_SETUP = "def plain():\n    return 1\n"
REFERENCE_PATH = Path(__file__).resolve().parent / "reference.json"


def reference_record() -> dict[str, Any]:
    return json.loads(REFERENCE_PATH.read_text())


def calibrate(update: bool = True) -> dict[str, Any]:
    """Time the reference kernel and report this machine's current speed.

    Returns the observed cost, the best ever recorded, and the ratio between
    them. A ratio of 1.0 means the machine is running as fast as it has ever
    been seen to run; 0.35 means a number measured now will read roughly three
    times its true cost.

    If this run is faster than the record *and* the measurement converged, the
    record is updated -- the yardstick can only ever get more demanding, and
    every change is auditable in git. A measurement that did not converge is
    never allowed to set a record, because a glitch low would permanently
    poison the reference.
    """
    r = bench(REFERENCE_NAME, REFERENCE_STMT, setup=REFERENCE_SETUP)
    rec = reference_record()
    best = float(rec["best_ns"])
    observed = r.ns_per_op
    ratio = (best / observed) if observed > 0 else 0.0

    if update and r.trustworthy and 0 < observed < best:
        rec["history"].append({
            "ns": round(observed, 2),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "load": _loadavg(),
            "note": f"new record, previous {best:.2f} ns",
        })
        rec["best_ns"] = round(observed, 2)
        rec["observed_at"] = rec["history"][-1]["at"]
        rec["source"] = "set by calibrate() during a suite run"
        REFERENCE_PATH.write_text(json.dumps(rec, indent=2) + "\n")
        best, ratio = observed, 1.0

    return {
        "kernel": rec["kernel"],
        "observed_ns": round(observed, 2),
        "best_ns": round(best, 2),
        "speed_ratio": round(ratio, 3),
        "converged": r.trustworthy,
        "stability_pct": round(r.stability_pct, 2),
        "throttle_floor": float(rec["throttle_floor"]),
    }


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class Suite:
    """Collects results and writes them next to the experiment as JSON."""

    def __init__(self, slug: str, question: str, setup: str = "", globals: dict[str, Any] | None = None) -> None:
        self.slug = slug
        self.question = question
        self.setup = setup
        self.globals = globals or {}
        self.results: list[Result] = []
        self.facts: dict[str, Any] = {}
        self.started_at = time.time()
        self.load_at_start = _loadavg()
        # Timed before any measurement and again in save(), so a suite that
        # started fast and ended slow is visible rather than averaged away.
        self.speed_at_start = calibrate()
        self.speed_at_end: dict[str, Any] | None = None
        print(f"machine speed: {self.speed_at_start['speed_ratio']:.2f}x of best "
              f"({self.speed_at_start['observed_ns']:.2f} ns against a "
              f"{self.speed_at_start['best_ns']:.2f} ns record)", flush=True)

    def run(self, name: str, stmt: str, **kw: Any) -> Result:
        kw.setdefault("setup", self.setup)
        kw.setdefault("globals", self.globals)
        r = bench(name, stmt, **kw)

        # A measurement whose halves disagree has not been sampled enough.
        # Re-run it once with three times the trials and keep that result
        # unconditionally -- keeping whichever of the two looked better would
        # be selecting on the very statistic used to decide publishability.
        if not r.trustworthy:
            print(f"{'  re-sampling (unstable)':<42} {r.stability_pct:>9.1f}%", flush=True)
            r = bench(name, stmt, **{**kw, "trials": r.trials * 3})
            r.extra["resampled"] = True

        self.results.append(r)
        print(r, flush=True)
        return r

    def record(self, key: str, value: Any) -> None:
        """Attach a non-timing measurement (a size, a count, a ratio)."""
        self.facts[key] = value
        print(f"{key:<42} {value}", flush=True)

    def get(self, name: str) -> Result | None:
        return next((r for r in self.results if r.name == name), None)

    def save(self, path: str | Path | None = None) -> Path:
        out = Path(path) if path else Path(__file__).resolve().parents[1] / self.slug / "results.json"
        out.parent.mkdir(parents=True, exist_ok=True)

        # Gate on the *worse* of the two calibrations. Taking the better one
        # would certify a suite that ran fast for five seconds and slowly for
        # the next forty minutes, which is precisely the failure this check
        # exists to catch.
        self.speed_at_end = calibrate()
        speed = min(self.speed_at_start["speed_ratio"], self.speed_at_end["speed_ratio"])
        floor = self.speed_at_start["throttle_floor"]
        throttled = speed < floor

        payload = {
            "slug": self.slug,
            "question": self.question,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "environment": environment(),
            "noise_floor_ns": NOISE_FLOOR_NS,
            "load_average_at_start": self.load_at_start,
            "load_average_at_end": _loadavg(),
            "machine_speed": {
                "ratio": round(speed, 3),
                "throttled": throttled,
                "floor": floor,
                "at_start": self.speed_at_start,
                "at_end": self.speed_at_end,
                "what_this_means": (
                    "Fraction of this machine's best-ever observed speed on a fixed "
                    "reference kernel. Absolute figures from a run below the floor "
                    "read high by roughly 1/ratio and must not be published as costs."
                ),
            },
            "unstable_results": [r.name for r in self.results if not r.trustworthy],
            "facts": self.facts,
            "results": [asdict(r) for r in self.results],
        }
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwrote {out}", flush=True)
        if throttled:
            print(f"\n!! MACHINE THROTTLED: this run got {speed:.2f}x of the best speed "
                  f"ever recorded (floor {floor}).\n"
                  f"   Every absolute figure here reads roughly {1 / speed:.1f}x high. "
                  f"Ratios within the run are also distorted:\n"
                  f"   the 2026-09-03 comparison found per-measurement inflation "
                  f"ranging from 2.1x to 3.6x, so it does not divide out.\n"
                  f"   Do not publish costs from this run. Re-measure when the machine "
                  f"is quiet.", flush=True)
        bad = payload["unstable_results"]
        if bad:
            print(f"\n!! {len(bad)} of {len(self.results)} measurements did not converge "
                  f"and must not be published:", flush=True)
            for name in bad:
                print(f"     {name}", flush=True)
        return out

    def table(self, baseline: str | None = None) -> str:
        """Markdown table, ready to paste into a post."""
        b = self.get(baseline) if baseline else None
        base = b.ns_per_op if b and b.ns_per_op > 0 else None
        header = "| Operation | ns/op | vs baseline |" if base else "| Operation | ns/op | Spread |"
        rows = []
        for r in self.results:
            if base:
                ratio = "--" if r.below_noise_floor else f"{r.ns_per_op / base:.2f}x"
                rows.append(f"| {r.name} | {r.format_ns()} | {ratio} |")
            else:
                rows.append(f"| {r.name} | {r.format_ns()} | {r.spread_pct:.1f}% |")
        return "\n".join([header, "|---|--:|--:|", *rows])
