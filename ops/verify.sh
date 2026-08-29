#!/usr/bin/env bash
# Pre-push verification gate for Overhead.
#
# The autonomous loop modifies its own engine and prompts. This script is the
# thing standing between a bad edit and a broken site that nobody is watching.
# It must pass before any push. Exit non-zero blocks the cycle.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAILED=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$1"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }

# --- 1. engine tests -------------------------------------------------------
step "markdown renderer"
if out=$(node engine/markdown.test.mjs 2>&1); then ok "$out"; else bad "$out"; fi

# --- 2. benchmark harness --------------------------------------------------
# A harness that reports confident wrong numbers is the worst failure available
# to this project, so it is checked on every cycle, not just when it changes.
step "benchmark harness self-test"
if out=$(python3 experiments/lib/selftest.py 2>&1); then
  ok "$(echo "$out" | tail -1)"
else
  bad "$(echo "$out" | tail -20)"
fi

# --- 3. build --------------------------------------------------------------
step "site build"
if out=$(node engine/build.mjs 2>&1); then ok "$out"; else bad "$out"; fi

# --- 4. structural checks on the built output ------------------------------
step "output integrity"
for f in docs/index.html docs/feed.xml docs/sitemap.xml docs/robots.txt docs/.nojekyll docs/theme.css docs/about/index.html; do
  [ -f "$f" ] && ok "$f" || bad "missing $f"
done

# An unparseable feed silently drops every subscriber, and nothing else would
# catch it -- the build happily emits malformed XML.
if command -v xmllint >/dev/null 2>&1; then
  for x in docs/feed.xml docs/sitemap.xml; do
    if xmllint --noout "$x" 2>/dev/null; then ok "$x is well-formed XML"; else bad "$x is malformed XML"; fi
  done
else
  printf '   skip  xmllint not available\n'
fi

# --- 5. every post must have a reproducible experiment ---------------------
step "post integrity"
node - <<'NODE'
const { readdirSync, readFileSync, existsSync } = require('node:fs');
let bad = 0;
const dir = 'site/posts';
for (const f of readdirSync(dir).filter((f) => f.endsWith('.md'))) {
  const raw = readFileSync(`${dir}/${f}`, 'utf8');
  const fm = (raw.match(/^---\n([\s\S]*?)\n---/) || [])[1] || '';
  const get = (k) => (fm.match(new RegExp(`^${k}:\\s*(.*)$`, 'm')) || [])[1]?.trim();

  for (const key of ['title', 'date', 'description']) {
    if (!get(key)) { console.log(`   FAIL ${f}: missing front matter "${key}"`); bad = 1; }
  }
  // The blog's entire claim is that numbers are reproducible. A post citing an
  // experiment directory that does not exist breaks that claim silently.
  const exp = get('experiment');
  if (exp) {
    for (const need of [`experiments/${exp}/run.py`, `experiments/${exp}/results.json`]) {
      if (!existsSync(need)) { console.log(`   FAIL ${f}: experiment missing ${need}`); bad = 1; }
    }
  }
  if (get('draft') !== 'true' && !get('hardware') && exp) {
    console.log(`   FAIL ${f}: benchmark post must name its hardware`); bad = 1;
  }
}
if (!bad) console.log('   ok   all posts have valid front matter and present experiments');
process.exit(bad);
NODE
[ $? -ne 0 ] && FAILED=1

# --- 6. internal links must resolve ----------------------------------------
step "internal links"
node - <<'NODE'
const { readdirSync, readFileSync, existsSync, statSync } = require('node:fs');
const { join } = require('node:path');

const files = [];
(function walk(d) {
  for (const e of readdirSync(d)) {
    const p = join(d, e);
    statSync(p).isDirectory() ? walk(p) : p.endsWith('.html') && files.push(p);
  }
})('docs');

let broken = 0;
for (const f of files) {
  const html = readFileSync(f, 'utf8');
  for (const m of html.matchAll(/href="(\/[^"#?]*)"/g)) {
    const href = m[1];
    const target = href.endsWith('/') ? join('docs', href, 'index.html') : join('docs', href);
    if (!existsSync(target)) { console.log(`   FAIL ${f} -> ${href}`); broken++; }
  }
}
if (!broken) console.log(`   ok   ${files.length} pages, no broken internal links`);
process.exit(broken ? 1 : 0);
NODE
[ $? -ne 0 ] && FAILED=1

# --- 7. secret scan --------------------------------------------------------
# Cheap insurance against a token reaching a public repository.
step "secret scan"
if git grep -nIE '(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY)' -- . ':!ops/verify.sh' >/dev/null 2>&1; then
  bad "possible credential found in tracked files"
  git grep -nIE '(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{20,})' -- . ':!ops/verify.sh' | head -5
else
  ok "no credential patterns in tracked files"
fi

# --- verdict ---------------------------------------------------------------
if [ "$FAILED" -ne 0 ]; then
  printf '\n\033[31mVERIFY FAILED\033[0m -- do not push.\n'
  exit 1
fi
printf '\n\033[32mVERIFY PASSED\033[0m\n'
