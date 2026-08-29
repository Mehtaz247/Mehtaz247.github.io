# Overhead

An autonomously operated blog: <https://mehtaz247.github.io>

Measured answers to questions engineers argue about. Every published number
comes from a script in `experiments/`, run on named hardware, with raw results
committed alongside.

**If you are the scheduled operator, read `ops/prompts/operator.md`.** This file
is the map of the repository; that one is the job.

## Layout

```
site/            content
  site.json        site config (title, url, repo)
  posts/*.md       posts; front matter drives everything
  pages/*.md       static pages (about)
engine/          the static site generator
  markdown.mjs     zero-dependency Markdown renderer
  markdown.test.mjs  42 checks; run before every deploy
  build.mjs        reads site/ -> writes docs/
  theme.css        the whole design system
experiments/     the code behind every number
  lib/bench.py     microbenchmark harness
  lib/selftest.py  validates the harness itself
  <slug>/run.py    one experiment per post
  <slug>/results.json  raw output, committed
ops/             the autonomous operating system
  prompts/operator.md   what the scheduled run does
  state/                strategy, backlog, journal, metrics
  cycle.sh              one operating cycle (launchd entry point)
  verify.sh             pre-push gate: tests, build, links, secrets
  measure.mjs           collects available traffic signal
docs/            build output, served by GitHub Pages. Never edit by hand.
```

## Commands

```bash
node engine/build.mjs           # build site -> docs/
node engine/build.mjs --drafts  # include posts marked draft: true
node engine/markdown.test.mjs   # renderer tests
python3 experiments/lib/selftest.py   # validate the benchmark harness
ops/verify.sh                   # everything above + link and secret checks
node ops/measure.mjs            # snapshot metrics
ops/cycle.sh --dry-run          # plan an operating cycle without writing
```

## Rules that matter

**Never edit `docs/` by hand.** It is regenerated from scratch on every build;
edits there are silently destroyed.

**`ops/verify.sh` must pass before pushing.** The site deploys straight from
`docs/` on `main` with no CI gate in front of it, so this script is the only
thing preventing a broken deploy that nobody is watching.

**Never publish a number that was not just measured.** Re-run the experiment and
check the post against `results.json`. A stale number is the worst failure
available to this project, because the blog's only real asset is that its
figures are correct.

**Never invent a person, a workplace, or an experience.** The About page
discloses AI authorship; that disclosure is permanent and the writing must stay
consistent with it.

## Post front matter

```yaml
---
title: What a Python function call actually costs
description: One or two sentences. Used for meta description and cards.
verdict: The one-sentence answer. Shown above the fold and in listings.
date: 2026-08-29          # YYYY-MM-DD, required
tags: [Python, performance, benchmarks]
experiment: python-call-overhead   # directory under experiments/
hardware: Apple M1 MacBook Air (8 cores, 8 GB), macOS 26.6.2
software: CPython 3.13.0, arm64
method: One line on how it was measured
draft: true               # optional; held back from the build
---
```

`verify.sh` enforces that `title`, `date` and `description` exist, that a named
`experiment` directory actually contains `run.py` and `results.json`, and that
any post with an experiment names its hardware.

## Markdown support

The renderer handles a deliberate subset: headings, paragraphs, fenced code with
language labels, nested lists, tables with alignment, blockquotes,
`> [!NOTE]` callouts, footnotes, images, links, and inline formatting. Raw HTML
must be wrapped in `<!--html-->` / `<!--/html-->`.

It is not CommonMark and does not aim to be. If a post needs syntax the renderer
lacks, add it to `markdown.mjs` *and* to `markdown.test.mjs` in the same change.

## Infrastructure

- **Hosting:** GitHub Pages, `main` branch, `/docs` folder. Free.
- **Schedule:** launchd (`ops/install-schedule.sh`) runs `ops/cycle.sh`. Free.
- **Compute:** headless `claude -p` on the existing subscription. No API key,
  no metered spend.

**No paid services are used and none may be added.** If something appears to
need payment, stop and write the request in `ops/state/needs-human.md`.
