// Ranked bar list (emphasis form): the selected row in the accent hue, the rest in grey.
// Every value is also printed on its row, so the tooltip only adds detail - it never gates.
import { h } from './dom.js';

export const STATUS = {
  ok: { icon: '✓', text: 'On time', tone: 'good' },
  partial: { icon: '◐', text: 'Not enough stock', tone: 'warning' },
  late: { icon: '⏱', text: 'Arrives late', tone: 'serious' },
  cutoff_passed: { icon: '⏱', text: 'Cut-off passed', tone: 'serious' },
  unavailable: { icon: '✕', text: 'Not orderable', tone: 'critical' },
  unknown: { icon: '?', text: 'Unknown', tone: 'neutral' },
};

export function statusChip(kind, label) {
  const s = STATUS[kind] || STATUS.unknown;
  return toneChip(s.tone, s.icon, label || s.text);
}

export function toneChip(tone, icon, text, title) {
  return h('span', { class: `tone-chip tone-${tone}`, title },
    h('span', { class: 'status-icon', 'aria-hidden': 'true', text: icon }), text);
}

// ------------------------------------------------------------------ tooltip
function tipEl() { return document.getElementById('tooltip'); }

export function showTip(rows, x, y) {
  const t = tipEl();
  t.replaceChildren(...rows.filter(Boolean).map(([label, value]) =>
    h('div', { class: 'tip-row' }, h('strong', { text: value }), h('span', { text: label }))));
  t.hidden = false;
  const pad = 14;
  const r = t.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - pad;
  if (top + r.height > window.innerHeight - 8) top = y - r.height - pad;
  t.style.left = `${Math.max(8, left)}px`;
  t.style.top = `${Math.max(8, top)}px`;
}

export function hideTip() { tipEl().hidden = true; }

export function attachTip(el, rows) {
  el.addEventListener('pointermove', (e) => showTip(rows, e.clientX, e.clientY));
  el.addEventListener('pointerleave', hideTip);
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); showTip(rows, r.right - 40, r.top + 8); });
  el.addEventListener('blur', hideTip);
}

// ------------------------------------------------------------------ ranked bar list
/**
 * rows: [{ id, label, sublabel, value, valueText, noValueText, status (Node), tags: [{kind, text}],
 *          figures: [string], tooltip: [[label, value]], selected }]
 */
export function barList(rows, { ariaLabel, onSelect } = {}) {
  const max = Math.max(0, ...rows.map((r) => r.value ?? 0)) || 1;
  const anySelected = rows.some((r) => r.selected);
  const list = h('div', {
    class: `bar-list${onSelect ? ' is-selectable' : ''}`,
    role: onSelect ? 'listbox' : 'list',
    'aria-label': ariaLabel,
  });
  rows.forEach((r, i) => {
    const accent = !anySelected || r.selected;
    const bar = r.value != null
      ? h('div', { class: `bar-fill${accent ? ' is-accent' : ''}`, style: { width: `${Math.max(1, (r.value / max) * 100)}%` } })
      : h('div', { class: 'bar-none', text: r.noValueText || 'No value' });
    const row = h('div', {
      class: `bar-row${r.selected ? ' is-selected' : ''}`,
      role: onSelect ? 'option' : 'listitem',
      'aria-selected': onSelect ? String(Boolean(r.selected)) : null,
      tabindex: onSelect ? (r.selected || (!anySelected && i === 0) ? '0' : '-1') : (r.tooltip ? '0' : null),
      'data-id': r.id,
    },
      h('div', { class: 'bar-head' },
        h('span', { class: 'bar-name', text: r.label }),
        (r.tags || []).map((t) => h('span', { class: `tag tag-${t.kind}`, text: t.text })),
        r.status || null),
      r.sublabel ? h('div', { class: 'bar-sub', text: r.sublabel }) : null,
      h('div', { class: 'bar-line' }, h('div', { class: 'bar-area' }, bar), h('span', { class: 'bar-value', text: r.valueText ?? '' })),
      r.figures?.length ? h('div', { class: 'bar-figures' }, r.figures.map((f) => h('span', { text: f }))) : null);
    if (r.tooltip) attachTip(row, r.tooltip);
    if (onSelect) {
      row.addEventListener('click', () => { hideTip(); onSelect(r.id); });
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hideTip(); onSelect(r.id); }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          (e.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling)?.focus();
        }
      });
    }
    list.append(row);
  });
  return list;
}
