import { $, fmt, h, localISO, nextWeekday, plural, storage } from './dom.js';
import { barList, hideTip, statusChip, toneChip } from './charts.js';

// ====================================================================== state & api
const S = {
  vocab: { claims: {}, allergens: {}, labels: {}, sales_units: {}, requirement_options: [] },
  customers: [],
  offer: null,
  decision: null,
  edit: null,
  selections: {},
  sort: {},
  expanded: {},
  rationale: null, // set once the person edits it
  submitted: null,
  dirty: false,
  requestChanged: false,
  editVersion: 0,
  requestVersion: 0,
  requestPending: false,
  evaluationVersion: 0,
  evaluationPending: false,
  evaluationError: null,
  submissionPending: false,
  submitError: null,
  reviewPending: new Set(),
  explore: { filters: new Set(), category: '' },
};

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data?.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `HTTP ${res.status}`);
  }
  return data;
}

// ====================================================================== vocabulary helpers
const UNIT_TEXT = { L: 'litres', KG: 'kg', PCS: 'pieces', SALES_UNIT: 'units' };
const UNIT_SHORT = { L: 'L', KG: 'kg', PCS: 'pcs', SALES_UNIT: 'units' };
const unitWord = (code) => S.vocab.sales_units[code] || code;
const unitLabel = (code, n) => `${plural(unitWord(code), n)}`;

function reqChips(c) {
  const out = [];
  for (const code of c.required_claims || []) out.push({ kind: 'claim', code, label: S.vocab.claims[code] || code });
  const al = c.free_from_allergens || [];
  for (const code of al) out.push({ kind: 'allergen', code, label: `free from ${S.vocab.allergens[code] || code}` });
  for (const code of c.required_labels || []) out.push({ kind: 'label', code, label: S.vocab.labels[code] || code });
  if (c.origin_country) out.push({ kind: 'origin', code: c.origin_country, label: c.origin_country === 'fi' ? 'Finnish' : `origin ${c.origin_country.toUpperCase()}` });
  return out;
}
const reqText = (c) => reqChips(c).map((r) => r.label).join(', ');

function itemTitle(req) {
  const qty = req.quantity != null && req.unit ? `${fmt.num(req.quantity, 2)} ${UNIT_SHORT[req.unit]} · ` : '';
  return `${qty}${req.product_query}`;
}

const OUTCOMES = {
  auto_approve: { tone: 'good', icon: '✓', title: 'Within authority', button: 'Place order' },
  needs_approval: { tone: 'warning', icon: '⚑', title: 'Needs human approval', button: 'Send for approval' },
  reject: { tone: 'critical', icon: '✕', title: 'Cannot be placed', button: 'Cannot be placed' },
};

function trustChip(status) {
  if (status === 'trusted') return toneChip('good', '✓', 'Trusted · on whitelist');
  if (status === 'unverified') return toneChip('warning', '!', 'New · needs verification');
  return toneChip('critical', '✕', 'Blocked');
}

function checkItem(c) {
  const kind = c.passed ? 'pass' : c.severity;
  const icon = { pass: '✓', review: '!', block: '✕', info: 'i' }[kind];
  const sr = { pass: 'Passed', review: 'Needs a person', block: 'Blocks the order', info: 'Note' }[kind];
  return h('li', { class: `check ${kind}` },
    h('span', { class: 'check-icon', 'aria-hidden': 'true', text: icon }),
    h('span', {}, h('span', { class: 'visually-hidden', text: `${sr}: ` }), c.message));
}

// ====================================================================== status & navigation
const PAGES = ['order', 'review', 'customers', 'audit', 'explore'];
const loaders = { review: loadReview, customers: loadCustomersPage, audit: () => loadAudit(), explore: () => S.exploreLoaded || runExplore() };

function route() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'order';
  for (const p of PAGES) $(`#page-${p}`).hidden = p !== page;
  document.querySelectorAll('.main-nav a').forEach((a) => {
    a.classList.toggle('is-active', a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  hideTip();
  loaders[page]?.();
}

async function loadHealth() {
  const el = $('#api-status');
  try {
    const hl = await api('/api/health');
    const parts = [];
    parts.push(hl.valio_api.ok ? 'Valio Aimo API connected' : 'Valio Aimo API not reachable');
    parts.push(hl.catalog.loaded ? `${fmt.num(hl.catalog.products)} products` : 'catalog missing');
    parts.push(`parser: ${hl.parser === 'claude' ? `Claude (${hl.model})` : 'rules'}`);
    el.textContent = parts.join(' · ');
    el.className = `ribbon-status ${hl.valio_api.ok && hl.catalog.loaded ? 'ok' : 'bad'}`;
    if (!hl.catalog.loaded) setStatus(hl.catalog.error || 'The product catalog is not loaded', true);
  } catch {
    el.textContent = 'Gateway not reachable';
    el.className = 'ribbon-status bad';
  }
}

async function refreshReviewCount() {
  try {
    const pending = await api('/api/orders?status=pending_approval');
    const badge = $('#nav-review-count');
    badge.textContent = String(pending.length);
    badge.hidden = pending.length === 0;
  } catch { /* ignore */ }
}

// ====================================================================== order page
const EXAMPLES = [
  { label: 'Hotel · 1,000 L lactose-free milk', customer: 'C-1001',
    text: 'I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability.' },
  { label: 'Hotel · cream & butter', customer: 'C-1001', text: '50 l lactose-free whipping cream and 20 kg Finnish butter for Monday' },
  { label: 'School · nut-free muesli', customer: 'C-1002', text: '15 kg muesli for Tuesday, everything must be nut-free' },
  { label: 'New customer · first order', customer: 'C-2001', text: '30 kg potatoes and 10 kg tomatoes for Tuesday' },
  { label: 'Suomeksi', customer: 'C-1001', text: 'Tarvitsen 200 litraa laktoositonta maitoa maanantaiksi' },
];

function setStatus(text, bad = false) {
  const el = $('#run-status');
  el.textContent = text || '';
  el.classList.toggle('bad', bad);
}

function orderStateMessage() {
  if (S.submissionPending) return 'Submitting this order…';
  if (S.requestPending) return 'Checking the request. The previous offer cannot be submitted.';
  if (S.dirty) return S.requestChanged
    ? 'The request or customer changed. Find a new option before ordering.'
    : 'Details changed. Re-run with these details before ordering.';
  if (S.evaluationPending) return 'Checking the selected option. Wait before ordering.';
  if (S.evaluationError) return 'The selected option could not be checked. Select an option again to retry.';
  if (S.offer && new Date(S.offer.valid_until) <= new Date()) return 'Offer expired. Re-run to check current prices and availability.';
  if (S.offer?.parsed.delivery_date_assumed) return 'Confirm the delivery date by re-running with these details.';
  if (S.offer?.parsed.clarification_required) return 'The request needs clarification. Correct the request and find a new option.';
  return '';
}

function updateOrderControls() {
  const run = $('#run-btn');
  run.disabled = S.requestPending || S.submissionPending;
  run.classList.toggle('is-busy', S.requestPending);
  $('#request-text').disabled = S.submissionPending;
  $('#customer').disabled = S.submissionPending;
  $('#reset-demo').disabled = S.requestPending || S.submissionPending;
  const rationale = $('#rationale');
  if (rationale) rationale.disabled = S.submissionPending;
  document.querySelectorAll('#examples button').forEach((b) => { b.disabled = S.requestPending || S.submissionPending; });
  document.querySelectorAll('#understood input, #understood select, #understood button').forEach((el) => { el.disabled = S.requestPending || S.submissionPending || Boolean(S.submitted); });
  const rerun = $('#rerun-details');
  if (rerun) rerun.disabled = S.requestPending || S.submissionPending || Boolean(S.submitted) || S.requestChanged || !S.edit?.items.length;
  const state = $('#offer-state');
  if (state) {
    state.textContent = orderStateMessage();
    state.hidden = !state.textContent || Boolean(S.submitted);
  }
}

function markDirty(requestChanged = false) {
  S.editVersion += 1;
  S.dirty = true;
  S.requestChanged ||= requestChanged;
  S.evaluationVersion += 1;
  S.evaluationPending = false;
  S.evaluationError = null;
  S.submitError = null;
  if (S.offer) rerenderDecision();
  updateOrderControls();
}

function currentCustomer() {
  return S.customers.find((c) => c.id === $('#customer').value) || S.customers[0];
}

function renderCustomerSelect(keep) {
  const sel = $('#customer');
  const value = keep || sel.value || storage.get('aimo.customer', 'C-1001');
  sel.replaceChildren(...S.customers.map((c) => h('option', { value: c.id, text: c.name })));
  sel.value = S.customers.some((c) => c.id === value) ? value : S.customers[0]?.id;
  renderTrustCard();
}

function renderTrustCard() {
  const c = currentCustomer();
  if (!c) return;
  $('#trust-card').replaceChildren(
    h('div', { class: 'trust-head' }, trustChip(c.status), h('strong', { class: 'small', text: c.segment }), h('span', { class: 'muted small', text: c.notes })),
    h('ul', { class: 'trust-reasons' }, c.trust.reasons.map((r) => h('li', { text: r }))));
}

function renderExamples() {
  $('#examples').replaceChildren(
    h('span', { class: 'muted small', text: 'Try:' }),
    ...EXAMPLES.map((ex) => h('button', {
      type: 'button', class: 'example', text: ex.label,
      onClick: () => {
        $('#request-text').value = ex.text;
        $('#customer').value = ex.customer;
        storage.set('aimo.customer', ex.customer);
        renderTrustCard();
        markDirty(true);
        runRequest();
      },
    })));
}

async function runRequest(override = null) {
  if (S.requestPending || S.submissionPending) return;
  const text = $('#request-text').value.trim();
  if (!text) { setStatus('Write what you need first.', true); $('#request-text').focus(); return; }
  const customerId = $('#customer').value;
  const version = ++S.requestVersion;
  const editVersion = S.editVersion;
  S.requestPending = true;
  S.dirty = true;
  S.evaluationVersion += 1;
  S.evaluationPending = false;
  S.evaluationError = null;
  S.submitError = null;
  updateOrderControls();
  if (S.offer) rerenderDecision();
  $('#results').classList.add('is-refreshing');
  setStatus(override ? 'Re-checking with your corrections…' : 'Understanding the request and checking prices and availability with Valio Aimo…');
  try {
    const res = await api('/api/requests', { method: 'POST', body: { customer_id: customerId, text, override } });
    if (version !== S.requestVersion) return;
    if (editVersion !== S.editVersion || text !== $('#request-text').value.trim() || customerId !== $('#customer').value) {
      setStatus('The request changed while it was being checked. Find a new option.', true);
      return;
    }
    Object.assign(S, { offer: res.offer, decision: res.decision, selections: {}, sort: {}, expanded: {}, rationale: null, submitted: null, dirty: false, requestChanged: false });
    S.edit = structuredClone(res.offer.parsed);
    const idx = S.customers.findIndex((c) => c.id === res.customer.id);
    if (idx >= 0) S.customers[idx] = res.customer;
    setStatus('');
    renderResults();
    if (!override) $('#results').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (version === S.requestVersion) setStatus(e.message, true);
  } finally {
    if (version === S.requestVersion) {
      S.requestPending = false;
      $('#results').classList.remove('is-refreshing');
      if (S.offer) rerenderDecision();
      updateOrderControls();
    }
  }
}

const selectedId = (i) => S.selections[i] ?? S.offer.items[i].recommended_product_id;
const selectedCand = (i) => S.offer.items[i].candidates.find((c) => c.product_id === selectedId(i));

function renderResults() {
  const host = $('#results');
  host.hidden = false;
  host.replaceChildren(h('div', { class: 'results-grid' },
    h('div', { class: 'results-main' }, understoodPanel(), operationsPanel(), S.offer.items.map((item, i) => itemPanel(item, i))),
    h('aside', { class: 'results-side', 'aria-label': 'Decision' }, decisionPanel())));
}

// ---------------------------------------------------------------------- understood (editable)
function understoodPanel() {
  const p = S.offer.parsed;
  const dateInput = h('input', { type: 'date', id: 'u-date', value: S.edit.delivery_date, min: localISO(new Date()) });
  dateInput.addEventListener('change', () => { S.edit.delivery_date = dateInput.value; S.edit.delivery_date_assumed = false; markDirty(); });
  const budgetInput = h('input', { type: 'number', id: 'u-budget', min: '0.01', step: '0.01', value: S.edit.budget_eur ?? '', placeholder: 'No cap' });
  budgetInput.addEventListener('input', () => { S.edit.budget_eur = budgetInput.value === '' ? null : Number(budgetInput.value); markDirty(); });
  const substitutes = h('input', { type: 'checkbox', id: 'u-substitutes', checked: S.edit.substitution_allowed !== false });
  substitutes.addEventListener('change', () => { S.edit.substitution_allowed = substitutes.checked; markDirty(); });
  return h('section', { class: 'panel', id: 'understood' },
    h('div', { class: 'panel-head' },
      h('h3', { text: 'What the agent understood' }),
      h('span', { class: 'muted small', text: `${p.parser === 'claude' ? 'Claude' : 'Rule-based'} parser · correct anything that's wrong` })),
    h('p', { id: 'offer-state', class: 'offer-state', role: 'status', hidden: !orderStateMessage(), text: orderStateMessage() }),
    h('div', { class: 'u-rows', id: 'u-rows' }, S.edit.items.map((it, i) => understoodRow(it, i))),
    S.edit.items.length ? null : h('p', { class: 'empty', text: 'No products recognised. Rephrase the request, e.g. “200 litres of lactose-free milk by Monday”.' }),
    h('div', { class: 'u-foot' },
      h('label', { class: 'field-inline', for: 'u-date' }, 'Delivery date', dateInput),
      p.delivery_date_assumed ? toneChip('warning', '!', 'Assumed: the request had no date') : null,
      h('button', { id: 'rerun-details', class: 'btn btn-small', type: 'button', onClick: rerunWithEdits, text: 'Re-run with these details', disabled: !S.edit.items.length })),
    h('div', { class: 'u-controls' },
      h('label', { class: 'field-inline', for: 'u-budget' }, 'Budget EUR, excl. VAT (optional)', budgetInput),
      h('label', { class: 'check-inline', for: 'u-substitutes' }, substitutes, 'Allow substitutes outside requested preferences')),
    p.clarification_required ? h('p', { class: 'offer-state', text: 'Some purchasing conditions could not be understood. Correct the request before ordering.' }) : null,
    p.warnings.length ? h('ul', { class: 'warnings' }, p.warnings.map((w) => h('li', { text: w }))) : null);
}

function understoodRow(it, i) {
  const product = h('input', { class: 'u-product', type: 'text', value: it.product_query, 'aria-label': `Product ${i + 1}` });
  product.addEventListener('input', () => { S.edit.items[i].product_query = product.value; markDirty(); });
  const qty = h('input', { class: 'u-qty', type: 'number', min: '0', step: 'any', value: it.quantity ?? '', placeholder: 'Qty', 'aria-label': `Quantity ${i + 1}` });
  qty.addEventListener('input', () => { S.edit.items[i].quantity = qty.value === '' ? null : Number(qty.value); markDirty(); });
  const unit = h('select', { class: 'u-unit', 'aria-label': `Unit ${i + 1}` },
    h('option', { value: '', text: 'Choose unit' }),
    Object.entries(UNIT_TEXT).map(([v, label]) => h('option', { value: v, text: label })));
  unit.value = it.unit || '';
  unit.addEventListener('change', () => { S.edit.items[i].unit = unit.value || null; markDirty(); });

  const add = h('select', { class: 'req-add', 'aria-label': `Add requirement to item ${i + 1}` },
    h('option', { value: '', text: '+ requirement' }),
    S.vocab.requirement_options.map((o, k) => h('option', { value: String(k), text: o.label })));
  add.addEventListener('change', () => {
    const o = S.vocab.requirement_options[Number(add.value)];
    if (o) { addRequirement(S.edit.items[i].constraints, o); refreshRow(i); markDirty(); }
  });
  return h('div', { class: 'u-row', 'data-index': i },
    product, qty, unit,
    h('div', { class: 'u-reqs' },
      reqChips(it.constraints).map((rc) => h('span', { class: 'req-chip' }, rc.label,
        h('button', { type: 'button', class: 'chip-x', 'aria-label': `Remove ${rc.label}`, text: '×',
          onClick: () => { removeRequirement(S.edit.items[i].constraints, rc); refreshRow(i); markDirty(); } }))),
      add),
    it.assumption ? h('p', { class: 'u-assumption', text: it.assumption }) : null);
}

function refreshRow(i) {
  const old = document.querySelector(`.u-row[data-index="${i}"]`);
  if (old) old.replaceWith(understoodRow(S.edit.items[i], i));
}

function addRequirement(c, o) {
  if (o.kind === 'claim' && !c.required_claims.includes(o.code)) c.required_claims.push(o.code);
  if (o.kind === 'allergen' && !c.free_from_allergens.includes(o.code)) c.free_from_allergens.push(o.code);
  if (o.kind === 'label' && !c.required_labels.includes(o.code)) c.required_labels.push(o.code);
  if (o.kind === 'origin') c.origin_country = o.code;
}

function removeRequirement(c, rc) {
  if (rc.kind === 'claim') c.required_claims = c.required_claims.filter((x) => x !== rc.code);
  if (rc.kind === 'allergen') c.free_from_allergens = c.free_from_allergens.filter((x) => x !== rc.code);
  if (rc.kind === 'label') c.required_labels = c.required_labels.filter((x) => x !== rc.code);
  if (rc.kind === 'origin') c.origin_country = null;
}

function rerunWithEdits() {
  if (S.requestPending || S.submissionPending || !S.offer || S.requestChanged) return;
  const incomplete = S.edit.items.findIndex((it) => !it.product_query.trim() || !Number.isFinite(it.quantity) || it.quantity <= 0 || !it.unit);
  if (incomplete >= 0) {
    setStatus('Each item needs a product, a positive quantity and an explicit unit.', true);
    $(`.u-row[data-index="${incomplete}"] input`)?.focus();
    return;
  }
  if (!S.edit.delivery_date || S.edit.delivery_date < localISO(new Date())) {
    setStatus('Choose a delivery date today or later.', true);
    $('#u-date').focus();
    return;
  }
  if (S.edit.budget_eur != null && (!Number.isFinite(S.edit.budget_eur) || S.edit.budget_eur <= 0)) {
    setStatus('Enter a positive budget in EUR, or leave the budget empty.', true);
    $('#u-budget').focus();
    return;
  }
  const original = S.offer.parsed.items;
  const override = {
    ...S.edit,
    delivery_date_assumed: false,
    items: S.edit.items.map((it, i) => {
      const changed = it.product_query.trim() !== (original[i]?.product_query || '');
      const hasQty = it.quantity != null && it.quantity > 0;
      return {
        ...it,
        product_query: it.product_query.trim(),
        search_phrases: changed ? [it.product_query.trim()] : it.search_phrases,
        exclude_words: changed ? [] : it.exclude_words,
        quantity: hasQty ? it.quantity : null,
        unit: hasQty ? it.unit : null,
        assumption: '',
      };
    }).filter((it) => it.product_query),
  };
  runRequest(override);
}

// ---------------------------------------------------------------------- operations
const OP_LABEL = {
  parse_request: 'Understand the request', search_catalog: 'Search the catalog', get_price: 'Get prices',
  check_availability: 'Check availability', rank_options: 'Rank the options', sign_offer: 'Sign the offer',
};
const OP_TONE = { done: ['good', '✓'], warning: ['warning', '!'], failed: ['critical', '✕'], skipped: ['neutral', '–'] };

function operationsPanel() {
  const ops = S.offer.operations;
  const total = ops.reduce((s, o) => s + (o.duration_ms || 0), 0);
  return h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h3', { text: 'How the agent worked' }),
      h('span', { class: 'muted small', text: `${ops.length} executed steps · ${fmt.num(total)} ms` })),
    h('ol', { class: 'ops' }, ops.map((o) => {
      const [tone, icon] = OP_TONE[o.status] || OP_TONE.done;
      return h('li', { class: `op op-${o.status}` },
        h('span', { class: `op-icon tone-${tone}`, 'aria-label': o.status, text: icon }),
        h('div', {},
          h('div', { class: 'op-title' }, h('strong', { text: OP_LABEL[o.name] || o.name }), h('code', { class: 'op-code', text: o.name }),
            o.duration_ms != null ? h('span', { class: 'muted small', text: `${fmt.num(o.duration_ms)} ms` }) : null),
          h('div', { class: 'op-detail', text: o.detail })));
    })));
}

// ---------------------------------------------------------------------- options per item
const SORTS = [['recommended', 'Recommended'], ['cost', 'Lowest cost'], ['reliability', 'Most reliable'], ['co2', 'Lowest CO₂e']];

function sortCands(cands, mode) {
  const arr = [...cands];
  const nullsLast = (a, b, f, dir) => {
    const va = f(a); const vb = f(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return dir * (va - vb);
  };
  if (mode === 'cost') arr.sort((a, b) => nullsLast(a, b, (c) => c.line_total, 1));
  if (mode === 'reliability') arr.sort((a, b) => nullsLast(a, b, (c) => c.delivery_reliability, -1));
  if (mode === 'co2') arr.sort((a, b) => nullsLast(a, b, (c) => c.co2_per_kg, 1));
  return arr;
}

function availabilityText(c) {
  const a = c.availability;
  const req = S.offer.parsed.delivery_date;
  switch (a.status) {
    case 'ok': return a.logistic_model === 'Terminal' ? `deliverable ${fmt.day(req)} (supplier stock)` : `in stock for ${fmt.day(req)}`;
    case 'partial': return `only ${fmt.num(a.available_amount)} ${unitLabel(c.sales_unit, a.available_amount)} available`;
    case 'late': return `earliest ${fmt.day(a.available_date)}`;
    case 'cutoff_passed': return `order cut-off was ${fmt.dateTime(a.closing_time)}`;
    case 'unavailable': return a.note || 'not orderable';
    default: return 'availability unknown';
  }
}

function candTooltip(c) {
  const a = c.availability;
  const amount = a.available_amount == null ? '–' : a.available_amount >= 1e6 ? (a.logistic_model === 'Terminal' ? 'not known (supplier)' : 'plenty') : fmt.num(a.available_amount);
  return [
    ['Product code', c.product_id],
    ['Unit price', c.price.unit_price != null ? `${fmt.eur(c.price.unit_price)} / ${unitWord(c.sales_unit)}` : 'no price'],
    c.content_per_sales_unit ? ['Content', `${fmt.num(c.content_per_sales_unit, 2)} ${c.content_unit} per ${unitWord(c.sales_unit)} (${c.sales_unit})`] : null,
    c.quantity != null ? ['Order quantity', `${fmt.num(c.quantity, 2)} ${unitLabel(c.sales_unit, c.quantity)}`] : null,
    ['Available', `${amount} · ${fmt.day(a.available_date)}`],
    a.closing_time ? ['Order cut-off', fmt.dateTime(a.closing_time)] : null,
    ['Stock model', a.logistic_model || '–'],
    c.price.list_price != null ? ['List price', fmt.eur(c.price.list_price)] : null,
    c.claims.length ? ['Claims', c.claims.map((x) => S.vocab.claims[x] || x).slice(0, 4).join(', ')] : null,
    c.labels.length ? ['Labels', c.labels.map((x) => S.vocab.labels[x] || x).slice(0, 3).join(', ')] : null,
    c.constraint_notes.length ? ['Doesn’t match', c.constraint_notes.join(', ')] : null,
  ];
}

function itemPanel(item, i) {
  const req = item.request;
  const mode = S.sort[i] || 'recommended';
  const sel = selectedId(i);
  const rows = sortCands(item.candidates, mode).map((c) => ({
    id: c.product_id,
    label: c.name,
    sublabel: [
      c.brand,
      c.quantity != null ? `${fmt.num(c.quantity, 2)} × ${unitWord(c.sales_unit)}${c.content_per_sales_unit ? ` of ${fmt.num(c.content_per_sales_unit, 2)} ${c.content_unit}` : ''}` : null,
      availabilityText(c),
    ].filter(Boolean).join(' · '),
    value: c.line_total,
    valueText: c.line_total != null ? fmt.eur(c.line_total) : '',
    noValueText: c.quantity == null ? 'Add a quantity to see the cost' : 'No price',
    status: statusChip(c.availability.status),
    tags: [
      c.product_id === item.recommended_product_id ? { kind: 'rec', text: 'Recommended' } : null,
      c.meets_constraints ? null : { kind: 'sub', text: 'Substitute' },
    ].filter(Boolean),
    figures: [
      c.price.comparison_price != null ? `${fmt.eur(c.price.comparison_price)} / ${c.price.comparison_unit}` : 'no comparison price',
      `Reliability ${fmt.pct(c.delivery_reliability)}`,
      c.co2_per_kg != null ? `${fmt.num(c.co2_per_kg, 2)} kg CO₂e/kg` : 'CO₂e n/a',
    ],
    tooltip: candTooltip(c),
    selected: c.product_id === sel,
  }));
  const rec = item.candidates.find((c) => c.product_id === item.recommended_product_id);
  const SHOW = 8;
  let visible = rows;
  if (!S.expanded[i] && rows.length > SHOW) {
    visible = rows.slice(0, SHOW);
    const chosen = rows.find((r) => r.selected);
    if (chosen && !visible.includes(chosen)) visible.push(chosen);
  }
  const toggle = rows.length > SHOW
    ? h('button', {
      type: 'button', class: 'link-button show-more',
      text: S.expanded[i] ? 'Show fewer options' : `Show all ${rows.length} options`,
      onClick: () => { S.expanded[i] = !S.expanded[i]; rerenderItem(i); },
    })
    : null;
  return h('section', { class: 'panel item-panel', id: `item-${i}` },
    h('div', { class: 'panel-head' }, h('h3', { text: itemTitle(req) }),
      h('div', { class: 'req-list' }, reqChips(req.constraints).map((rc) => h('span', { class: 'req-chip static', text: rc.label })))),
    h('p', { class: 'item-note', text: item.note }),
    item.candidates.length ? [
      h('div', { class: 'sort-tabs', role: 'group', 'aria-label': 'Sort options' },
        SORTS.map(([k, label]) => h('button', {
          type: 'button', class: `sort-tab${mode === k ? ' is-active' : ''}`, 'aria-pressed': String(mode === k), text: label,
          onClick: () => { S.sort[i] = k; rerenderItem(i); },
        }))),
      barList(visible, { ariaLabel: `Options for ${itemTitle(req)}`, onSelect: (pid) => selectCandidate(i, pid) }),
      toggle,
      h('p', { class: 'chart-caption', text: 'Bar length = cost of your quantity, excl. VAT. Choose an option to see how the decision changes. Hover for details.' }),
      rec ? h('p', { class: 'why' }, h('strong', { text: 'Why the agent recommends it: ' }), rec.rank_reason) : null,
    ] : h('p', { class: 'empty', text: 'Nothing matched. Edit the product words above and re-run.' }));
}

function rerenderItem(i, focusSelected = false) {
  const old = $(`#item-${i}`);
  const fresh = itemPanel(S.offer.items[i], i);
  old.replaceWith(fresh);
  if (focusSelected) fresh.querySelector('[aria-selected="true"]')?.focus();
}

async function selectCandidate(i, pid) {
  if (S.submitted || S.submissionPending || S.requestPending || S.dirty) return;
  const offerId = S.offer.id;
  const version = ++S.evaluationVersion;
  S.selections[i] = pid;
  const selections = { ...S.selections };
  S.evaluationPending = true;
  S.evaluationError = null;
  S.submitError = null;
  S.decision = null;
  S.rationale = null;
  rerenderItem(i, true);
  rerenderDecision();
  updateOrderControls();
  try {
    const decision = await api(`/api/offers/${offerId}/evaluate`, { method: 'POST', body: { selections } });
    if (version !== S.evaluationVersion || S.offer?.id !== offerId || S.dirty || S.requestPending) return;
    S.decision = decision;
  } catch (e) {
    if (version === S.evaluationVersion && S.offer?.id === offerId) {
      S.evaluationError = e.message;
      setStatus(e.message, true);
    }
  } finally {
    if (version === S.evaluationVersion && S.offer?.id === offerId) {
      S.evaluationPending = false;
      rerenderDecision();
      updateOrderControls();
    }
  }
}

// ---------------------------------------------------------------------- decision
function orderLines() {
  return S.offer.items.map((item, i) => ({ item, i, c: selectedCand(i) })).filter((l) => l.c && l.c.line_total != null);
}

function defaultRationale() {
  return S.offer.items.map((item, i) => {
    const c = selectedCand(i);
    if (!c) return null;
    const comparable = item.candidates.filter((x) => x.meets_constraints && x.availability.status === 'ok' && x.line_total != null)
      .sort((a, b) => a.line_total - b.line_total);
    const rank = comparable.findIndex((x) => x.product_id === c.product_id);
    const parts = [`${itemTitle(item.request)}: ${c.name}`];
    const want = reqText(item.request.constraints);
    parts.push(c.meets_constraints ? `matches the request${want ? ` (${want})` : ''}` : `substitute: ${c.constraint_notes.join(', ')}`);
    parts.push(availabilityText(c));
    if (c.price.comparison_price != null) parts.push(`${fmt.eur(c.price.comparison_price)}/${c.price.comparison_unit}`);
    if (rank === 0 && comparable.length > 1) parts.push(`cheapest of ${comparable.length} matching on-time options`);
    else if (rank > 0) parts.push(`#${rank + 1} by cost of ${comparable.length} matching on-time options`);
    if (c.delivery_reliability != null) parts.push(`delivery reliability ${fmt.pct(c.delivery_reliability)}`);
    return parts.join(' · ');
  }).filter(Boolean).join('\n');
}

function checkGroup(title, checks, collapsed = false) {
  if (!checks.length) return null;
  const list = h('ul', { class: 'checks' }, checks.map(checkItem));
  if (collapsed) return h('details', { class: 'check-group' }, h('summary', { text: title }), list);
  return h('div', { class: 'check-group' }, h('div', { class: 'label', text: title }), list);
}

function decisionPanel() {
  const d = S.decision;
  const o = S.offer;
  const lines = orderLines();
  const out = d ? OUTCOMES[d.outcome] : { tone: 'warning', icon: '!', title: 'Decision unavailable', button: 'Check selection first' };
  const checks = d?.checks || [];
  const failing = checks.filter((c) => !c.passed && c.severity !== 'info');
  const notes = checks.filter((c) => !c.passed && c.severity === 'info');
  const passed = checks.filter((c) => c.passed);
  return h('section', { class: 'panel decision-panel', id: 'decision' },
    h('div', { class: 'stat-label', text: 'Order total, excl. VAT' }),
    h('div', { class: 'hero-figure', text: fmt.eur(d?.total ?? lines.reduce((total, l) => total + l.c.line_total, 0)) }),
    h('div', { class: 'muted small', text: `${lines.length} ${plural('line', lines.length)} · delivery ${fmt.day(o.parsed.delivery_date)} · ordering as ${currentCustomerName(o.customer_id)}` }),
    h('div', { class: `outcome tone-${out.tone}` },
      h('span', { class: 'outcome-icon', 'aria-hidden': 'true', text: out.icon }),
      h('div', {}, h('strong', { text: out.title }), h('span', { text: d?.summary || orderStateMessage() }))),
    checkGroup('Needs attention', failing),
    checkGroup('Notes', notes),
    checkGroup(`Passed checks (${passed.length})`, passed, true),
    S.submitted ? submittedBlock() : submitBlock(lines, out, d),
    h('p', { class: 'fine' }, `Offer ${o.id} · fixed prices until ${fmt.time(o.valid_until)} · `,
      h('span', { title: `SHA-256 ${o.content_hash}\nEd25519 ${o.signature}`, text: `signed ✓ ${o.content_hash.slice(0, 12)}…` })));
}

function currentCustomerName(id) {
  return S.customers.find((c) => c.id === id)?.name || id;
}

function submitBlock(lines, out, d) {
  const area = h('textarea', { id: 'rationale', rows: '4', 'aria-describedby': 'rationale-hint' });
  area.value = S.rationale ?? defaultRationale();
  area.addEventListener('input', () => { S.rationale = area.value; });
  const stateMessage = orderStateMessage();
  const blocked = !d || d.outcome === 'reject' || lines.length === 0 || Boolean(stateMessage);
  return h('div', { class: 'submit-block' },
    stateMessage ? h('p', { class: 'offer-state', role: 'status', text: stateMessage }) : null,
    S.evaluationError ? h('p', { class: 'bad small', role: 'alert', text: S.evaluationError }) : null,
    S.submitError ? h('p', { class: 'bad small', role: 'alert', text: S.submitError }) : null,
    h('label', { for: 'rationale', text: 'Rationale (saved to the audit trail)' }),
    area,
    h('span', { id: 'rationale-hint', class: 'hint muted', text: 'Written by the agent from the data above. Edit it if you chose differently.' }),
    h('button', {
      class: 'btn btn-primary', type: 'button', id: 'submit-order', disabled: blocked,
      text: S.submissionPending ? 'Submitting…' : lines.length === 0 ? 'Add the missing details first' : out.button,
      onClick: () => submitOrder(area.value),
    }));
}

function submittedBlock() {
  const o = S.submitted;
  const map = {
    placed: ['good', '✓', `Order ${o.id} placed`, 'This order was within the customer’s authority, so the agent placed it without a person. (Simulated: the mock backend has no order endpoint.)'],
    pending_approval: ['warning', '⚑', `Order ${o.id} is waiting for approval`, 'A person will see it in the review queue, together with the reasons and the agent’s rationale.'],
    rejected: ['critical', '✕', `Order ${o.id} was not placed`, o.decision.summary],
  };
  const [tone, icon, title, text] = map[o.status];
  return h('div', { class: `result-box tone-${tone}`, role: 'status' },
    h('strong', {}, h('span', { 'aria-hidden': 'true', text: `${icon} ` }), title),
    h('p', { text }),
    h('div', { class: 'result-actions' },
      o.status === 'pending_approval' ? h('a', { class: 'btn btn-small btn-primary', href: '#review', text: 'Open review queue' }) : null,
      h('a', { class: 'btn btn-small', href: '#audit', text: 'See audit trail', onClick: () => { S.auditSubject = o.id; } }),
      h('button', { class: 'btn btn-small', type: 'button', text: 'New request', onClick: newRequest })));
}

function rerenderDecision() {
  const old = $('#decision');
  if (old) old.replaceWith(decisionPanel());
}

async function submitOrder(rationale) {
  const blocked = orderStateMessage();
  if (!S.offer || S.submitted || !S.decision || blocked || S.decision.outcome === 'reject' || !orderLines().length) {
    if (blocked) { setStatus(blocked, true); if (S.offer) rerenderDecision(); updateOrderControls(); }
    return;
  }
  const offerId = S.offer.id;
  const selections = { ...S.selections };
  S.submissionPending = true;
  S.submitError = null;
  rerenderDecision();
  updateOrderControls();
  try {
    S.submitted = await api('/api/orders', { method: 'POST', body: { offer_id: offerId, selections, rationale } });
    refreshReviewCount();
  } catch (e) {
    S.submitError = e.message;
    setStatus(e.message, true);
  } finally {
    S.submissionPending = false;
    rerenderDecision();
    updateOrderControls();
  }
  if (S.submitted) {
    try { S.customers = await api('/api/customers'); renderTrustCard(); } catch { /* Order confirmation is already authoritative. */ }
  }
}

function newRequest() {
  if (S.submissionPending) return;
  S.requestVersion += 1;
  S.evaluationVersion += 1;
  Object.assign(S, { offer: null, decision: null, edit: null, submitted: null, selections: {}, sort: {}, rationale: null, dirty: false, requestChanged: false, requestPending: false, evaluationPending: false, evaluationError: null, submitError: null });
  $('#results').hidden = true;
  $('#results').replaceChildren();
  $('#request-text').value = '';
  renderCustomerSelect();
  setStatus('');
  updateOrderControls();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  $('#request-text').focus();
}

// ====================================================================== review page
function linesTable(lines) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, h('th', { text: 'Product' }), h('th', { class: 'num', text: 'Quantity' }),
      h('th', { class: 'num', text: 'Unit price' }), h('th', { class: 'num', text: 'Line total' }), h('th', { text: 'Delivery' }))),
    h('tbody', {}, lines.map((l) => h('tr', {},
      h('td', {}, l.name, h('span', { class: 'cell-sub', text: `${l.product_id}${l.is_recommended ? '' : ' · chosen instead of the recommendation'}` })),
      h('td', { class: 'num', text: `${fmt.num(l.quantity, 2)} ${unitLabel(l.sales_unit, l.quantity)}` }),
      h('td', { class: 'num', text: fmt.eur(l.unit_price) }),
      h('td', { class: 'num', text: fmt.eur(l.line_total) }),
      h('td', { text: fmt.day(l.delivery_date) }))))));
}

async function loadReview() {
  $('#reviewer').value = $('#reviewer').value || storage.get('aimo.reviewer', '');
  try {
    const [pending, all] = await Promise.all([api('/api/orders?status=pending_approval'), api('/api/orders')]);
    $('#review-list').replaceChildren(...(pending.length ? pending.map(reviewCard)
      : [h('p', { class: 'empty', text: 'Nothing to review right now. Orders the agent may not place on its own appear here.' })]));
    const done = all.filter((o) => o.status !== 'pending_approval').slice(0, 15);
    $('#review-done').replaceChildren(done.length ? h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Order', 'Customer', 'Status', 'Decided by', 'Total'].map((t, k) => h('th', { class: k === 4 ? 'num' : null, text: t })))),
      h('tbody', {}, done.map((o) => h('tr', {},
        h('td', {}, o.id, h('span', { class: 'cell-sub', text: fmt.dateTime(o.created_at) })),
        h('td', { text: o.customer_name }),
        h('td', {}, o.status === 'placed' ? toneChip('good', '✓', 'Placed') : toneChip('critical', '✕', 'Rejected')),
        h('td', { text: o.reviewer ? `${o.reviewer}${o.review_comment ? `: “${o.review_comment}”` : ''}` : 'Agent (within authority)' }),
        h('td', { class: 'num', text: fmt.eur(o.total) }))))))
      : h('p', { class: 'muted', text: 'No decided orders yet.' }));
  } catch (e) {
    $('#review-list').replaceChildren(h('p', { class: 'empty', text: e.message }));
  }
  refreshReviewCount();
}

function reviewCard(o) {
  const c = S.customers.find((x) => x.id === o.customer_id);
  const reasons = o.decision.checks.filter((ch) => !ch.passed && ch.severity !== 'info');
  const notes = o.decision.checks.filter((ch) => !ch.passed && ch.severity === 'info');
  const expired = o.offer_valid_until && new Date(o.offer_valid_until) < new Date();
  const verify = c?.status === 'unverified' && c.business_id_valid
    ? h('input', { type: 'checkbox', id: `verify-${o.id}` }) : null;
  const comment = h('input', { type: 'text', placeholder: 'Comment (optional, saved to the audit trail)', 'aria-label': `Comment on ${o.id}`, maxlength: '500' });
  const pending = S.reviewPending.has(o.id);
  const hardBlocked = reasons.some((ch) => ch.severity === 'block');
  return h('article', { class: 'panel review-card', 'data-order-id': o.id },
    h('div', { class: 'review-head' },
      h('div', {}, h('h3', { text: `${o.id} · ${o.customer_name}` }), h('div', { class: 'muted small', text: `Created ${fmt.dateTime(o.created_at)} · offer ${o.offer_id}` })),
      h('div', { class: 'review-total' }, h('div', { class: 'stat-label', text: 'Total excl. VAT' }), h('div', { class: 'stat-value', text: fmt.eur(o.total) }))),
    h('div', { class: 'review-chips' },
      c ? trustChip(c.status) : null,
      expired ? toneChip('warning', '!', 'Offer expired: prices may have changed') : toneChip('good', '✓', `Prices fixed until ${fmt.time(o.offer_valid_until)}`)),
    h('h4', { text: 'Why it needs a person' }),
    h('ul', { class: 'checks' }, reasons.map(checkItem), notes.map(checkItem)),
    linesTable(o.lines),
    o.request_text ? h('blockquote', { class: 'quote' }, h('span', { class: 'muted small', text: 'Request: ' }), o.request_text) : null,
    o.rationale ? h('div', { class: 'rationale' }, h('span', { class: 'muted small', text: 'Agent rationale' }), h('p', { text: o.rationale })) : null,
    h('div', { class: 'review-actions' },
      verify ? h('label', { class: 'check-inline', for: `verify-${o.id}` }, verify, `Also verify ${c.name} and add it to the whitelist`) : null,
      comment,
      h('button', { class: 'btn btn-primary btn-small', type: 'button', text: 'Approve', disabled: pending || expired || hardBlocked, onClick: () => decide(o, 'approve', comment.value, verify?.checked) }),
      h('button', { class: 'btn btn-small', type: 'button', text: 'Reject', disabled: pending, onClick: () => decide(o, 'reject', comment.value, false) })),
    expired ? h('p', { class: 'offer-state', text: 'This offer expired. Reject it and request a fresh offer before approving.' }) : null,
    h('p', { class: 'review-error bad small', role: 'alert', hidden: true }));
}

async function decide(order, decision, comment, verifyCustomer) {
  if (S.reviewPending.has(order.id)) return;
  const reviewer = $('#reviewer').value.trim();
  if (!reviewer) { $('#reviewer').focus(); $('#reviewer').placeholder = 'Enter your name first'; return; }
  if (decision === 'approve' && ((order.offer_valid_until && new Date(order.offer_valid_until) <= new Date()) || order.decision.checks.some((ch) => !ch.passed && ch.severity === 'block'))) {
    await loadReview();
    return;
  }
  storage.set('aimo.reviewer', reviewer);
  S.reviewPending.add(order.id);
  const card = document.querySelector(`.review-card[data-order-id="${CSS.escape(order.id)}"]`);
  const controls = [...(card?.querySelectorAll('button, input') || [])];
  const disabledBefore = controls.map((el) => el.disabled);
  controls.forEach((el) => { el.disabled = true; });
  card?.setAttribute('aria-busy', 'true');
  const error = card?.querySelector('.review-error');
  if (error) error.hidden = true;
  try {
    await api(`/api/orders/${order.id}/decision`, { method: 'POST', body: { decision, reviewer, comment, verify_customer: Boolean(verifyCustomer) } });
    try { S.customers = await api('/api/customers'); renderCustomerSelect(); } catch { /* Decision was saved; still refresh the queue. */ }
    await loadReview();
  } catch (e) {
    if (error) { error.textContent = e.message; error.hidden = false; }
    else alert(e.message);
  } finally {
    S.reviewPending.delete(order.id);
    controls.forEach((el, i) => { el.disabled = disabledBefore[i]; });
    card?.removeAttribute('aria-busy');
  }
}

// ====================================================================== customers page
function validYTunnus(v) {
  const m = /^(\d{7})-(\d)$/.exec(v.trim());
  if (!m) return false;
  const w = [7, 9, 10, 5, 8, 4, 2];
  const r = [...m[1]].reduce((s, d, k) => s + Number(d) * w[k], 0) % 11;
  if (r === 1) return false;
  return Number(m[2]) === (r === 0 ? 0 : 11 - r);
}

async function loadCustomersPage() {
  try { S.customers = await api('/api/customers'); } catch (e) { $('#customer-table').replaceChildren(h('p', { class: 'empty', text: e.message })); return; }
  renderCustomerSelect();
  $('#customer-table').replaceChildren(h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, ['Customer', 'Business ID', 'Trust', 'Orders', 'Avg order', 'Auto-approve up to', 'Hard rules', ''].map((t, k) =>
      h('th', { class: [3, 4, 5].includes(k) ? 'num' : null, text: t })))),
    h('tbody', {}, S.customers.map((c) => h('tr', {},
      h('td', {}, h('strong', { text: c.name }), h('span', { class: 'cell-sub', text: `${c.id} · ${c.segment}` })),
      h('td', {}, c.business_id || '–', h('span', { class: 'cell-sub', text: c.business_id_valid ? '✓ checksum OK' : '✕ checksum fails' })),
      h('td', {}, trustChip(c.status)),
      h('td', { class: 'num', text: fmt.num(c.past_orders) }),
      h('td', { class: 'num', text: c.avg_order_value != null ? fmt.eur0(c.avg_order_value) : '–' }),
      h('td', { class: 'num', text: c.auto_approve_limit > 0 ? fmt.eur0(c.auto_approve_limit) : 'every order reviewed' }),
      h('td', {}, c.strict_allergens.length ? h('div', { class: 'mini-chips' }, c.strict_allergens.map((a) => h('span', { class: 'mini-chip', text: `never ${S.vocab.allergens[a] || a}` }))) : '–'),
      h('td', {}, c.status === 'unverified'
        ? h('button', { class: 'btn btn-small', type: 'button', text: 'Verify & whitelist', disabled: !c.business_id_valid, onClick: () => verifyCustomer(c) })
        : null)))))));
}

async function verifyCustomer(c) {
  const reviewer = storage.get('aimo.reviewer', '') || prompt('Your name (saved to the audit trail):') || '';
  if (!reviewer.trim()) return;
  storage.set('aimo.reviewer', reviewer.trim());
  try {
    await api(`/api/customers/${c.id}/verify`, { method: 'POST', body: { reviewer: reviewer.trim() } });
    loadCustomersPage();
  } catch (e) { alert(e.message); }
}

function initRegisterForm() {
  const bid = $('#reg-bid');
  const hint = $('#reg-bid-hint');
  bid.addEventListener('input', () => {
    const v = bid.value.trim();
    if (!v) { hint.textContent = ''; hint.className = 'hint'; return; }
    const ok = validYTunnus(v);
    hint.textContent = ok ? '✓ Y-tunnus checksum OK' : '✕ Not a valid Y-tunnus (format 1234567-8, check digit must match)';
    hint.className = `hint ${ok ? 'ok' : 'bad'}`;
  });
  $('#register-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('#reg-name').value.trim();
    const status = $('#reg-status');
    if (name.length < 2) { status.textContent = 'Enter the business name.'; status.className = 'run-status bad'; return; }
    try {
      const c = await api('/api/customers', { method: 'POST', body: { name, segment: $('#reg-segment').value, business_id: bid.value.trim() } });
      status.textContent = `${c.name} registered as ${c.id}: not on the whitelist yet, so its orders go to the review queue.`;
      status.className = 'run-status';
      e.target.reset();
      hint.textContent = '';
      await loadCustomersPage();
    } catch (err) { status.textContent = err.message; status.className = 'run-status bad'; }
  });
}

// ====================================================================== audit page
function actorChip(actor) {
  const [kind, who] = actor.split(':');
  const label = { customer: `Customer ${who || ''}`, agent: 'Agent', policy: 'Policy', human: `Person · ${who || ''}`, system: 'System' }[kind] || actor;
  const cls = { agent: 'actor-agent', human: 'actor-person', policy: 'actor-policy' }[kind] || '';
  return h('span', { class: `actor ${cls}`, text: label.trim() });
}

async function loadAudit(subject = S.auditSubject) {
  S.auditSubject = null;
  let res;
  try {
    res = await api(`/api/audit${subject ? `?subject=${encodeURIComponent(subject)}` : ''}`);
  } catch (e) { $('#audit-list').replaceChildren(h('li', { class: 'empty', text: e.message })); return; }
  const chain = res.chain;
  const toolbar = [
    chain.valid ? toneChip('good', '✓', `Hash chain intact · ${fmt.num(chain.count)} events`) : toneChip('critical', '✕', `Chain broken at event #${chain.broken_at}`),
  ];
  if (subject) toolbar.push(h('span', {}, 'Showing ', h('strong', { text: subject }), ' · ', h('button', { class: 'link-button', type: 'button', text: 'show everything', onClick: () => loadAudit('') })));
  $('#audit-toolbar').replaceChildren(...toolbar);
  $('#audit-list').replaceChildren(...(res.events.length ? res.events.map((e) => h('li', { class: 'event' },
    h('time', { datetime: e.ts, text: fmt.dateTime(e.ts) }),
    actorChip(e.actor),
    h('div', { class: 'event-body' },
      h('p', { text: e.summary }),
      h('div', { class: 'event-meta' },
        h('button', { class: 'link-button', type: 'button', text: e.subject, title: 'Show only this', onClick: () => loadAudit(e.subject) }),
        h('code', { text: e.action }),
        h('span', { title: `hash ${e.hash}\nprevious ${e.prev_hash}`, text: `#${e.seq} · ${e.hash.slice(0, 10)}…` })),
      Object.keys(e.data || {}).length ? h('details', {}, h('summary', { text: 'Details' }), h('pre', { text: JSON.stringify(e.data, null, 2) })) : null)))
    : [h('li', { class: 'empty', text: 'No events yet. Place an order to see the trail.' })]));
}

// ====================================================================== explore page
async function runExplore() {
  S.exploreLoaded = true;
  const q = $('#explore-q').value.trim();
  const body = $('#explore-body');
  if (q.length < 2) { body.replaceChildren(h('p', { class: 'empty', text: 'Type at least two letters.' })); return; }
  body.classList.add('is-refreshing');
  try {
    const params = new URLSearchParams({ q, filters: [...S.explore.filters].join(','), category: S.explore.category, limit: '40' });
    const res = await api(`/api/catalog/search?${params}`);
    renderFacets(res.facets);
    renderExplore(res);
  } catch (e) {
    body.replaceChildren(h('p', { class: 'empty', text: e.message }));
  } finally {
    body.classList.remove('is-refreshing');
  }
}

function renderFacets(facets) {
  $('#explore-facets').replaceChildren(...facets.map((f) => h('button', {
    type: 'button', class: 'facet', 'aria-pressed': String(f.active),
    onClick: () => { if (S.explore.filters.has(f.key)) S.explore.filters.delete(f.key); else S.explore.filters.add(f.key); S.explore.category = ''; runExplore(); },
  }, f.label, h('span', { class: 'count', text: fmt.num(f.count) }))));
}

function kpi(label, value, sub) {
  return h('div', { class: 'kpi' }, h('div', { class: 'stat-label', text: label }), h('div', { class: 'stat-value', text: value }), sub ? h('div', { class: 'muted small', text: sub }) : null);
}

function renderExplore(res) {
  const k = res.kpis;
  const withCo2 = res.groups.filter((g) => g.co2 != null);
  const chart = withCo2.length ? barList(withCo2.map((g) => ({
    id: g.category,
    label: g.example,
    sublabel: `Product group ${g.category} · ${fmt.num(g.count)} ${plural('product', g.count)} · reliability ${fmt.pct(g.reliability)}`,
    value: g.co2,
    valueText: `${fmt.num(g.co2, 2)} kg`,
    selected: g.category === S.explore.category,
    tooltip: [['Product group', g.category], ['Products here', fmt.num(g.count)], ['Avg kg CO₂e per kg', fmt.num(g.co2, 3)], ['Avg delivery reliability', fmt.pct(g.reliability)]],
  })), {
    ariaLabel: 'Carbon footprint by product group',
    onSelect: (cat) => { S.explore.category = S.explore.category === cat ? '' : cat; runExplore(); },
  }) : h('p', { class: 'empty', text: 'No carbon-footprint data for these products.' });

  const table = h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, ['Product', 'Group', 'Sold as', 'Claims & labels', 'Reliability', 'kg CO₂e/kg', 'Price & stock'].map((t, i) =>
      h('th', { class: [4, 5].includes(i) ? 'num' : null, text: t })))),
    h('tbody', {}, res.results.map((p) => {
      const cell = h('td', {}, h('button', { class: 'btn btn-small', type: 'button', text: 'Check', onClick: () => quote(p, cell) }));
      const chips = [...(p.claims || []).map((c) => S.vocab.claims[c]).filter(Boolean), ...(p.labels || []).map((l) => S.vocab.labels[l]).filter(Boolean)].slice(0, 4);
      return h('tr', {},
        h('td', {}, p.name, h('span', { class: 'cell-sub', text: [p.brand, p.id, p.country?.toUpperCase()].filter(Boolean).join(' · ') })),
        h('td', { text: p.category }),
        h('td', { text: `${unitWord(p.sales_unit)} (${p.sales_unit})${p.content ? ` · ${fmt.num(p.content, 2)} ${p.content_unit}` : ''}` }),
        h('td', {}, chips.length ? h('div', { class: 'mini-chips' }, chips.map((c) => h('span', { class: 'mini-chip', text: c }))) : '–'),
        h('td', { class: 'num', text: fmt.pct(p.reliability) }),
        h('td', { class: 'num', text: p.co2 != null ? fmt.num(p.co2, 2) : 'n/a' }),
        cell);
    }))));

  const groupNote = res.groups_total > withCo2.length
    ? `Showing ${withCo2.length} of ${res.groups_total} product groups (groups without CO₂e data are left out).` : '';
  $('#explore-body').replaceChildren(
    h('div', { class: 'kpi-row' },
      kpi('Products found', fmt.num(k.total), `for “${res.phrases.join(' / ')}”`),
      kpi('With delivery data', fmt.num(k.with_reliability), k.total ? `${fmt.pct(k.with_reliability / k.total)} of results` : null),
      kpi('Avg delivery reliability', fmt.pct(k.avg_reliability), 'share of ordered quantity delivered'),
      kpi('Product groups', fmt.num(res.groups_total), S.explore.category ? `filtered to group ${S.explore.category}` : 'click a bar below to filter')),
    h('section', { class: 'panel item-panel' },
      h('div', { class: 'panel-head' }, h('h3', { text: 'Carbon footprint by product group' }),
        h('span', { class: 'muted small', text: 'kg CO₂e per kg, average of the product group (not per product)' })),
      chart,
      h('p', { class: 'chart-caption', text: `Choose a group to filter the list below; choose it again to clear. ${groupNote}` })),
    h('section', { class: 'panel item-panel' },
      h('div', { class: 'panel-head' }, h('h3', { text: `Products${S.explore.category ? ` in group ${S.explore.category}` : ''}` }),
        h('span', { class: 'muted small', text: `first ${res.results.length} of ${fmt.num(res.total)} · live price and stock only for products you check, as Valio Aimo’s API guidelines ask` })),
      table));
}

async function quote(p, cell) {
  cell.replaceChildren(h('span', { class: 'muted small', text: 'Checking…' }));
  try {
    const day = $('#explore-date').value || nextWeekday();
    const res = await api('/api/catalog/quote', { method: 'POST', body: { product_ids: [p.id], requested_date: day } });
    const r = res[p.id];
    const price = r.price.unit_price != null ? `${fmt.eur(r.price.unit_price)} / ${unitWord(p.sales_unit)}` : 'no price';
    const cmp = r.price.comparison_price != null ? ` · ${fmt.eur(r.price.comparison_price)}/${r.price.comparison_unit}` : '';
    cell.replaceChildren(h('div', {}, h('strong', { text: price }), h('span', { class: 'cell-sub', text: `${cmp.replace(' · ', '')} for ${fmt.day(day)}` }), statusChip(r.availability.status)));
  } catch (e) {
    cell.replaceChildren(h('span', { class: 'hint bad', text: e.message }));
  }
}

// ====================================================================== boot
async function init() {
  try { S.vocab = await api('/api/vocab'); } catch { /* keep defaults */ }
  try { S.customers = await api('/api/customers'); } catch (e) { setStatus(e.message, true); }
  renderCustomerSelect();
  renderExamples();
  initRegisterForm();
  $('#explore-date').value = nextWeekday();

  $('#customer').addEventListener('change', () => { storage.set('aimo.customer', $('#customer').value); renderTrustCard(); markDirty(true); });
  $('#request-text').addEventListener('input', () => markDirty(true));
  $('#request-form').addEventListener('submit', (e) => { e.preventDefault(); runRequest(); });
  $('#request-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) runRequest(); });
  $('#explore-form').addEventListener('submit', (e) => { e.preventDefault(); S.explore.category = ''; runExplore(); });
  $('#reviewer').addEventListener('change', () => storage.set('aimo.reviewer', $('#reviewer').value.trim()));
  $('#reset-demo').addEventListener('click', async () => {
    if (!confirm('Reset the demo? Customers return to the seed data; orders and the audit trail are cleared.')) return;
    await api('/api/admin/reset', { method: 'POST' });
    S.customers = await api('/api/customers');
    newRequest();
    refreshReviewCount();
    route();
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('scroll', hideTip, { passive: true });
  route();
  updateOrderControls();
  loadHealth();
  refreshReviewCount();
}

init();
