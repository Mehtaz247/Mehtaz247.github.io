#!/usr/bin/env node
// Collects whatever signal is available about how the blog is doing, and
// appends it to ops/state/metrics.json as a time series.
//
// The site is static and carries no tracking script, so there is no pageview
// data and there will not be unless a human sets up an analytics property.
// What IS available with zero setup is the GitHub traffic API, which reports
// views and unique visitors for the repository -- and since the repository IS
// the site (Pages serves from docs/ on main), that is a genuine, if partial,
// readership signal.
//
// Run: node ops/measure.mjs

import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const METRICS = join(ROOT, 'ops', 'state', 'metrics.json');
const config = JSON.parse(readFileSync(join(ROOT, 'site', 'site.json'), 'utf8'));
const REPO = config.repo.replace('https://github.com/', '');

// gh carries the auth; shelling out to it avoids handling a token in this file
// at all, which is the safest way to not leak one.
function gh(path) {
  try {
    return JSON.parse(execFileSync('gh', ['api', path], { encoding: 'utf8', timeout: 30000 }));
  } catch (err) {
    const msg = String(err.stderr || err.message).split('\n')[0];
    console.error(`  ! gh api ${path}: ${msg}`);
    return null;
  }
}

const today = new Date().toISOString().slice(0, 10);
console.log(`measuring ${REPO} on ${today}\n`);

// --- repository signals ----------------------------------------------------

const repo = gh(`repos/${REPO}`);
const views = gh(`repos/${REPO}/traffic/views`);
const clones = gh(`repos/${REPO}/traffic/clones`);
const referrers = gh(`repos/${REPO}/traffic/popular/referrers`);
const paths = gh(`repos/${REPO}/traffic/popular/paths`);
const issues = gh(`repos/${REPO}/issues?state=all&per_page=100`);

// --- content inventory -----------------------------------------------------
// Cheap to compute and it makes every traffic number interpretable: 200 views
// across 3 posts and across 30 posts mean very different things.

const postsDir = join(ROOT, 'site', 'posts');
const postFiles = existsSync(postsDir) ? readdirSync(postsDir).filter((f) => f.endsWith('.md')) : [];
const drafts = postFiles.filter((f) => /^draft:\s*true$/m.test(readFileSync(join(postsDir, f), 'utf8')));
const experiments = existsSync(join(ROOT, 'experiments'))
  ? readdirSync(join(ROOT, 'experiments')).filter((d) => existsSync(join(ROOT, 'experiments', d, 'results.json')))
  : [];

// Issues opened by anyone other than the repo owner are the closest thing to
// the "reproductions" signal that strategy.json names as primary.
const owner = REPO.split('/')[0].toLowerCase();
const externalIssues = (issues || []).filter((i) => (i.user?.login || '').toLowerCase() !== owner);

const snapshot = {
  date: today,
  posts_published: postFiles.length - drafts.length,
  drafts: drafts.length,
  experiments_with_results: experiments.length,

  stars: repo?.stargazers_count ?? null,
  forks: repo?.forks_count ?? null,
  watchers: repo?.subscribers_count ?? null,

  // GitHub's traffic API only ever returns a trailing 14-day window, so these
  // are rolling totals, not cumulative. Do not sum them across snapshots.
  views_14d: views?.count ?? null,
  unique_visitors_14d: views?.uniques ?? null,
  clones_14d: clones?.count ?? null,
  unique_cloners_14d: clones?.uniques ?? null,

  referrers: (referrers || []).slice(0, 8).map((r) => ({ source: r.referrer, views: r.count, uniques: r.uniques })),
  top_paths: (paths || []).slice(0, 8).map((p) => ({ path: p.path, views: p.count, uniques: p.uniques })),

  issues_total: (issues || []).length,
  issues_from_others: externalIssues.length,
  reproductions: externalIssues
    .filter((i) => /reproduc|benchmark|number|result|measur|disagree/i.test(i.title + ' ' + (i.body || '')))
    .map((i) => ({ number: i.number, title: i.title, user: i.user?.login })),
};

// --- append ----------------------------------------------------------------

let history = { note: '', snapshots: [] };
if (existsSync(METRICS)) {
  try {
    history = JSON.parse(readFileSync(METRICS, 'utf8'));
  } catch {
    console.error('  ! metrics.json unparseable; starting a fresh series');
  }
}
history.note = 'Time series of whatever signal is available. GitHub traffic figures are ' +
  'rolling 14-day windows, not cumulative totals -- never sum them across snapshots. ' +
  'Pageview data requires an analytics property that only a human can create; ' +
  'see ops/state/needs-human.md.';
history.snapshots = (history.snapshots || []).filter((s) => s.date !== today);
history.snapshots.push(snapshot);
history.snapshots.sort((a, b) => a.date.localeCompare(b.date));

writeFileSync(METRICS, JSON.stringify(history, null, 2) + '\n');

// --- report ----------------------------------------------------------------

const prev = history.snapshots[history.snapshots.length - 2];
const delta = (key) => {
  if (!prev || prev[key] == null || snapshot[key] == null) return '';
  const d = snapshot[key] - prev[key];
  return d === 0 ? '  (=)' : `  (${d > 0 ? '+' : ''}${d} since ${prev.date})`;
};

const line = (label, value, key) =>
  console.log(`  ${label.padEnd(26)} ${String(value ?? '--').padStart(6)}${key ? delta(key) : ''}`);

line('posts published', snapshot.posts_published, 'posts_published');
line('experiments with data', snapshot.experiments_with_results, 'experiments_with_results');
line('repo views (14d)', snapshot.views_14d, 'views_14d');
line('unique visitors (14d)', snapshot.unique_visitors_14d, 'unique_visitors_14d');
line('stars', snapshot.stars, 'stars');
line('forks', snapshot.forks, 'forks');
line('issues from others', snapshot.issues_from_others, 'issues_from_others');

if (snapshot.referrers.length) {
  console.log('\n  referrers:');
  for (const r of snapshot.referrers) console.log(`    ${String(r.uniques).padStart(5)} uniques  ${r.source}`);
}
if (snapshot.reproductions.length) {
  console.log('\n  possible reproductions (primary metric -- read these):');
  for (const r of snapshot.reproductions) console.log(`    #${r.number} ${r.title} (@${r.user})`);
}

console.log(`\nwrote ${METRICS} (${history.snapshots.length} snapshots)`);
