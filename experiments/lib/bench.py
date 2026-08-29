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

* **Everything is recorded.** Results carry machine, interpreter and
  parameters, so a published number can always be traced back to its run.
"""

from __future__ import annotations

import gc
import json
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
    spread_pct: float         # (median - min) / min, as a noise indicator
    iterations: int
    trials: int
    below_noise_floor: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def format_ns(self) -> str:
        if self.below_noise_floor:
            return "<0.5"
        if self.ns_per_op < 10:
            return f"{self.ns_per_op:.2f}"
        return f"{self.ns_per_op:.1f}"

    def __str__(self) -> str:
        flag = "  (below noise floor)" if self.below_noise_floor else ""
        return (f"{self.name:<42} {self.format_ns():>9} ns"
                f"   (median {self.median_ns:>8.2f}, spread {self.spread_pct:>5.1f}%){flag}")


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


def bench(
    name: str,
    stmt: str,
    *,
    setup: str = "",
    globals: dict[str, Any] | None = None,
    trials: int = 9,
    min_trial_s: float = 0.05,
    subtract_loop: bool = True,
    note: str = "",
    **extra: Any,
) -> Result:
    """Measure the marginal per-iteration cost of `stmt` in nanoseconds.

    `stmt` and `setup` are Python source, inlined into a timing loop. Names
    referenced by `stmt` must be defined in `setup` or supplied via `globals`.
    """
    g = globals if globals is not None else {}
    inner, _ = _compile(stmt, setup, g)
    empty, _ = _compile("pass", setup, g)
    timer = time.perf_counter_ns

    # Calibrate: grow the iteration count until one trial runs long enough that
    # timer resolution and one-off effects are a negligible fraction of it.
    iterations = 1
    while iterations < 100_000_000:
        elapsed = inner(range(iterations), timer, g)
        if elapsed >= min_trial_s * 1e9:
            break
        growth = max(2.0, min(10.0, (min_trial_s * 1e9) / max(elapsed, 1)))
        iterations = int(iterations * growth) + 1

    # A GC pause mid-trial is real cost in production but pure noise here,
    # where allocation rates are unrepresentative of any real workload.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        gc.collect()
        it = range(iterations)
        samples = [inner(it, timer, g) / iterations for _ in range(trials)]
        overhead = 0.0
        if subtract_loop:
            base = [empty(it, timer, g) / iterations for _ in range(max(3, trials // 2))]
            overhead = min(base)
    finally:
        if gc_was_enabled:
            gc.enable()

    lo = min(samples) - overhead
    med = statistics.median(samples) - overhead
    below = lo < NOISE_FLOOR_NS
    lo = max(0.0, lo)
    med = max(0.0, med)
    spread = ((med - lo) / lo * 100) if lo > NOISE_FLOOR_NS else 0.0

    return Result(
        name=name, ns_per_op=lo, median_ns=med, spread_pct=spread,
        iterations=iterations, trials=trials, below_noise_floor=below,
        note=note, extra=extra,
    )


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

    def run(self, name: str, stmt: str, **kw: Any) -> Result:
        kw.setdefault("setup", self.setup)
        kw.setdefault("globals", self.globals)
        r = bench(name, stmt, **kw)
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
        payload = {
            "slug": self.slug,
            "question": self.question,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "environment": environment(),
            "noise_floor_ns": NOISE_FLOOR_NS,
            "facts": self.facts,
            "results": [asdict(r) for r in self.results],
        }
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwrote {out}", flush=True)
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
