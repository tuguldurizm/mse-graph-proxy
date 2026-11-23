// Bilingual UI (issue #10): Mongolian (default) or English, chosen with ?lang=en|mn or the
// "English / Монгол" button, and remembered in this browser. Switching reloads the page in the
// other language (keeping the selected node and the filters), so the text is translated once,
// as the page builds it:
//   tr('…')   a string;   L`…${x}…`   the literal parts of a template, never the values put in.
// Both work run by run: a stretch of text with Cyrillic in it and none of < > " = ` or a line
// break, trimmed, looked up in EN (i18n_en.js); a run with no entry stays Mongolian. The static
// HTML is translated the same way at start-up (translateStatic).
// Names are data, not text: in English a company shows its English name (name_en) where the
// sources give one, anything else a transliteration of the Mongolian name (MNS 5217:2012, as
// in passports), with the original in the node's tooltip and the details panel.
const LANG = (() => {
  const q = (new URLSearchParams(location.search).get('lang') || '').toLowerCase();
  let saved = null;
  try { if (q === 'en' || q === 'mn') localStorage.setItem('lang', q); saved = localStorage.getItem('lang'); } catch {}
  return (q || saved) === 'en' ? 'en' : 'mn';
})();
document.documentElement.lang = LANG;
const EN_ON = LANG === 'en';
const RUN = /[^<>"=`\n]*[А-Яа-яЁёӨөҮү][^<>"=`\n]*/g;

function tr(s) {
  if (!EN_ON || s == null) return s;
  return String(s).replace(RUN, m => {
    const k = m.trim();
    let v = EN[k];
    if (v == null) {      // a count before the words, as in the server's texts ("3 эзэмшигч")
      const p = /^([\d\s.,:/%+−-]+)(\S.*)$/.exec(k);
      if (p && EN[p[2]] != null) v = p[1] + EN[p[2]];
    }
    if (v == null) return m;
    const i = m.indexOf(k);
    return m.slice(0, i) + v + m.slice(i + k.length);
  });
}
// Mongolian on purpose (matched against the data, or the name of the other language): not translated
const mn = s => s;
function L(strings, ...values) {
  let out = tr(strings[0]);
  values.forEach((v, i) => { out += `${v}` + tr(strings[i + 1]); });
  return out;
}

// Mongolian Cyrillic to Latin, MNS 5217:2012 (ө, ү as o, u, as in passports)
const TRANSLIT = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'ye', ё: 'yo', ж: 'j', з: 'z', и: 'i', й: 'i',
  к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', ө: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ү: 'u', ф: 'f',
  х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sh', ъ: 'i', ы: 'y', ь: 'i', э: 'e', ю: 'yu', я: 'ya' };
function translit(s) {
  const chars = [...String(s ?? '')];
  return chars.map((c, i) => {
    const low = c.toLowerCase(), t = TRANSLIT[low];
    if (t == null) return c;
    if (c === low) return t;
    // an upper-case letter: "Х" is "Kh" in "Хаан", "KH" in "ХХК"
    const next = chars[i + 1] || '', prev = chars[i - 1] || '';
    const caps = (next && next !== next.toLowerCase()) || (!/\p{L}/u.test(next) && prev && prev !== prev.toLowerCase());
    return caps ? t.toUpperCase() : t[0].toUpperCase() + t.slice(1);
  }).join('');
}
// legal forms in a name, in English (the rest of the name is transliterated)
const LEGAL_EN = { ХХК: 'LLC', ХК: 'JSC', ТӨХК: 'State-owned JSC', ТӨҮГ: 'State-owned enterprise', ББСБ: 'NBFI',
                   ХЗХ: 'Savings and credit cooperative', ТББ: 'NGO' };
const nameEn = s => String(s ?? '').split(/(\s+)/).map(w => LEGAL_EN[w] || translit(w)).join('');
// a name to show: an entity {name, name_en} or a name given as text
const dn = x => !x ? '' : !EN_ON ? (x.name ?? '') : (x.name_en || nameEn(x.name));
const dnS = s => EN_ON && s ? nameEn(s) : (s ?? '');

// a position's title: in English, its standard role (normalize.ROLES_STD, issue #34)
const ROLE_EN = { chair: 'Chair of the board', vice_chair: 'Vice chair of the board', board_member: 'Board member',
  independent_director: 'Independent director', ceo: 'CEO', deputy_ceo: 'Deputy CEO', cfo: 'CFO',
  secretary: 'Company secretary', supervisory: 'Supervisory board', other_executive: 'Executive', unknown: 'Position' };
// a title given as text (no role_std): the common ones are in EN, the rest transliterated
const titleS = t => EN_ON && t ? (EN[t] || translit(t)) : (t ?? '');
const roleTitle = x => !x ? '' : EN_ON ? (ROLE_EN[x.role_std] || titleS(x.title)) : (x.title ?? '');

// sectors: mse.mn's text is in Mongolian; in English the ISIC section of the registry's main activity
// (normalize.SECTIONS_STD), or a translation of the text when EN has one
const ISIC_EN = { A: 'Agriculture, forestry and fishing', B: 'Mining and quarrying', C: 'Manufacturing',
  D: 'Electricity, gas, steam and air conditioning', E: 'Water supply, sewerage and waste', F: 'Construction',
  G: 'Wholesale and retail trade', H: 'Transportation and storage', I: 'Accommodation and food service',
  J: 'Information and communication', K: 'Financial and insurance activities', L: 'Real estate',
  M: 'Professional, scientific and technical activities', N: 'Administrative and support services',
  O: 'Public administration and defence', P: 'Education', Q: 'Human health and social work',
  R: 'Arts, entertainment and recreation', S: 'Other service activities', T: 'Activities of households as employers',
  U: 'Extraterritorial organisations' };
const secName = (text, std) => !EN_ON || !text ? (text ?? '') : (EN[text] || (std && ISIC_EN[std]) || text);

// the data-quality issues (quality.ISSUES), by code
const ISSUE_EN = { percent_over_100: 'holdings add up to more than 100%', duplicate_holder: 'one holder twice, with different percentages',
  duplicate_holder_same: 'one holder twice', no_shareholders: 'no shareholders', no_percent: 'holders without percentages (registry only)',
  no_officers: 'no board or management', no_ceo: 'no CEO', no_register_no: 'no 7-digit register number: cannot be enriched from Open Data Lab',
  stale_as_of: 'holdings data is out of date', no_sector: 'no sector: left out of the same-sector checks',
  registry_not_read: 'has a register number, but its registry page was not read', missing_as_of: 'holdings without a date (as_of)',
  no_sector_std: 'no standard sector (only the MSE text)', unknown_roles: 'position titles not recognised' };
const issueText = i => !EN_ON || !ISSUE_EN[i.code] ? i.text : ISSUE_EN[i.code] + (i.detail ? `: ${tr(i.detail)}` : '');

// translate the static HTML once, at start-up: text nodes and title / placeholder attributes
function translateStatic(root) {
  if (!EN_ON) return;
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode())
    if (n.parentElement && !/^(SCRIPT|STYLE)$/.test(n.parentElement.tagName)) n.nodeValue = tr(n.nodeValue);
  root.querySelectorAll('[title], [placeholder]').forEach(el => {
    for (const a of ['title', 'placeholder']) if (el.hasAttribute(a)) el.setAttribute(a, tr(el.getAttribute(a)));
  });
}
