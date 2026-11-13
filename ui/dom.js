// Small DOM + formatting helpers. All text goes in through textContent (data is untrusted).

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (typeof v === 'boolean' && k in el) el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

const eurFmt = new Intl.NumberFormat('en-IE', { style: 'currency', currency: 'EUR' });
const eur0Fmt = new Intl.NumberFormat('en-IE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 });

export const fmt = {
  eur: (v) => (v == null ? '–' : eurFmt.format(v)),
  eur0: (v) => (v == null ? '–' : eur0Fmt.format(v)),
  num: (v, digits = 0) => (v == null ? '–' : new Intl.NumberFormat('en-GB', { maximumFractionDigits: digits }).format(v)),
  pct: (v) => {
    if (v == null) return 'n/a';
    const p = v * 100;
    return `${Math.abs(p - Math.round(p)) < 0.05 ? Math.round(p) : p.toFixed(1)}%`;
  },
  day: (iso) => {
    if (!iso) return '–';
    const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  },
  time: (iso) => (iso ? new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '–'),
  dateTime: (iso) =>
    iso
      ? new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
      : '–',
};

export function plural(word, n) {
  if (n === 1 || word === 'kg') return word;
  return /(s|x|ch|sh)$/.test(word) ? `${word}es` : `${word}s`;
}

export const storage = {
  get(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode: ignore */ }
  },
};

export function localISO(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function nextWeekday(from = new Date()) {
  const d = new Date(from);
  do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
  return localISO(d);
}
