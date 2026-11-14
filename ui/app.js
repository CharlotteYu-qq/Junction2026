import { $, fmt, h, localISO, plural, storage } from './dom.js';
import { barList, hideTip, toneChip } from './charts.js';

// ====================================================================== state & api
const S = {
  session: null, // { signed_in, role: 'buyer', name, customer_id, password_required, customers }
  vocab: { claims: {}, allergens: {}, labels: {}, sales_units: {}, requirement_options: [] },
  customers: [],
  // The conversation with the agent: { role: 'buyer' | 'agent', text }. Messages before `from` belong to an
  // order that is done; `aside` marks the cancellation exchange. Neither is sent to the agent again.
  chat: { messages: [], understood: null, from: 0 },
  pendingCancel: null, // { order, reason }: the agent asked the buyer to confirm cancelling this order
  detailsOpen: false, // the buyer opened "what the agent understood / how it worked"
  notice: null, // reminder to double-check, when the rule-based fallback understood the request
  voice: null, // the running speech recognition, while the buyer is speaking
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
  if (res.status === 401 && S.session?.signed_in && !path.startsWith('/api/session')) {
    showSignIn('Your session ended. Sign in again.'); // expired or signed out in another tab
  }
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

function checkItem(c) {
  const kind = c.passed ? 'pass' : c.severity;
  const icon = { pass: '✓', review: '!', block: '✕', info: 'i' }[kind];
  const sr = { pass: 'Passed', review: 'Needs a person', block: 'Blocks the order', info: 'Note' }[kind];
  return h('li', { class: `check ${kind}` },
    h('span', { class: 'check-icon', 'aria-hidden': 'true', text: icon }),
    h('span', {}, h('span', { class: 'visually-hidden', text: `${sr}: ` }), c.message));
}

// ====================================================================== status & navigation
const PAGES = ['order', 'orders', 'explore'];
const ROLE_PAGES = { buyer: PAGES }; // the server checks the same role on every API call
const loaders = { orders: loadMyOrders, explore: () => S.exploreLoaded || runExplore() };

function route() {
  if (!S.session?.signed_in) return;
  const allowed = ROLE_PAGES[S.session.role];
  const page = allowed.includes(location.hash.slice(1)) ? location.hash.slice(1) : allowed[0];
  $('#page-signin').hidden = true;
  for (const p of PAGES) $(`#page-${p}`).hidden = p !== page;
  document.querySelectorAll('.main-nav a').forEach((a) => {
    a.classList.toggle('is-active', a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  hideTip();
  loaders[page]?.();
}

// ====================================================================== order page
// Each buyer sees its own demo scenarios first, then a couple that work for anyone.
const EXAMPLES = [
  { label: '1,000 L lactose-free milk', customer: 'C-1001',
    text: 'I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability.' },
  { label: 'Cream & butter', customer: 'C-1001', text: '50 l lactose-free whipping cream and 20 kg Finnish butter for Monday' },
  { label: 'Suomeksi', customer: 'C-1001', text: 'Tarvitsen 200 litraa laktoositonta maitoa maanantaiksi' },
  { label: 'Nut-free muesli', customer: 'C-1002', text: '15 kg muesli for Tuesday, everything must be nut-free' },
  { label: 'Potatoes & tomatoes', customer: 'C-2001', generic: true, text: '30 kg potatoes and 10 kg tomatoes for Tuesday' },
  { label: 'Lactose-free milk', generic: true, text: '200 litres of lactose-free milk by Friday' },
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
    ? 'You are changing the request. Send it to the agent before ordering.'
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
  $('#voice-btn').disabled = S.requestPending || S.submissionPending;
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

// The buyer orders as the customer it signed in as; there is no way to pick another one.
function currentCustomer() {
  return S.customers.find((c) => c.id === S.session?.customer_id);
}

function renderExamples() {
  const mine = EXAMPLES.filter((ex) => ex.customer === S.session.customer_id);
  const shown = [...mine, ...EXAMPLES.filter((ex) => ex.generic && !mine.includes(ex))].slice(0, 4);
  $('#examples').replaceChildren(
    h('span', { class: 'muted small', text: 'Try:' }),
    ...shown.map((ex) => h('button', {
      type: 'button', class: 'example', text: ex.label,
      onClick: () => {
        newRequest({ focus: false }); // an example starts a new conversation
        $('#request-text').value = ex.text;
        sendMessage();
      },
    })));
}

// ---------------------------------------------------------------------- conversation with the agent
// The buyer and the agent talk until the request is complete: the agent asks for a missing quantity,
// unit or delivery date (or an unclear condition) before it checks prices and stock with Valio Aimo.
function renderThread() {
  const msgs = S.chat.messages;
  const last = msgs[msgs.length - 1];
  const waiting = last?.role === 'agent' && last.question;
  $('#chat-thread').hidden = msgs.length === 0;
  $('#chat-thread').replaceChildren(...msgs.map((m) => h('div', { class: `chat-msg chat-${m.role}` },
    h('span', { class: 'chat-who', text: m.role === 'buyer' ? (currentCustomer()?.name || 'You') : 'Ordering agent' }),
    h('p', { text: m.text }),
    m.notice ? h('p', { class: 'chat-note', role: 'note', text: m.notice }) : null,
    m === last && waiting ? h('button', {
      type: 'button', class: 'link-button chat-skip', text: 'Show options with what you know so far',
      disabled: S.requestPending, onClick: () => sendMessage({ skip: true }),
    }) : null,
    m === last && m.action === 'cancel' && S.pendingCancel ? h('div', { class: 'chat-actions' },
      h('button', { type: 'button', class: 'btn btn-primary btn-small', text: 'Yes, cancel it', onClick: confirmCancel }),
      h('button', { type: 'button', class: 'btn btn-small', text: 'No, keep it', onClick: keepOrder })) : null,
    m.action === 'orders' ? h('a', { class: 'chat-action link-button', href: '#orders', text: 'See my orders' }) : null)));
  $('#chat-thread').lastElementChild?.scrollIntoView({ block: 'nearest' }); // keep the newest message in view
  $('#request-label').textContent = waiting ? 'Your answer' : msgs.length ? 'Anything to change or add?' : 'Your request';
  $('#request-text').placeholder = waiting ? 'e.g. 200 litres, delivered on Friday' : 'e.g. I need 1,000 litres of lactose-free milk by Monday';
  $('#run-btn').textContent = msgs.length ? 'Send' : 'Find the best option';
}

function offerSummary(offer) {
  const p = offer.parsed;
  const wants = p.items.map((it) => itemTitle(it)).join('; ') || 'no products recognised';
  const picks = offer.items.map((item) => item.candidates.find((c) => c.product_id === item.recommended_product_id))
    .filter(Boolean).map((c) => `${c.name} × ${fmt.num(c.quantity, 2)} ${unitLabel(c.sales_unit, c.quantity)} = ${fmt.eur(c.line_total)}`);
  return `Got it: ${wants}, delivery ${fmt.day(p.delivery_date)}. `
    + (picks.length ? `I checked prices and stock with Valio Aimo. I recommend ${picks.join('; ')}. Choose an option below and place the order.`
      : 'I found no option that can be ordered right now: see the options below, or tell me what to change.');
}

function say(text, extra = {}) {
  S.chat.messages.push({ role: 'agent', text, ...extra });
  renderThread();
}

// The options and the decision appear below the conversation; the buyer picks one and places the order.
function showOffer(res, { scroll = true } = {}) {
  Object.assign(S, { offer: res.offer, decision: res.decision, selections: {}, sort: {}, expanded: {}, rationale: null, submitted: null, dirty: false, requestChanged: false });
  S.edit = structuredClone(res.offer.parsed);
  S.chat.understood = res.offer.parsed;
  S.notice = res.notice || null; // set when the rule-based fallback understood the request
  const idx = S.customers.findIndex((c) => c.id === res.customer.id);
  if (idx >= 0) S.customers[idx] = res.customer;
  setStatus('');
  renderResults();
  if (scroll) $('#results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function startPending(message) {
  S.requestPending = true;
  S.dirty = true;
  S.evaluationVersion += 1;
  S.evaluationPending = false;
  S.evaluationError = null;
  S.submitError = null;
  updateOrderControls();
  if (S.offer) rerenderDecision();
  $('#results').classList.add('is-refreshing');
  setStatus(message);
}

function endPending(version) {
  if (version !== S.requestVersion) return;
  S.requestPending = false;
  $('#results').classList.remove('is-refreshing');
  if (S.offer) rerenderDecision();
  renderThread();
  updateOrderControls();
}

const CONFIRM_YES = /^\s*(?:yes|yep|yeah|ok(?:ay)?|sure|please do|do it|confirm\w*|kyllä|joo|ja)\b/i;
const CONFIRM_NO = /^\s*(?:no|nope|don'?t|keep|ei|nej)\b/i;

function conversationForAgent() {
  return S.chat.messages.slice(S.chat.from).filter((m) => !m.aside).map(({ role, text }) => ({ role, text }));
}

async function sendMessage({ skip = false } = {}) {
  if (S.requestPending || S.submissionPending) return;
  cancelVoice(); // sending while still listening keeps what was heard so far
  const text = $('#request-text').value.trim();
  if (!skip && text && S.pendingCancel && (CONFIRM_YES.test(text) || CONFIRM_NO.test(text))) {
    S.chat.messages.push({ role: 'buyer', text, aside: true }); // a typed answer to "cancel order …?"
    $('#request-text').value = '';
    if (CONFIRM_YES.test(text)) await confirmCancel(); else keepOrder();
    return;
  }
  if (!skip && !text) { setStatus('Write what you need first.', true); $('#request-text').focus(); return; }
  S.pendingCancel = null; // anything else the buyer says leaves the cancellation unanswered
  if (!skip && S.submitted && S.chat.from < S.chat.messages.length) {
    S.chat.from = S.chat.messages.length; // that order is done: this message starts a new one
    S.chat.understood = null;
  }
  if (!skip) {
    S.chat.messages.push({ role: 'buyer', text });
    $('#request-text').value = '';
  }
  const wasDirty = S.dirty;
  const version = ++S.requestVersion;
  startPending(skip ? 'Checking prices and availability with what the agent knows so far…' : 'The agent is reading your message…');
  renderThread();
  try {
    const res = await api('/api/chat', {
      method: 'POST',
      body: { messages: conversationForAgent(), understood: S.chat.understood, skip_questions: skip },
    });
    if (version !== S.requestVersion) return;
    if (res.kind === 'cancel' || res.kind === 'answer') { // about an order already placed, not the current request
      S.chat.messages[S.chat.messages.length - 1].aside = true;
      S.dirty = wasDirty;
      if (res.kind === 'cancel') S.pendingCancel = { order: res.order, reason: text };
      S.chat.messages.push({ role: 'agent', text: res.kind === 'cancel' ? res.question : res.text, aside: true, action: res.kind === 'cancel' ? 'cancel' : null });
      setStatus('');
      return;
    }
    if (res.kind === 'question') {
      S.chat.understood = res.understood;
      S.chat.messages.push({ role: 'agent', text: res.question, question: true, notice: res.notice });
      setStatus('');
      return;
    }
    showOffer(res);
    S.chat.messages.push({ role: 'agent', text: offerSummary(res.offer), notice: res.notice });
  } catch (e) {
    if (version !== S.requestVersion) return;
    setStatus(e.message, true);
    if (!skip) { S.chat.messages.pop(); $('#request-text').value = text; } // let the buyer send it again
  } finally {
    endPending(version);
    if (version === S.requestVersion && !S.requestPending) $('#request-text').focus({ preventScroll: true });
  }
}

// ---------------------------------------------------------------------- voice input
// The browser's own speech recognition (Chrome, Edge, Safari). The words land in the text box, so the
// buyer can check and edit them before sending; nothing is sent automatically.
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const VOICE_ERRORS = {
  'not-allowed': 'Microphone access is blocked: allow it in the browser to speak your request.',
  'service-not-allowed': 'Microphone access is blocked: allow it in the browser to speak your request.',
  'no-speech': 'No speech was heard. Try again, a little closer to the microphone.',
  'audio-capture': 'No microphone was found.',
  network: 'Voice input needs an internet connection.',
};

function initVoice() {
  if (!SpeechRecognition) return; // e.g. Firefox: typing only
  const lang = $('#voice-lang');
  const saved = storage.get('aimo.voiceLang', '');
  const browser = (navigator.language || '').slice(0, 2);
  lang.value = saved || { fi: 'fi-FI', sv: 'sv-SE' }[browser] || 'en-GB';
  lang.addEventListener('change', () => storage.set('aimo.voiceLang', lang.value));
  $('#voice-controls').hidden = false;
  $('#voice-btn').addEventListener('click', () => (S.voice ? S.voice.stop() : startVoice()));
}

function startVoice() {
  const box = $('#request-text');
  const before = box.value.trim();
  const rec = new SpeechRecognition();
  rec.lang = $('#voice-lang').value;
  rec.interimResults = true;
  rec.continuous = true; // keep listening through short pauses; the buyer clicks again to stop
  let heard = '';
  rec.onresult = (e) => {
    let interim = '';
    heard = '';
    for (const r of e.results) {
      if (r.isFinal) heard += r[0].transcript;
      else interim += r[0].transcript;
    }
    box.value = [before, (heard + interim).trim()].filter(Boolean).join(' ');
  };
  rec.onerror = (e) => { if (e.error !== 'aborted') setStatus(VOICE_ERRORS[e.error] || `Voice input stopped (${e.error}).`, true); };
  rec.onend = () => {
    S.voice = null;
    box.value = [before, heard.trim()].filter(Boolean).join(' ');
    if (box.value.trim() !== before) markDirty(true);
    renderVoice();
    box.focus();
  };
  S.voice = rec;
  setStatus('');
  rec.start();
  renderVoice();
}

// Stop listening without touching the text box again (it already holds what was heard so far).
function cancelVoice() {
  if (!S.voice) return;
  S.voice.onend = null;
  S.voice.onresult = null;
  S.voice.abort();
  S.voice = null;
  renderVoice();
}

function renderVoice() {
  if (!SpeechRecognition) return;
  const listening = Boolean(S.voice);
  $('#voice-btn').classList.toggle('is-listening', listening);
  $('#voice-btn').setAttribute('aria-pressed', String(listening));
  $('#voice-label').textContent = listening ? 'Listening… click to stop' : 'Speak';
  $('#voice-lang').disabled = listening;
}

// Re-run with the person's corrections from "What the agent understood" (no new message).
async function runRequest(override) {
  if (S.requestPending || S.submissionPending || !S.offer) return;
  const text = conversationForAgent().filter((m) => m.role === 'buyer').map((m) => m.text).join(' · ') || S.offer.request_text;
  const version = ++S.requestVersion;
  const editVersion = S.editVersion;
  startPending('Re-checking with your corrections…');
  try {
    const res = await api('/api/requests', { method: 'POST', body: { customer_id: S.session.customer_id, text, override } });
    if (version !== S.requestVersion) return;
    if (editVersion !== S.editVersion) {
      setStatus('The details changed while they were being checked. Re-run with these details.', true);
      return;
    }
    showOffer(res, { scroll: false });
    say(offerSummary(res.offer), { notice: res.notice });
  } catch (e) {
    if (version === S.requestVersion) setStatus(e.message, true);
  } finally {
    endPending(version);
  }
}

const selectedId = (i) => S.selections[i] ?? S.offer.items[i].recommended_product_id;
const selectedCand = (i) => S.offer.items[i].candidates.find((c) => c.product_id === selectedId(i));

function renderResults() {
  const host = $('#results');
  host.hidden = false;
  if (S.submitted) { // once ordered, only the order itself, in one row under the conversation
    host.replaceChildren(orderStrip());
    return;
  }
  host.replaceChildren(h('div', { class: 'results-grid' },
    h('div', { class: 'results-main' }, agentDetails(), S.offer.items.map((item, i) => itemPanel(item, i))),
    h('aside', { class: 'results-side', 'aria-label': 'Decision' }, decisionPanel())));
}

// ---------------------------------------------------------------------- agent details (collapsed)
// What the agent understood and the steps it ran are kept out of the buyer's way. They open by
// themselves when the request still needs a correction, or when the AI assistant was unavailable.
function agentDetails() {
  const p = S.offer.parsed;
  const fallback = p.parser === 'rules';
  const needsFix = fallback || p.delivery_date_assumed || p.clarification_required
    || p.items.some((it) => it.quantity == null || !it.unit || it.assumption);
  const summary = fallback ? 'Double-check what was understood: the AI assistant was unavailable'
    : needsFix ? 'Check what the agent understood: something is still missing'
      : 'Details: what the agent understood and how it worked';
  const details = h('details', { class: 'agent-details', id: 'agent-details', open: needsFix || S.detailsOpen },
    h('summary', { text: summary }),
    understoodPanel(), operationsPanel());
  details.addEventListener('toggle', () => { if (!needsFix) S.detailsOpen = details.open; });
  return details;
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
  const SHOW = 4; // the rest are behind "Show all N options"
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

// ---------------------------------------------------------------------- decision and placing the order
// The buyer chooses an option and places the order. Within the agent's authority it goes through; outside
// it, a person in the customer's organisation approves by typing their full name. Hard rules are never waived.
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

function reviewReasons(d) {
  return (d?.checks || []).filter((c) => !c.passed && c.severity === 'review');
}

const OUTCOMES = {
  auto_approve: { tone: 'good', icon: '✓', title: 'Within the agent’s authority', button: 'Place order' },
  needs_approval: { tone: 'warning', icon: '⚑', title: 'Needs a person’s approval', button: 'Approve and place order…' },
  reject: { tone: 'critical', icon: '✕', title: 'Cannot be placed', button: 'Cannot be placed' },
};
const SIMULATED = '(Simulated: the mock backend has no order endpoint.)';

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
    submitBlock(lines, out, d),
    h('p', { class: 'fine' }, `Offer ${o.id} · fixed prices until ${fmt.time(o.valid_until)} · `,
      h('span', { title: `SHA-256 ${o.content_hash}\nEd25519 ${o.signature}`, text: `signed ✓ ${o.content_hash.slice(0, 12)}…` })));
}

function currentCustomerName(id) {
  return S.customers.find((c) => c.id === id)?.name || id;
}

function submitBlock(lines, out, d) {
  const area = h('textarea', { id: 'rationale', rows: '3', 'aria-describedby': 'rationale-hint' });
  area.value = S.rationale ?? defaultRationale();
  area.addEventListener('input', () => { S.rationale = area.value; });
  const stateMessage = orderStateMessage();
  const blocked = !d || d.outcome === 'reject' || lines.length === 0 || Boolean(stateMessage);
  return h('div', { class: 'submit-block' },
    S.notice ? h('p', { class: 'offer-state', role: 'note', text: S.notice }) : null,
    stateMessage ? h('p', { class: 'offer-state', role: 'status', text: stateMessage }) : null,
    S.evaluationError ? h('p', { class: 'bad small', role: 'alert', text: S.evaluationError }) : null,
    S.submitError ? h('p', { class: 'bad small', role: 'alert', text: S.submitError }) : null,
    h('details', { class: 'rationale-details' },
      h('summary', { text: 'Rationale (saved to the audit trail)' }),
      area,
      h('span', { id: 'rationale-hint', class: 'hint muted', text: 'Written by the agent from the data above. Edit it if you chose differently.' })),
    h('button', {
      class: 'btn btn-primary', type: 'button', id: 'submit-order', disabled: blocked,
      text: S.submissionPending ? 'Placing…' : lines.length === 0 ? 'Add the missing details first' : out.button,
      onClick: () => (d?.outcome === 'needs_approval' ? openApproval() : submitOrder()),
    }));
}

// The placed (or cancelled) order in one horizontal row; the options and details are no longer shown.
function orderStrip() {
  const o = S.submitted;
  const status = {
    placed: ['good', '✓', 'Placed'],
    cancelled: ['neutral', '–', 'Cancelled'],
    rejected: ['critical', '✕', 'Not placed'],
  }[o.status] || ['critical', '✕', 'Not placed'];
  const decided = o.status === 'cancelled' ? (o.cancel_reason ? `Cancelled: “${o.cancel_reason}”` : 'Cancelled')
    : o.status === 'placed' ? (o.reviewer ? `Approved by ${o.reviewer}` : 'Within the agent’s authority')
      : o.decision.summary.replace(/^Blocked: /, '');
  const delivery = o.lines.length ? o.lines.map((l) => l.delivery_date).sort()[0] : null;
  const cancellable = o.status === 'placed' && o.cancellable_until && new Date(o.cancellable_until) > new Date();
  const cell = (label, ...content) => h('div', { class: 'strip-cell' }, h('span', { class: 'stat-label', text: label }), ...content);
  return h('section', { class: 'panel order-strip', id: 'decision', 'aria-label': `Order ${o.id}` },
    cell(`Order ${o.id}`, toneChip(...status)),
    h('div', { class: 'strip-cell strip-lines' }, h('span', { class: 'stat-label', text: 'Ordered' }),
      o.lines.length ? o.lines.map((l) => h('span', { class: 'cell-line', text: `${fmt.num(l.quantity, 2)} ${unitLabel(l.sales_unit, l.quantity)} · ${l.name}` }))
        : h('span', { text: o.request_text || '–' })),
    cell('Delivery', h('strong', { text: delivery ? fmt.day(delivery) : '–' })),
    cell('Total, excl. VAT', h('strong', { class: 'strip-total', text: fmt.eur(o.total) })),
    cell('Decided', h('span', { text: decided }),
      cancellable ? h('span', { class: 'muted small', text: `Can be cancelled until ${fmt.dateTime(o.cancellable_until)}` }) : null),
    h('div', { class: 'strip-actions' },
      h('a', { class: 'btn btn-small', href: '#orders', text: 'See my orders' }),
      cancellable ? h('button', { class: 'btn btn-small', type: 'button', text: 'Cancel order', onClick: () => cancelOrder(o, () => renderResults()) }) : null,
      h('button', { class: 'btn btn-small', type: 'button', text: 'New request', onClick: () => newRequest() })));
}

// Bring the conversation (and the order row under it) back into view.
function backToConversation() {
  $('#results').scrollIntoView({ behavior: 'smooth', block: 'end' });
  $('#request-text').focus({ preventScroll: true });
}

function rerenderDecision() {
  const old = $('#decision');
  if (old) old.replaceWith(S.submitted ? orderStrip() : decisionPanel());
}

// Place the selected options. `approver` is the full name typed in the approval dialog, when needed.
async function submitOrder(approver = '') {
  const blocked = orderStateMessage();
  if (!S.offer || S.submitted || S.submissionPending || !S.decision || blocked || S.decision.outcome === 'reject' || !orderLines().length) {
    if (blocked) { setStatus(blocked, true); if (S.offer) rerenderDecision(); updateOrderControls(); }
    return false;
  }
  S.submissionPending = true;
  S.submitError = null;
  rerenderDecision();
  updateOrderControls();
  try {
    S.submitted = await api('/api/orders', {
      method: 'POST',
      body: { offer_id: S.offer.id, selections: { ...S.selections }, rationale: S.rationale ?? defaultRationale(), approved_by: approver },
    });
    const o = S.submitted;
    if (o.status === 'placed') {
      say(`Order ${o.id} is placed (${fmt.eur(o.total)})${o.reviewer ? `, approved by ${o.reviewer}` : ''}. Need anything else? Just write it below.`,
        { action: 'orders' });
    }
    return true;
  } catch (e) {
    S.submitError = e.message;
    if (!approver) setStatus(e.message, true); // the approval dialog shows its own errors
    return false;
  } finally {
    S.submissionPending = false;
    if (S.submitted) {
      renderResults(); // the order row replaces the options and the decision panel
      backToConversation();
    } else {
      rerenderDecision();
    }
    updateOrderControls();
  }
}

async function confirmCancel() {
  const pending = S.pendingCancel;
  if (!pending) return;
  S.pendingCancel = null;
  renderThread();
  try {
    const updated = await api(`/api/orders/${pending.order.id}/cancel`, { method: 'POST', body: { reason: pending.reason } });
    if (S.submitted?.id === updated.id) { S.submitted = updated; rerenderDecision(); }
    say(`Done: order ${updated.id} is cancelled.`, { aside: true, action: 'orders' });
  } catch (e) {
    say(`I couldn’t cancel it: ${e.message}`, { aside: true });
  }
}

function keepOrder() {
  const pending = S.pendingCancel;
  S.pendingCancel = null;
  if (pending) say(`OK, order ${pending.order.id} stays as it is.`, { aside: true });
}

// Cancel a placed order (until the order cut-off of its delivery). `done` re-renders whatever showed it.
async function cancelOrder(order, done) {
  const reason = prompt(`Cancel order ${order.id} (${fmt.eur(order.total)})? You can give a reason (optional).`, '');
  if (reason === null) return; // the buyer clicked Cancel in the prompt
  try {
    const updated = await api(`/api/orders/${order.id}/cancel`, { method: 'POST', body: { reason: reason.trim() } });
    if (S.submitted?.id === updated.id) {
      S.submitted = updated;
      say(`Order ${updated.id} is cancelled.`, { aside: true });
    }
    done?.(updated);
  } catch (e) {
    alert(e.message);
  }
}

// ---------------------------------------------------------------------- approval by a named person
function openApproval() {
  const d = S.decision;
  if (!d || !S.offer || S.submitted || S.dirty || S.requestPending || S.submissionPending) return;
  $('#approve-summary').replaceChildren(
    h('div', { class: 'approve-total' }, h('span', { class: 'stat-label', text: 'Order total, excl. VAT' }), h('strong', { text: fmt.eur(d.total) })),
    h('ul', { class: 'approve-lines' }, orderLines().map(({ c }) => h('li', {},
      h('span', { text: `${fmt.num(c.quantity, 2)} ${unitLabel(c.sales_unit, c.quantity)} · ${c.name}` }), h('strong', { text: fmt.eur(c.line_total) })))),
    h('p', { class: 'label', text: 'Why it needs a person' }),
    h('ul', { class: 'checks' }, reviewReasons(d).map(checkItem)));
  $('#approve-form .signin-error').hidden = true;
  $('#approve-name').value = '';
  $('#approve-submit').disabled = false;
  if (!$('#approve-dialog').open) $('#approve-dialog').showModal();
  $('#approve-name').focus();
}

function declineApproval() {
  if ($('#approve-dialog').open) $('#approve-dialog').close();
}

function initApproval() {
  const dialog = $('#approve-dialog');
  $('#approve-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('#approve-name').value.trim().replace(/\s+/g, ' ');
    const error = $('#approve-form .signin-error');
    if (!/^\S+ \S+/.test(name)) {
      error.textContent = 'Type your first and last name to approve.';
      error.hidden = false;
      $('#approve-name').focus();
      return;
    }
    error.hidden = true;
    $('#approve-submit').disabled = true;
    const ok = await submitOrder(name);
    $('#approve-submit').disabled = false;
    if (ok) dialog.close();
    else { error.textContent = S.submitError || 'The order could not be placed.'; error.hidden = false; }
  });
  $('#approve-cancel').addEventListener('click', declineApproval);
  $('#approve-close').addEventListener('click', declineApproval);
  dialog.addEventListener('cancel', (e) => { e.preventDefault(); declineApproval(); }); // the Esc key
}

function newRequest({ focus = true } = {}) {
  if (S.submissionPending) return;
  cancelVoice();
  if ($('#approve-dialog').open) $('#approve-dialog').close();
  S.requestVersion += 1;
  S.evaluationVersion += 1;
  Object.assign(S, { offer: null, decision: null, edit: null, submitted: null, selections: {}, sort: {}, rationale: null, dirty: false, requestChanged: false, requestPending: false, evaluationPending: false, evaluationError: null, submitError: null });
  S.chat = { messages: [], understood: null, from: 0 };
  S.pendingCancel = null;
  S.notice = null;
  $('#results').hidden = true;
  $('#results').classList.remove('is-refreshing');
  $('#results').replaceChildren();
  $('#request-text').value = '';
  renderThread();
  setStatus('');
  updateOrderControls();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (focus) $('#request-text').focus();
}

// ====================================================================== my orders (buyer portal)
function orderStatusChip(status) {
  if (status === 'placed') return toneChip('good', '✓', 'Placed');
  if (status === 'pending_approval') return toneChip('warning', '⚑', 'Waiting for approval');
  if (status === 'cancelled') return toneChip('neutral', '–', 'Cancelled');
  return toneChip('critical', '✕', 'Not placed');
}

function decidedBy(o) {
  if (o.status === 'cancelled') return `Cancelled ${fmt.dateTime(o.cancelled_at)}${o.cancel_reason ? `: “${o.cancel_reason}”` : ''}`;
  if (o.reviewer) return `Approved by ${o.reviewer}`;
  if (o.status === 'pending_approval') return 'Waiting for approval';
  if (o.status === 'placed') return 'Within the agent’s authority';
  return o.decision.summary;
}

async function loadMyOrders() {
  const host = $('#my-orders');
  let orders;
  try { orders = await api('/api/orders'); } catch (e) { host.replaceChildren(h('p', { class: 'empty', text: e.message })); return; }
  if (!orders.length) {
    host.replaceChildren(h('p', { class: 'empty', text: 'No orders yet. Describe what you need on the Order page.' }));
    return;
  }
  host.replaceChildren(h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, ['Order', 'Status', 'What', 'Decided by', 'Total', ''].map((t, k) => h('th', { class: k === 4 ? 'num' : null, text: t })))),
    h('tbody', {}, orders.map((o) => h('tr', {},
      h('td', {}, o.id, h('span', { class: 'cell-sub', text: fmt.dateTime(o.created_at) })),
      h('td', {}, orderStatusChip(o.status)),
      h('td', {}, o.lines.length
        ? o.lines.map((l) => h('span', { class: 'cell-line', text: `${fmt.num(l.quantity, 2)} ${unitLabel(l.sales_unit, l.quantity)} · ${l.name}` }))
        : o.request_text || '–'),
      h('td', { text: decidedBy(o) }),
      h('td', { class: 'num', text: fmt.eur(o.total) }),
      h('td', {}, o.status === 'placed' && o.cancellable_until && new Date(o.cancellable_until) > new Date()
        ? h('button', { class: 'btn btn-small', type: 'button', text: 'Cancel', title: `Possible until ${fmt.dateTime(o.cancellable_until)}`,
          onClick: () => cancelOrder(o, () => loadMyOrders()) })
        : null)))))));
}

// ====================================================================== explore page
async function runExplore() {
  S.exploreLoaded = true;
  const q = $('#explore-q').value.trim();
  const body = $('#explore-body');
  if (q.length < 2) { body.replaceChildren(h('p', { class: 'empty', text: 'Type at least two letters.' })); return; }
  body.classList.add('is-refreshing');
  try {
    const params = new URLSearchParams({ q, filters: [...S.explore.filters].join(','), category: S.explore.category });
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
  const carbonGroups = res.groups.filter((g) => g.co2 != null)
    .sort((a, b) => b.co2 - a.co2).slice(0, 12);
  const reliabilityGroups = res.groups.filter((g) => g.reliability != null)
    .sort((a, b) => a.reliability - b.reliability).slice(0, 12);
  const carbonChart = carbonGroups.length ? barList(carbonGroups.map((g) => ({
    id: g.category,
    label: `Product group ${g.category}`,
    sublabel: `${fmt.num(g.count)} ${plural('product', g.count)} · avg. delivery reliability ${fmt.pct(g.reliability)}`,
    value: g.co2,
    valueText: `${fmt.num(g.co2, 2)} kg CO₂e/kg`,
    selected: g.category === S.explore.category,
    tooltip: [['Product group', g.category], ['Products here', fmt.num(g.count)], ['Avg kg CO₂e per kg', fmt.num(g.co2, 3)], ['Avg delivery reliability', fmt.pct(g.reliability)]],
  })), {
    ariaLabel: 'Aggregated carbon footprint by product group',
    onSelect: (cat) => { S.explore.category = S.explore.category === cat ? '' : cat; runExplore(); },
  }) : h('p', { class: 'empty', text: 'No carbon-footprint data for these product groups.' });
  const reliabilityChart = reliabilityGroups.length ? barList(reliabilityGroups.map((g) => ({
    id: g.category,
    label: `Product group ${g.category}`,
    sublabel: `${fmt.num(g.count)} ${plural('product', g.count)}`,
    value: g.reliability,
    valueText: fmt.pct(g.reliability),
    selected: g.category === S.explore.category,
    tooltip: [['Product group', g.category], ['Products here', fmt.num(g.count)], ['Avg delivery reliability', fmt.pct(g.reliability)]],
  })), {
    ariaLabel: 'Aggregated delivery reliability by product group',
    onSelect: (cat) => { S.explore.category = S.explore.category === cat ? '' : cat; runExplore(); },
  }) : h('p', { class: 'empty', text: 'No delivery-reliability data for these product groups.' });

  $('#explore-body').replaceChildren(
    h('div', { class: 'kpi-row' },
      kpi('Products in view', fmt.num(k.total), 'matches the selected search and filters'),
      kpi('With carbon data', fmt.num(k.with_co2), k.total ? `${fmt.pct(k.with_co2 / k.total)} of results` : null),
      kpi('Avg carbon footprint', k.avg_co2 == null ? 'n/a' : `${fmt.num(k.avg_co2, 2)} kg CO₂e/kg`),
      kpi('With delivery data', fmt.num(k.with_reliability), k.total ? `${fmt.pct(k.with_reliability / k.total)} of results` : null),
      kpi('Avg delivery reliability', fmt.pct(k.avg_reliability), 'share of ordered quantity delivered'),
      kpi('Product groups', fmt.num(res.groups_total), S.explore.category ? `filtered to group ${S.explore.category}` : 'aggregate view')),
    h('div', { class: 'explore-chart-grid' },
      h('section', { class: 'panel item-panel' },
        h('div', { class: 'panel-head' }, h('h3', { text: 'Carbon footprint by group' }),
          h('span', { class: 'muted small', text: 'Average kg CO₂e per kg' })),
        carbonChart,
        h('p', { class: 'chart-caption', text: `Highest averages among ${fmt.num(carbonGroups.length)} groups shown. Select a group to filter the metrics.` })),
      h('section', { class: 'panel item-panel' },
        h('div', { class: 'panel-head' }, h('h3', { text: 'Delivery reliability by group' }),
          h('span', { class: 'muted small', text: 'Share of ordered quantity delivered' })),
        reliabilityChart,
        h('p', { class: 'chart-caption', text: `Lowest averages among ${fmt.num(reliabilityGroups.length)} groups shown. Select a group to filter the metrics.` }))));
}

// ====================================================================== sign-in
function renderSignIn(message = '') {
  const s = S.session || {};
  const customers = s.customers || [];
  const sel = $('#signin-customer');
  const keep = sel.value || storage.get('aimo.customer', 'C-1001');
  sel.replaceChildren(...customers.map((c) => h('option', { value: c.id, text: c.name })));
  if (customers.some((c) => c.id === keep)) sel.value = keep;
  document.querySelectorAll('.signin-password').forEach((el) => { el.hidden = s.password_required === false; });
  const note = message || (s.password_required === false ? 'Local development: no password is set on this server, so signing in needs none.' : '');
  $('#signin-status').textContent = note;
  $('#signin-status').hidden = !note;
}

function showSignIn(message = '') {
  S.session = { ...(S.session || {}), signed_in: false, role: null };
  for (const p of PAGES) $(`#page-${p}`).hidden = true;
  $('.main-nav').hidden = true;
  $('#session-box').hidden = true;
  $('#page-signin').hidden = false;
  hideTip();
  renderSignIn(message);
  api('/api/session').then((s) => { S.session = s; renderSignIn(message); }).catch(() => { /* keep the form */ });
}

async function signIn(form, body) {
  const error = form.querySelector('.signin-error');
  const button = form.querySelector('button[type=submit]');
  error.hidden = true;
  button.disabled = true;
  try {
    await api('/api/session/login', { method: 'POST', body });
    history.replaceState(null, '', location.pathname); // start on the portal's first page
    location.reload();
  } catch (e) {
    error.textContent = e.message;
    error.hidden = false;
    button.disabled = false;
  }
}

function initSignIn() {
  $('#signin-buyer').addEventListener('submit', (e) => {
    e.preventDefault();
    const customer = $('#signin-customer').value;
    storage.set('aimo.customer', customer);
    signIn(e.target, { customer_id: customer, password: $('#signin-buyer-password').value });
  });
  $('#sign-out').addEventListener('click', async () => {
    try { await api('/api/session/logout', { method: 'POST' }); } catch { /* reload either way */ }
    history.replaceState(null, '', location.pathname);
    location.reload();
  });
}

// ====================================================================== boot
function showPortal() {
  const s = S.session;
  document.body.dataset.role = s.role;
  $('#session-who').replaceChildren(h('strong', { text: s.name }),
    h('span', { class: 'muted', text: ' · buyer portal' }));
  $('#session-box').hidden = false;
  document.querySelectorAll('.main-nav a').forEach((a) => { a.hidden = !a.dataset.roles.split(' ').includes(s.role); });
  $('.main-nav').hidden = false;
}

async function init() {
  initSignIn();
  try { S.session = await api('/api/session'); } catch { S.session = { signed_in: false }; }
  if (!S.session.signed_in) { showSignIn(); return; }
  showPortal();
  try { S.vocab = await api('/api/vocab'); } catch { /* keep defaults */ }
  try { S.customers = await api('/api/customers'); } catch (e) { setStatus(e.message, true); }
  $('#explore-form').addEventListener('submit', (e) => { e.preventDefault(); S.explore.category = ''; runExplore(); });

  renderExamples();
  initVoice();
  initApproval();
  $('#request-text').addEventListener('input', () => markDirty(true));
  $('#request-form').addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  $('#request-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendMessage(); });
  updateOrderControls();
  window.addEventListener('hashchange', route);
  window.addEventListener('scroll', hideTip, { passive: true });
  route();
}

init();
