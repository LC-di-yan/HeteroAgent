/* ────────────────────────────────────────────────────────────────
   Markdown → DOM (dependency-free, XSS-safe)
   Every node is created with DOM APIs and textContent: model output is
   never concatenated into innerHTML. Only http(s)/mailto links are kept.
   ──────────────────────────────────────────────────────────────── */

const INLINE_RE = new RegExp(
  '(\u0060[^\u0060]+\u0060)' +            // inline code
  '|(\\*\\*[^*]+\\*\\*)' +               // **bold**
  '|(__[^_]+__)' +                       // __bold__
  '|(~~[^~]+~~)' +                       // ~~strike~~
  '|(\\*[^*\\n]+\\*)' +                  // *italic*
  '|(\\[[^\\]]*\\]\\((?:https?:\\/\\/|mailto:)[^)\\s]+\\))' + // [label](url)
  '|(https?:\\/\\/[^\\s<>()\\[\\]]+)',   // bare url
  'g',
);

function safeUrl(raw) {
  try {
    const url = new URL(raw, window.location.origin);
    if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:') {
      return url.href;
    }
  } catch (err) {
    /* invalid url */
  }
  return null;
}

function appendInline(parent, text, depth) {
  const source = String(text == null ? '' : text);
  if (depth > 3) {
    parent.appendChild(document.createTextNode(source));
    return;
  }
  let cursor = 0;
  // A fresh regex per pass: nested inline parsing must not share lastIndex.
  const re = new RegExp(INLINE_RE.source, 'g');
  let match = re.exec(source);
  while (match) {
    if (match.index > cursor) {
      parent.appendChild(document.createTextNode(source.slice(cursor, match.index)));
    }
    const token = match[0];
    if (token.charAt(0) === '\u0060') {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else if (token.startsWith('**') || token.startsWith('__')) {
      const strong = document.createElement('strong');
      appendInline(strong, token.slice(2, -2), depth + 1);
      parent.appendChild(strong);
    } else if (token.startsWith('~~')) {
      const del = document.createElement('s');
      appendInline(del, token.slice(2, -2), depth + 1);
      parent.appendChild(del);
    } else if (token.charAt(0) === '*') {
      const em = document.createElement('em');
      appendInline(em, token.slice(1, -1), depth + 1);
      parent.appendChild(em);
    } else if (token.charAt(0) === '[') {
      const close = token.indexOf('](');
      const label = token.slice(1, close);
      const href = safeUrl(token.slice(close + 2, -1));
      if (href) {
        const a = document.createElement('a');
        a.href = href;
        a.textContent = label || href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        parent.appendChild(a);
      } else {
        parent.appendChild(document.createTextNode(token));
      }
    } else {
      const href = safeUrl(token);
      if (href) {
        const a = document.createElement('a');
        a.href = href;
        a.textContent = token;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        parent.appendChild(a);
      } else {
        parent.appendChild(document.createTextNode(token));
      }
    }
    cursor = match.index + token.length;
    match = re.exec(source);
  }
  if (cursor < source.length) {
    parent.appendChild(document.createTextNode(source.slice(cursor)));
  }
}

function inlineInto(parent, text) {
  const lines = String(text == null ? '' : text).split('\n');
  lines.forEach(function (line, index) {
    if (index > 0) parent.appendChild(document.createElement('br'));
    appendInline(parent, line, 0);
  });
}

function codeBlock(lang, body) {
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  if (lang) code.dataset.lang = lang;
  code.textContent = body.replace(/\n$/, '');
  pre.appendChild(code);
  if (lang) {
    const badge = document.createElement('span');
    badge.className = 'code-lang';
    badge.textContent = lang;
    pre.appendChild(badge);
  }
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'code-copy';
  copy.textContent = '复制';
  copy.setAttribute('aria-label', '复制代码');
  copy.addEventListener('click', function () {
    copyText(code.textContent).then(function (ok) {
      copy.textContent = ok ? '已复制' : '复制失败';
      window.setTimeout(function () { copy.textContent = '复制'; }, 1600);
    });
  });
  pre.appendChild(copy);
  return pre;
}

export function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(function () { return true; })
      .catch(function () { return legacyCopy(text); });
  }
  return Promise.resolve(legacyCopy(text));
}

function legacyCopy(text) {
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (err) {
    return false;
  }
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.indexOf('|') !== -1;
}

function splitRow(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|')) row = row.slice(0, -1);
  return row.split('|').map(function (cell) { return cell.trim(); });
}

function buildTable(header, rows) {
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  header.forEach(function (cell) {
    const th = document.createElement('th');
    inlineInto(th, cell);
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  rows.forEach(function (cells) {
    const tr = document.createElement('tr');
    cells.forEach(function (cell) {
      const td = document.createElement('td');
      inlineInto(td, cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function isBlockStart(line, next) {
  if (/^\s*\u0060\u0060\u0060/.test(line)) return true;
  if (/^\s{0,3}#{1,6}\s+/.test(line)) return true;
  if (/^\s{0,3}>\s?/.test(line)) return true;
  if (/^\s*([-*+])\s+/.test(line)) return true;
  if (/^\s*\d+[.)]\s+/.test(line)) return true;
  if (/^\s*([-*_])\s*\1\s*\1[\s-]*$/.test(line)) return true;
  if (line.indexOf('|') !== -1 && next && isTableSeparator(next)) return true;
  return false;
}

export function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    const fence = line.match(/^\s*\u0060\u0060\u0060+\s*([\w+-]*)/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*\u0060\u0060\u0060+\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      frag.appendChild(codeBlock(fence[1] || '', body.join('\n')));
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length <= 2 ? 'h3' : 'h4';
      const el = document.createElement(level);
      inlineInto(el, heading[2]);
      frag.appendChild(el);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s-]*$/.test(line)) {
      frag.appendChild(document.createElement('hr'));
      i += 1;
      continue;
    }

    if (/^\s{0,3}>\s?/.test(line)) {
      const quote = document.createElement('blockquote');
      const body = [];
      while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
        i += 1;
      }
      const p = document.createElement('p');
      inlineInto(p, body.join('\n'));
      quote.appendChild(p);
      frag.appendChild(quote);
      continue;
    }

    if (/^\s*([-*+])\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      const itemRe = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length) {
        const item = lines[i].match(itemRe);
        if (!item) break;
        const body = [item[1]];
        i += 1;
        while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1]) &&
               !/^\s+\S/.test(lines[i])) {
          body.push(lines[i].trim());
          i += 1;
        }
        const li = document.createElement('li');
        inlineInto(li, body.join('\n'));
        list.appendChild(li);
      }
      frag.appendChild(list);
      continue;
    }

    if (line.indexOf('|') !== -1 && lines[i + 1] && isTableSeparator(lines[i + 1])) {
      const header = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].indexOf('|') !== -1 && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      frag.appendChild(buildTable(header, rows));
      continue;
    }

    const para = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) {
      para.push(lines[i]);
      i += 1;
    }
    const p = document.createElement('p');
    inlineInto(p, para.join('\n'));
    frag.appendChild(p);
  }
  return frag;
}

export const __test__ = { appendInline, isTableSeparator, splitRow };
