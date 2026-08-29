// Zero-dependency Markdown renderer.
// Deliberately supports a restricted subset: this engine runs unattended for
// months, and every document it renders is authored inside this repo, so a
// small, auditable renderer beats a general-purpose dependency that can drift.
//
// Supported: ATX headings, paragraphs, fenced code, nested lists,
// blockquotes/callouts, tables, thematic breaks, footnotes, images, links,
// bold/italic/strikethrough, inline code, autolinks, and raw HTML blocks
// fenced by <!--html--> ... <!--/html-->.

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

const escapeAttr = (s) => String(s).replace(/[&<>"]/g, (c) => ESCAPES[c]);

export function slugify(s) {
  return String(s)
    .toLowerCase()
    .replace(/[`*_~]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
}

// ---------------------------------------------------------------------------
// Inline
// ---------------------------------------------------------------------------

// Code spans are extracted first and re-inserted last so their contents are
// never treated as markup. Everything in between operates on escaped text,
// so all HTML emitted here is our own.
function inline(src, ctx) {
  const codes = [];
  let text = String(src).replace(/(`+)([\s\S]*?)\1/g, (_m, _t, code) => {
    codes.push(code.replace(/^ (.*) $/, '$1'));
    return ' CODE' + (codes.length - 1) + ' ';
  });

  text = escapeHtml(text);

  // Images before links: the ! prefix distinguishes them.
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g,
    (_m, alt, src2, title) =>
      '<img src="' + escapeAttr(src2) + '" alt="' + escapeAttr(alt) + '"' +
      (title ? ' title="' + escapeAttr(title) + '"' : '') + ' loading="lazy" decoding="async">');

  text = text.replace(/\[\^([^\]]+)\]/g, (_m, id) => {
    let n = ctx.footnoteOrder.indexOf(id);
    if (n < 0) n = ctx.footnoteOrder.push(id) - 1;
    const s = slugify(id);
    return '<sup class="fnref"><a href="#fn-' + s + '" id="fnref-' + s + '">' + (n + 1) + '</a></sup>';
  });

  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_m, label, href, title) => {
    const external = /^https?:\/\//.test(href);
    const rel = external ? ' rel="noopener" target="_blank"' : '';
    return '<a href="' + escapeAttr(href) + '"' + (title ? ' title="' + escapeAttr(title) + '"' : '') + rel + '>' + label + '</a>';
  });

  text = text.replace(/&lt;(https?:\/\/[^&\s]+)&gt;/g,
    (_m, href) => '<a href="' + escapeAttr(href) + '" rel="noopener" target="_blank">' + href + '</a>');

  text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  text = text.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // Typography, applied after markup so it cannot corrupt an attribute.
  text = text.replace(/---/g, '&mdash;').replace(/(\w)--(\w)/g, '$1&ndash;$2');

  return text.replace(/ CODE(\d+) /g, (_m, i) => '<code>' + escapeHtml(codes[+i]) + '</code>');
}

// ---------------------------------------------------------------------------
// Block
// ---------------------------------------------------------------------------

const indentOf = (line) => line.match(/^ */)[0].length;

// Lists are parsed by indentation depth; multi-line items recurse through
// parseBlocks so nested lists and code blocks inside items work.
function parseList(lines, ctx) {
  const first = lines[0].match(/^(\s*)([-*+]|\d+[.)])\s+/);
  const ordered = /\d/.test(first[2]);
  const baseIndent = first[1].length;
  const items = [];
  let current = null;

  for (const line of lines) {
    const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (m && m[1].length <= baseIndent + 1) {
      if (current) items.push(current);
      current = [m[3]];
    } else if (current) {
      current.push(line.slice(Math.min(indentOf(line), baseIndent + 2)));
    }
  }
  if (current) items.push(current);

  const tag = ordered ? 'ol' : 'ul';
  const startN = ordered ? parseInt(first[2], 10) : 1;
  const start = startN !== 1 ? ' start="' + startN + '"' : '';

  const html = items.map((item) => {
    const body = item.join('\n');
    const complex = /\n\s*(?:[-*+]|\d+[.)])\s|\n\s*\n|\n\s*```/.test(body);
    const rendered = complex
      ? parseBlocks(body.split('\n'), ctx).replace(/^<p>([\s\S]*?)<\/p>/, '$1')
      : inline(body.replace(/\n\s*/g, ' '), ctx);
    return '<li>' + rendered.trim() + '</li>';
  }).join('\n');

  return '<' + tag + start + '>\n' + html + '\n</' + tag + '>';
}

function parseTable(rows, ctx) {
  const cells = (row) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
  const head = cells(rows[0]);
  const aligns = cells(rows[1]).map((s) =>
    s.startsWith(':') && s.endsWith(':') ? 'center' : s.endsWith(':') ? 'right' : s.startsWith(':') ? 'left' : '');
  const align = (i) => (aligns[i] ? ' style="text-align:' + aligns[i] + '"' : '');

  const thead = head.map((c, i) => '<th' + align(i) + '>' + inline(c, ctx) + '</th>').join('');
  const tbody = rows.slice(2).map((r) =>
    '<tr>' + cells(r).map((c, i) => '<td' + align(i) + '>' + inline(c, ctx) + '</td>').join('') + '</tr>').join('\n');

  // Wrapped so a wide table scrolls inside the column, never the page.
  return '<div class="table-wrap"><table>\n<thead><tr>' + thead + '</tr></thead>\n<tbody>\n' + tbody + '\n</tbody>\n</table></div>';
}

function parseBlocks(lines, ctx) {
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    // Escape hatch for hand-written HTML (inline charts, custom figures).
    if (line.trim() === '<!--html-->') {
      const buf = [];
      i++;
      while (i < lines.length && lines[i].trim() !== '<!--/html-->') buf.push(lines[i++]);
      i++;
      out.push(buf.join('\n'));
      continue;
    }

    const fence = line.match(/^(\s*)(```|~~~)\s*([\w+-]*)/);
    if (fence) {
      const marker = fence[2];
      const lang = fence[3];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) buf.push(lines[i++]);
      i++;
      const cls = lang ? ' class="language-' + escapeAttr(lang) + '"' : '';
      const label = lang ? '<span class="code-lang">' + escapeHtml(lang) + '</span>' : '';
      out.push('<figure class="code">' + label + '<pre><code' + cls + '>' + escapeHtml(buf.join('\n')) + '</code></pre></figure>');
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      const raw = heading[2].replace(/\s+#+\s*$/, '');
      const id = slugify(raw);
      if (level >= 2 && level <= 3) ctx.toc.push({ level, id, text: raw.replace(/[`*_]/g, '') });
      out.push('<h' + level + ' id="' + id + '">' + inline(raw, ctx) +
        '<a class="anchor" href="#' + id + '" aria-label="Link to this section">#</a></h' + level + '>');
      i++;
      continue;
    }

    if (/^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$/.test(line)) {
      out.push('<hr>');
      i++;
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
        i++;
      }
      const admon = buf[0] && buf[0].match(/^\[!(\w+)\]\s*(.*)$/);
      if (admon) {
        buf[0] = admon[2];
        const kind = admon[1].toLowerCase();
        out.push('<aside class="callout callout-' + escapeAttr(kind) + '">' +
          '<p class="callout-label">' + escapeHtml(admon[1]) + '</p>' + parseBlocks(buf, ctx) + '</aside>');
      } else {
        out.push('<blockquote>' + parseBlocks(buf, ctx) + '</blockquote>');
      }
      continue;
    }

    // Footnote definition: [^id]: text, with indented continuation lines.
    const fndef = line.match(/^\[\^([^\]]+)\]:\s*(.*)$/);
    if (fndef) {
      const buf = [fndef[2]];
      i++;
      while (i < lines.length && /^\s{2,}\S/.test(lines[i])) buf.push(lines[i++].replace(/^\s{2,}/, ''));
      ctx.footnotes[fndef[1]] = buf.join('\n');
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
      const buf = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) buf.push(lines[i++]);
      out.push(parseTable(buf, ctx));
      continue;
    }

    if (/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) {
      const buf = [];
      while (i < lines.length) {
        const l = lines[i];
        const isItem = /^\s*(?:[-*+]|\d+[.)])\s+/.test(l);
        const isCont = l.trim() && indentOf(l) > 0;
        const isBlankThenMore = !l.trim() && /^\s*(?:[-*+]|\d+[.)])\s+|^\s{2,}\S/.test(lines[i + 1] || '');
        if (!isItem && !isCont && !isBlankThenMore) break;
        buf.push(l);
        i++;
      }
      while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
      out.push(parseList(buf, ctx));
      continue;
    }

    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !/^\s*(?:#{1,6}\s|>|```|~~~|\|)/.test(lines[i]) &&
           !/^\s*(?:[-*+]|\d+[.)])\s+/.test(lines[i]) &&
           !/^\[\^[^\]]+\]:/.test(lines[i]) &&
           lines[i].trim() !== '<!--html-->') {
      buf.push(lines[i++]);
    }
    if (buf.length) out.push('<p>' + inline(buf.join('\n').trim(), ctx) + '</p>');
  }

  return out.join('\n');
}

export function renderMarkdown(src, opts = {}) {
  const ctx = { toc: [], footnotes: {}, footnoteOrder: [], host: opts.host };
  // Definitions may appear after their references, so render the body first,
  // then append notes in reference order.
  let html = parseBlocks(String(src).replace(/\r\n?/g, '\n').split('\n'), ctx);

  if (ctx.footnoteOrder.length) {
    const items = ctx.footnoteOrder.map((id) => {
      const s = slugify(id);
      const body = parseBlocks((ctx.footnotes[id] || '').split('\n'), ctx)
        .replace(/<\/p>$/, ' <a class="fn-back" href="#fnref-' + s + '" aria-label="Back to text">&#8617;</a></p>');
      return '<li id="fn-' + s + '">' + body + '</li>';
    }).join('\n');
    html += '\n<section class="footnotes"><h2 id="notes">Notes</h2><ol>\n' + items + '\n</ol></section>';
  }

  return { html, toc: ctx.toc };
}
