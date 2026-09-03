#!/usr/bin/env python3
"""Check that every figure quoted in a post exists in its experiment's results.

The worst failure available to this blog is a published number that no longer
matches the run behind it, and the way that happens is mundane: the experiment
gets re-run, the numbers move a little, and one sentence in the middle of the
post keeps the old value. Eyes do not catch that. This does.

For each `123 ns` / `1.9 µs` / `4.1 ms` literal and each `12.5x` ratio in the
post, it looks for something in `results.json` that rounds to it -- a measured
`ns_per_op`, a recorded fact, or a ratio between two measurements. Anything with
no match is reported.

It is deliberately not part of `verify.sh`. A post may legitimately quote a
number from outside its own experiment, and a check that blocks the deploy for
that would be trained away rather than fixed. Run it by hand before publishing
and account for every line it prints.

It also refuses a post whose results were measured while the machine was
running well below its recorded speed, unless the post discloses that. See
`experiments/lib/reference.json` for why that check exists.

**Know what this does not catch.** It asks whether *some* value in the results
rounds to each figure, not whether the *right* one does. A sentence saying
"113 ns" where it should say "112 ns" passes silently if any other measurement
happens to be 113 ns -- which is exactly what happened on 2026-08-31, and it was
caught by reading the rendered page instead. This tool removes the tedious half
of the job; it does not remove the reading.

Usage: python3 ops/check-figures.py site/posts/<slug>.md
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TIME = re.compile(r"(?<![\w.])([\d,]+(?:\.\d+)?)\s*(ns|µs|us|ms)\b")
RATIO = re.compile(r"(?<![\w.])([\d,]+(?:\.\d+)?)x\b")
UNITS = {"ns": 1.0, "µs": 1e3, "us": 1e3, "ms": 1e6}


def rounds_to(claimed: float, actual: float, decimals: int) -> bool:
    """Whether `claimed` is what `actual` looks like printed at `decimals`.

    Compared by distance rather than through `round()`, which disagrees with
    itself on exact halves: `round(56.785, 2)` is 56.78 because 56.785 has no
    exact binary representation, and a post printing "56.79 ms" would be
    reported as unsourced.
    """
    return abs(actual - claimed) <= 0.5 * 10 ** -decimals + 1e-9


def main(post_path: str) -> int:
    text = Path(post_path).read_text()
    fm = re.search(r"^---\n(.*?)\n---\n", text, re.S)
    slug = re.search(r"^experiment:\s*(\S+)", fm.group(1), re.M) if fm else None
    if not slug:
        print("post declares no experiment; nothing to check")
        return 0

    exp = ROOT / "experiments" / slug.group(1)
    data = json.loads((exp / "results.json").read_text())

    # A post that corrects itself quotes figures from more than one run of the
    # same experiment -- "it read 13.4 ns in August and 36.2 ns in September" is
    # the correction. Every committed run of the experiment is a legitimate
    # source, so all of them are loaded; results.json is still the run the post
    # reports and the only one the gates below look at.
    runs = [data] + [json.loads(f.read_text())
                     for f in sorted(exp.glob("results-*.json"))]

    # Corroboration from outside the harness is quoted in posts too, and it is
    # committed for exactly that reason, so it counts as a source.
    cross = exp / "crosscheck.json"
    extra_facts: list[float] = []
    if cross.exists():
        def _numbers(o):
            if isinstance(o, bool):
                return
            if isinstance(o, (int, float)):
                yield float(o)
            elif isinstance(o, dict):
                for v in o.values():
                    yield from _numbers(v)
            elif isinstance(o, list):
                for v in o:
                    yield from _numbers(v)
        extra_facts = list(_numbers(json.loads(cross.read_text())))
    measured = [r["ns_per_op"] for d in runs for r in d["results"]]
    facts = ([v for d in runs for v in d["facts"].values() if isinstance(v, (int, float))]
             + extra_facts)
    values = measured + facts
    # Differences and ratios between measurements are quoted constantly ("the
    # gap is 112 ns", "4,412x"), so they count as sourced too.
    derived = [a - b for a in measured for b in measured if a > b]
    ratios = [a / b for a in measured for b in measured if b > 0] + facts

    # Per-unit figures -- "12 ns per row", "91 ns per column per row" -- are a
    # measurement (or a difference between two) divided by a count that the
    # experiment recorded. Without this, every such figure reads as unsourced,
    # which is how a checker gets ignored.
    counts = sorted({int(v) for v in facts if isinstance(v, int) and 2 <= v <= 10_000_000})
    derived += [x / c for x in list(derived) + measured for c in counts]

    body = text[fm.end():] if fm else text
    body = re.sub(r"```.*?```", "", body, flags=re.S)   # code blocks quote source, not results

    unmatched = []
    checked = 0
    for m in TIME.finditer(body):
        claimed = float(m.group(1).replace(",", "")) * UNITS[m.group(2)]
        decimals = len((m.group(1).split(".") + [""])[1])
        scale = UNITS[m.group(2)]
        checked += 1
        if not any(rounds_to(claimed / scale, v / scale, decimals) for v in values + derived):
            unmatched.append(f"  {m.group(0)!r}  (line {body[:m.start()].count(chr(10)) + 1})")

    for m in RATIO.finditer(body):
        claimed = float(m.group(1).replace(",", ""))
        decimals = len((m.group(1).split(".") + [""])[1])
        checked += 1
        if not any(rounds_to(claimed, r, decimals) for r in ratios):
            unmatched.append(f"  {m.group(0)!r}  (line {body[:m.start()].count(chr(10)) + 1})")

    print(f"{post_path}: {checked} figures checked against {slug.group(1)}/results.json")

    # A run taken while the machine was delivering a fraction of its normal
    # speed is precise, reproducible and wrong: on 2026-09-03 a whole suite came
    # back 2.1-3.6x slow with every measurement converging. If the run was
    # throttled, the post has to say so, on the same terms as an unconverged
    # measurement -- disclose it or do not publish the figure.
    speed = data.get("machine_speed")
    if speed and speed.get("throttled"):
        disclosed = "machine was running slowly" in text
        print(f"\n{'note' if disclosed else '!!'}: this run was measured at "
              f"{speed['ratio']:.2f}x of the machine's best recorded speed "
              f"(floor {speed['floor']}); absolute figures read "
              f"~{1 / speed['ratio']:.1f}x high.")
        if not disclosed:
            print("   the post does not contain the phrase 'machine was running slowly'; "
                  "either re-measure on a quiet machine or disclose it.")
            return 1
        print("   the post discloses this.")
    elif speed:
        print(f"machine speed during that run: {speed['ratio']:.2f}x of the best recorded")
    else:
        print("note: this run predates the machine-speed reference; its absolute "
              "figures have no accuracy warrant.")

    if data.get("unstable_results"):
        # Quoting one of these is only acceptable if the post says so. The
        # disclosure phrase is the contract: no disclosure, no publication.
        disclosed = "did not converge" in text
        print(f"\n{'note' if disclosed else '!!'}: {len(data['unstable_results'])} measurement(s) "
              f"did not converge:")
        for n in data["unstable_results"]:
            print(f"     {n}")
        if not disclosed:
            print("   the post does not contain the phrase 'did not converge'; "
                  "either drop these figures or disclose them.")
            return 1
        print("   the post discloses this. Check each one is marked where it appears.")
    if unmatched:
        print(f"\n{len(unmatched)} figure(s) with no source in results.json -- "
              f"account for each one before publishing:")
        print("\n".join(unmatched))
        return 1
    print("every figure traces back to a measured value")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
