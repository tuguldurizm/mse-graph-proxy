// Export and share (issue #10): the address bar always holds what the graph shows and how, so a
// copied link opens the same view; the graph as drawn (after the filters) downloads as PNG, as a
// CSV of its links, or as GEXF for Gephi. Pure helpers here (no page state), so the tests can run
// them in Node; index.html wires them to the page.
//
// The query: one view, then the choices that differ from their defaults.
//   view    ?focus=<key>                  a node and its neighbourhood (the change feed links here)
//           ?a=<key>&b=<key>              the shortest path between two nodes
//           ?group=<owner key>            an owner's control group (#28)
//           ?community=<group id>         a detected group (#28)
//           (none)                        the market overview
//   choices depth (1-3, default 2), color (sector | group), size (betweenness | pagerank | degree |
//           market_cap), sector (<sector text>), min (stake %: 1 5 10 20 50), ties (board |
//           positions | owners), lang (en | mn, see i18n.js)
const VIEW_KEYS = ['focus', 'a', 'b', 'group', 'community'];
// query name -> [the page's select id, its default]
const CHOICES = { depth: ['depth', '2'], color: ['colorBy', ''], size: ['sizeBy', ''], sector: ['sector', ''],
                  min: ['minPct', '0'], ties: ['ties', ''] };

// the query for a view and the choices ({select id: value}), keeping other parameters (lang)
function viewQuery(search, shown, choices) {
  const q = new URLSearchParams(search);
  for (const k of [...VIEW_KEYS, ...Object.keys(CHOICES)]) q.delete(k);
  if (shown.kind === 'focus') q.set('focus', shown.key);
  else if (shown.kind === 'path') { q.set('a', shown.a); q.set('b', shown.b); }
  else if (shown.kind === 'group') q.set('group', shown.key);
  else if (shown.kind === 'community') q.set('community', shown.id);
  for (const [name, [id, dflt]] of Object.entries(CHOICES)) {
    const v = choices[id];
    if (v != null && v !== '' && v !== dflt) q.set(name, v);
  }
  return q.toString();
}

// the view and the choices a query asks for
function parseView(search) {
  const q = new URLSearchParams(search), get = k => q.get(k) || '';
  const shown = get('focus') ? { kind: 'focus', key: get('focus') }
    : get('a') && get('b') ? { kind: 'path', a: get('a'), b: get('b') }
    : get('group') ? { kind: 'group', key: get('group') }
    : get('community') ? { kind: 'community', id: get('community') }
    : { kind: 'overview' };
  const choices = {};
  for (const [name, [id]] of Object.entries(CHOICES)) if (q.has(name)) choices[id] = q.get(name);
  return { shown, choices };
}

// ---- CSV: one row per link shown -------------------------------------------------------------
const CSV_COLUMNS = ['link', 'source_key', 'source_name', 'source_name_en', 'source_type', 'source_ticker',
  'target_key', 'target_name', 'target_name_en', 'target_type', 'target_ticker', 'percent', 'holding_type',
  'title', 'role_std'];
function csvCell(v) {
  if (v == null) return '';
  if (typeof v === 'number') return String(v);
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;          // never a formula in a spreadsheet
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function csvLinks(nodes, edges) {
  const by = new Map(nodes.map(n => [n.key, n]));
  const end = (k, side) => {
    const n = by.get(k) || { key: k };
    return { [`${side}_key`]: k, [`${side}_name`]: n.name, [`${side}_name_en`]: n.name_en,
             [`${side}_type`]: n.type, [`${side}_ticker`]: n.ticker };
  };
  const rows = edges.map(e => ({ link: e.type, ...end(e.source, 'source'), ...end(e.target, 'target'),
    percent: e.percent, holding_type: e.holding_type, title: e.title, role_std: e.role_std }));
  // a byte-order mark, so spreadsheets read the Cyrillic as UTF-8
  return '﻿' + [CSV_COLUMNS.join(','), ...rows.map(r => CSV_COLUMNS.map(c => csvCell(r[c])).join(','))]
    .join('\r\n') + '\r\n';
}

// ---- GEXF 1.3 (Gephi): nodes with their attributes, directed links ---------------------------
const xmlEsc = s => String(s ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '')
  .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));
const NODE_ATTRS = [['type', 'string'], ['ticker', 'string'], ['name_en', 'string'], ['listed', 'boolean'],
  ['sector', 'string'], ['sector_std', 'string'], ['group_id', 'string'], ['group_label', 'string'],
  ['centrality_degree', 'integer'], ['centrality_betweenness', 'double'], ['centrality_pagerank', 'double'],
  ['market_cap', 'double']];
const EDGE_ATTRS = [['link', 'string'], ['percent', 'double'], ['holding_type', 'string'], ['title', 'string'],
  ['role_std', 'string']];
function attvalues(obj, attrs) {
  const vs = attrs.filter(([k]) => obj[k] != null && obj[k] !== '')
    .map(([k]) => `<attvalue for="${k}" value="${xmlEsc(obj[k])}"/>`);
  return vs.length ? `<attvalues>${vs.join('')}</attvalues>` : '';
}
function gexf(nodes, edges, { description = '', date = '' } = {}) {
  const keys = new Set(nodes.map(n => n.key));
  const decl = (cls, attrs) => `    <attributes class="${cls}">\n` +
    attrs.map(([k, t]) => `      <attribute id="${k}" title="${k}" type="${t}"/>\n`).join('') + '    </attributes>\n';
  const ns = nodes.map(n => `      <node id="${xmlEsc(n.key)}" label="${xmlEsc(n.name || n.key)}">${attvalues(n, NODE_ATTRS)}</node>\n`);
  const es = edges.filter(e => keys.has(e.source) && keys.has(e.target)).map((e, i) =>
    `      <edge id="${i}" source="${xmlEsc(e.source)}" target="${xmlEsc(e.target)}" label="${xmlEsc(e.type)}"` +
    ` weight="${e.type === 'OWNS' && e.percent != null ? +e.percent : 1}">` +
    `${attvalues({ link: e.type, ...e }, EDGE_ATTRS)}</edge>\n`);
  return '<?xml version="1.0" encoding="UTF-8"?>\n<gexf xmlns="http://gexf.net/1.3" version="1.3">\n' +
    `  <meta${date ? ` lastmodifieddate="${xmlEsc(date)}"` : ''}><creator>MSE connections graph</creator>` +
    `<description>${xmlEsc(description)}</description></meta>\n` +
    '  <graph defaultedgetype="directed" mode="static">\n' + decl('node', NODE_ATTRS) + decl('edge', EDGE_ATTRS) +
    `    <nodes>\n${ns.join('')}    </nodes>\n    <edges>\n${es.join('')}    </edges>\n  </graph>\n</gexf>\n`;
}

// a file name: mse-graph-<what>-<date>.<ext>, letters, digits and dashes only
const exportName = (what, date, ext) =>
  `mse-graph-${String(what || 'view').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'view'}-${date}.${ext}`;
