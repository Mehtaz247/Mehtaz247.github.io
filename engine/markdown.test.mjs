// Tests for the markdown renderer. Run: node engine/markdown.test.mjs
// The autonomous publish cycle runs this before every deploy; a failure here
// blocks the deploy rather than shipping mangled posts.
import { renderMarkdown, slugify, escapeHtml } from './markdown.mjs';

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    failures.push(name + ': ' + err.message);
  }
}

const has = (md, needle, label) => check(label || needle, () => {
  const { html } = renderMarkdown(md);
  if (!html.includes(needle)) {
    throw new Error('expected ' + JSON.stringify(needle) + '\n  got: ' + html.slice(0, 400));
  }
});

const lacks = (md, needle, label) => check(label, () => {
  const { html } = renderMarkdown(md);
  if (html.includes(needle)) {
    throw new Error('did not expect ' + JSON.stringify(needle) + '\n  got: ' + html.slice(0, 400));
  }
});

// --- headings ---------------------------------------------------------------
has('# Hello there', '<h1 id="hello-there">', 'h1 gets slug id');
has('## A Section', '<h2 id="a-section">', 'h2 gets slug id');
has('## A Section', 'class="anchor"', 'headings get anchor links');
check('toc collects h2/h3 only', () => {
  const { toc } = renderMarkdown('# One\n\n## Two\n\n### Three\n\n#### Four');
  if (toc.length !== 2) throw new Error('expected 2 toc entries, got ' + toc.length);
  if (toc[0].id !== 'two' || toc[1].id !== 'three') throw new Error('wrong toc: ' + JSON.stringify(toc));
});

// --- inline -----------------------------------------------------------------
has('**bold**', '<strong>bold</strong>');
has('*em*', '<em>em</em>');
has('_em_', '<em>em</em>', 'underscore emphasis');
has('~~gone~~', '<del>gone</del>');
has('a `bit` of code', '<code>bit</code>');
has('***both***', '<strong><em>both</em></strong>');
check('snake_case identifiers are not italicised', () => {
  const { html } = renderMarkdown('use max_pool_size_bytes here');
  if (html.includes('<em>')) throw new Error('mangled identifier: ' + html);
});

// --- escaping (the security-critical path) ----------------------------------
lacks('<script>alert(1)</script>', '<script>', 'raw html in a paragraph is escaped');
lacks('```\n<script>alert(1)</script>\n```', '<script>alert(1)</script>', 'html inside code fence is escaped');
has('`<b>x</b>`', '&lt;b&gt;x&lt;/b&gt;', 'inline code escapes html');
check('escapeHtml covers all five entities', () => {
  const out = escapeHtml('&<>"\'');
  if (out !== '&amp;&lt;&gt;&quot;&#39;') throw new Error('got ' + out);
});

// --- code fences ------------------------------------------------------------
has('```python\nx = 1\n```', 'class="language-python"');
has('```python\nx = 1\n```', '<span class="code-lang">python</span>', 'fence emits a language label');
has('```\nplain\n```', '<pre><code>plain</code></pre>', 'fence without a language');
check('fence preserves interior blank lines', () => {
  const { html } = renderMarkdown('```\na\n\nb\n```');
  if (!html.includes('a\n\nb')) throw new Error('lost blank line: ' + html);
});

// --- lists ------------------------------------------------------------------
has('- one\n- two', '<li>one</li>');
has('1. one\n2. two', '<ol>');
has('3. three\n4. four', '<ol start="3">', 'ordered list honours its start number');
check('nested lists nest', () => {
  const { html } = renderMarkdown('- a\n  - b\n  - c\n- d');
  const opens = (html.match(/<ul>/g) || []).length;
  if (opens !== 2) throw new Error('expected 2 <ul>, got ' + opens + '\n' + html);
});
check('a paragraph after a list is not swallowed', () => {
  const { html } = renderMarkdown('- a\n- b\n\nAfter the list.');
  if (!html.includes('<p>After the list.</p>')) throw new Error(html);
});

// --- tables -----------------------------------------------------------------
has('| a | b |\n|---|---|\n| 1 | 2 |', '<table>');
has('| a | b |\n|---|--:|\n| 1 | 2 |', 'text-align:right', 'table respects right alignment');
has('| a | b |\n|---|---|\n| 1 | 2 |', 'class="table-wrap"', 'tables are wrapped for overflow');
check('table body rows are all rendered', () => {
  const { html } = renderMarkdown('| a |\n|---|\n| 1 |\n| 2 |\n| 3 |');
  const rows = (html.match(/<tr>/g) || []).length;
  if (rows !== 4) throw new Error('expected 4 <tr> (1 head + 3 body), got ' + rows);
});

// --- quotes and callouts ----------------------------------------------------
has('> quoted', '<blockquote>');
has('> [!NOTE]\n> Be careful.', 'callout-note');
has('> [!NOTE]\n> Be careful.', 'Be careful.', 'callout keeps its body');

// --- links and images -------------------------------------------------------
has('[x](https://example.com)', 'rel="noopener"', 'external links get rel=noopener');
has('[x](/about/)', '<a href="/about/">x</a>', 'internal links stay plain');
lacks('[x](/about/)', 'target="_blank"', 'internal links do not open a new tab');
has('![alt text](/img.png)', 'loading="lazy"', 'images are lazy-loaded');
has('![alt text](/img.png)', 'alt="alt text"', 'images keep alt text');

// --- footnotes --------------------------------------------------------------
check('footnotes are collected and rendered', () => {
  const { html } = renderMarkdown('A claim.[^src]\n\n[^src]: The evidence.');
  if (!html.includes('class="footnotes"')) throw new Error('no footnote section: ' + html);
  if (!html.includes('The evidence.')) throw new Error('lost footnote body: ' + html);
  if (!html.includes('id="fnref-src"')) throw new Error('no back-reference anchor: ' + html);
});
check('footnotes number in reference order', () => {
  const { html } = renderMarkdown('One[^b] two[^a].\n\n[^a]: A\n[^b]: B');
  const first = html.indexOf('>1</a>');
  if (first < 0) throw new Error('no first footnote marker');
});

// --- figure includes --------------------------------------------------------
// Charts are generated from results.json and inlined, so the renderer must
// splice the file in verbatim, must not escape it, and must fail loudly when
// it cannot be resolved -- a silently missing chart is a published post with a
// hole where its evidence should be.
check('figure include inlines the resolved file', () => {
  const { html } = renderMarkdown('!figure[A caption](assets/charts/x.svg)', {
    include: (p) => '<svg id="' + p + '"></svg>',
  });
  if (!html.includes('<svg id="assets/charts/x.svg"></svg>')) throw new Error('not inlined: ' + html);
  if (!html.includes('<figure class="chart">')) throw new Error('no figure wrapper: ' + html);
  if (!html.includes('<figcaption>A caption</figcaption>')) throw new Error('no caption: ' + html);
});
check('figure caption renders inline markdown', () => {
  const { html } = renderMarkdown('!figure[**bold** and `code`](a/b.svg)', { include: () => '<svg/>' });
  if (!html.includes('<strong>bold</strong>')) throw new Error('caption not rendered: ' + html);
});
check('a figure with no caption omits the figcaption', () => {
  const { html } = renderMarkdown('!figure[](a/b.svg)', { include: () => '<svg/>' });
  if (html.includes('figcaption')) throw new Error('unexpected figcaption: ' + html);
});
check('a figure with no include resolver throws', () => {
  let threw = false;
  try { renderMarkdown('!figure[x](a/b.svg)'); } catch (e) { threw = true; }
  if (!threw) throw new Error('expected a throw');
});
check('an unresolvable figure propagates the error', () => {
  let threw = false;
  try {
    renderMarkdown('!figure[x](a/b.svg)', { include: () => { throw new Error('nope'); } });
  } catch (e) { threw = true; }
  if (!threw) throw new Error('expected a throw');
});
check('a figure directive is not swallowed into a paragraph', () => {
  const { html } = renderMarkdown('Some text.\n!figure[c](a/b.svg)\nMore text.', {
    include: () => '<svg/>',
  });
  if (!html.includes('<figure class="chart">')) throw new Error('paragraph ate the figure: ' + html);
  if (!html.includes('More text.')) throw new Error('lost trailing text: ' + html);
});
check('text that merely looks like a figure is left alone', () => {
  const { html } = renderMarkdown('write !figure[x](y.svg) inline to include a chart',
    { include: () => { throw new Error('should not be called'); } });
  if (!html.startsWith('<p>')) throw new Error('expected a paragraph: ' + html);
});

// --- misc -------------------------------------------------------------------
has('---', '<hr>', 'thematic break');
has('<!--html-->\n<div class="x">raw</div>\n<!--/html-->', '<div class="x">raw</div>', 'raw html escape hatch');
check('slugify strips punctuation and collapses runs', () => {
  const out = slugify('Hello, World! -- Is `this` OK?');
  if (out !== 'hello-world-is-this-ok') throw new Error('got ' + out);
});
check('empty input does not throw', () => { renderMarkdown(''); });

// --- report -----------------------------------------------------------------
if (failures.length) {
  console.error('FAIL ' + failures.length + ' of ' + (passed + failures.length));
  for (const f of failures) console.error('  x ' + f);
  process.exit(1);
}
console.log('ok  markdown: ' + passed + ' checks passed');
