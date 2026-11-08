/* ────────────────────────────────────────────────────────────────
   Icon set · single stroke weight (1.75) on a 24px grid
   No emoji anywhere in the interface; every glyph is inline SVG.
   ──────────────────────────────────────────────────────────────── */
const NS = 'http://www.w3.org/2000/svg';

const PATHS = {
  brand: [
    'M12 3.2l7.6 4.4v8.8L12 20.8 4.4 16.4V7.6z',
    'M12 8.3l3.7 2.1v4.2L12 16.7l-3.7-2.1v-4.2z',
  ],
  sparkle: [
    'M12 3.4l1.9 5.7 5.7 1.9-5.7 1.9L12 18.6l-1.9-5.7L4.4 11l5.7-1.9z',
    'M18.6 15.4l.6 1.8 1.8.6-1.8.6-.6 1.8-.6-1.8-1.8-.6 1.8-.6z',
  ],
  route: [
    'M8 6a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0',
    'M21 18a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0',
    'M5.5 8.5v4a3.5 3.5 0 0 0 3.5 3.5h4.5',
  ],
  book: [
    'M5 4.9A1.9 1.9 0 0 1 6.9 3H19v15.2H6.9A1.9 1.9 0 0 0 5 20.1z',
    'M5 4.9v15.2',
    'M8.6 7.4h6.8',
  ],
  ticket: [
    'M6 3.8h12v16.4l-3-1.9-3 1.9-3-1.9-3 1.9z',
    'M9.2 8.2h5.6',
    'M9.2 12h5.6',
  ],
  shield: ['M12 3.3l7.2 2.9v5.1c0 4.3-2.9 7.5-7.2 9.5-4.3-2-7.2-5.2-7.2-9.5V6.2z'],
  shieldCheck: [
    'M12 3.3l7.2 2.9v5.1c0 4.3-2.9 7.5-7.2 9.5-4.3-2-7.2-5.2-7.2-9.5V6.2z',
    'M9 12.2l2.1 2.1 3.9-4',
  ],
  shieldX: [
    'M12 3.3l7.2 2.9v5.1c0 4.3-2.9 7.5-7.2 9.5-4.3-2-7.2-5.2-7.2-9.5V6.2z',
    'M9.6 10.2l4.8 4.8',
    'M14.4 10.2l-4.8 4.8',
  ],
  pen: [
    'M4.5 19.5h4L19 9a2.1 2.1 0 0 0-3-3L5.5 16.5z',
    'M14.6 6.4l3 3',
  ],
  plus: ['M12 5v14', 'M5 12h14'],
  minus: ['M5 12h14'],
  menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],
  close: ['M6.4 6.4l11.2 11.2', 'M17.6 6.4L6.4 17.6'],
  check: ['M4.8 12.6l4.6 4.6L19.2 7'],
  chevronDown: ['M7 10l5 5 5-5'],
  chevronRight: ['M9.5 6.5l5.5 5.5-5.5 5.5'],
  arrowUp: ['M12 19.5V5', 'M5.8 11.2L12 5l6.2 6.2'],
  copy: [
    'M8.6 8.6V5.9A1.9 1.9 0 0 1 10.5 4h7.6A1.9 1.9 0 0 1 20 5.9v7.6a1.9 1.9 0 0 1-1.9 1.9h-2.7',
    'M5.9 8.6h7.6A1.9 1.9 0 0 1 15.4 10.5v7.6A1.9 1.9 0 0 1 13.5 20H5.9A1.9 1.9 0 0 1 4 18.1v-7.6A1.9 1.9 0 0 1 5.9 8.6z',
  ],
  refresh: ['M20.3 9.4A8.4 8.4 0 1 0 21 12', 'M20.6 4.2v5.4h-5.4'],
  download: ['M12 4v11.4', 'M7.6 11l4.4 4.4L16.4 11', 'M5 19.6h14'],
  message: [
    'M4.5 7.2A2.2 2.2 0 0 1 6.7 5h10.6a2.2 2.2 0 0 1 2.2 2.2v7a2.2 2.2 0 0 1-2.2 2.2H9.7l-5.2 4z',
  ],
  clock: ['M20.5 12a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0', 'M12 7.6V12l3 1.9'],
  gauge: ['M4.6 19.4v-5.8', 'M9.6 19.4V9.2', 'M14.6 19.4v-8.3', 'M19.4 19.4V5.2'],
  sliders: [
    'M4 8h6', 'M14.4 8H20', 'M4 16h1.6', 'M10.4 16H20',
    'M13.4 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
    'M9.4 16a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
  ],
  key: [
    'M12 9a3.4 3.4 0 1 1-6.8 0 3.4 3.4 0 0 1 6.8 0',
    'M11.2 11.2L20 20', 'M17 17l1.6-1.6', 'M14.6 14.6l1.6 1.6',
  ],
  user: [
    'M15.6 8.6a3.6 3.6 0 1 1-7.2 0 3.6 3.6 0 0 1 7.2 0',
    'M5.2 19.6c.5-3.4 3.3-5.3 6.8-5.3s6.3 1.9 6.8 5.3',
  ],
  cpu: [
    'M6.6 6.6h10.8v10.8H6.6z', 'M9.4 3.4v3.2', 'M14.6 3.4v3.2',
    'M9.4 17.4v3.2', 'M14.6 17.4v3.2', 'M3.4 9.4h3.2', 'M3.4 14.6h3.2',
    'M17.4 9.4h3.2', 'M17.4 14.6h3.2',
  ],
  layers: [
    'M12 3.6l8.4 4.4-8.4 4.4L3.6 8z',
    'M3.6 12.6l8.4 4.4 8.4-4.4',
    'M3.6 16.6l8.4 4.4 8.4-4.4',
  ],
  zap: ['M13.2 2.8L5.4 13.4h5.2l-.8 7.8 7.8-10.6h-5.2z'],
  alert: ['M12 4.4l8.6 15H3.4z', 'M12 10.2v4', 'M12 16.8v.1'],
  info: ['M20.5 12a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0', 'M12 11.2v5', 'M12 8v.1'],
  link: [
    'M10.4 13.6l3.2-3.2',
    'M13.2 7.6l1-1a3.4 3.4 0 0 1 4.8 4.8l-1 1',
    'M10.8 16.4l-1 1a3.4 3.4 0 0 1-4.8-4.8l1-1',
  ],
  trash: [
    'M4.5 7h15', 'M9.5 7V5.2h5V7',
    'M6.4 7l.9 12.2h9.4L17.6 7', 'M10.2 11v4.6', 'M13.8 11v4.6',
  ],
  external: [
    'M14 4.5h5.5V10', 'M19.5 4.5L11 13',
    'M18 14.5v4.2a1.3 1.3 0 0 1-1.3 1.3H5.8a1.3 1.3 0 0 1-1.3-1.3V7.8a1.3 1.3 0 0 1 1.3-1.3h4.2',
  ],
  file: ['M6.5 4.3h7L18 8.8v10.9H6.5z', 'M13.5 4.3v4.5H18'],
  search: ['M17.4 11a6.4 6.4 0 1 1-12.8 0 6.4 6.4 0 0 1 12.8 0', 'M15.8 15.8L20 20'],
  clock2: ['M20.5 12a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0', 'M12 8v4.2l2.8 1.7'],
  sun: [
    'M15.9 12a3.9 3.9 0 1 1-7.8 0 3.9 3.9 0 0 1 7.8 0',
    'M12 1.9v2.7', 'M12 19.4v2.7', 'M1.9 12h2.7', 'M19.4 12h2.7',
    'M4.6 4.6l1.9 1.9', 'M17.5 17.5l1.9 1.9', 'M19.4 4.6l-1.9 1.9', 'M6.5 17.5l-1.9 1.9',
  ],
  moon: ['M20.8 13.4A8.6 8.6 0 1 1 10.6 3.2a6.9 6.9 0 0 0 10.2 10.2z'],
};

/* Icons drawn with fill instead of stroke. */
const FILLED = new Set(['stop']);
PATHS.stop = ['M8.2 8.2h7.6v7.6H8.2z'];

const DEFAULT = ['M20.5 12a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0'];

export function icon(name, options = {}) {
  const opts = options || {};
  const size = opts.size || 18;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('fill', FILLED.has(name) ? 'currentColor' : 'none');
  if (!FILLED.has(name)) {
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(opts.stroke || (size <= 16 ? 1.95 : 1.75)));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
  }
  if (opts.cls) svg.setAttribute('class', opts.cls);
  svg.style.flex = 'none';
  const paths = PATHS[name] || DEFAULT;
  for (const d of paths) {
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', d);
    svg.appendChild(p);
  }
  return svg;
}

/* Horizontal brand lockup mark used in the rail header. */
export function brandMark(size = 34) {
  return icon('brand', { size: Math.round(size * 0.62), stroke: 1.6 });
}

export const STAGE_META = {
  supervisor_route: { label: '意图路由', icon: 'route' },
  knowledge_rag: { label: '知识检索', icon: 'book' },
  ticket_handler: { label: '工单处理', icon: 'ticket' },
  compliance_check: { label: '合规审查', icon: 'shield' },
  synthesize: { label: '生成回复', icon: 'pen' },
};

export function stageMeta(node) {
  return STAGE_META[node] || { label: node || '处理中', icon: 'cpu' };
}
