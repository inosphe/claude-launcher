/* Real composer module: append (not replace), one copy per meaning, folded
   included terms, the closed mode keeping focus in the field, and the
   per-session visibility store. No browser needed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {}; this.textContent = '';
    this.dataset = {}; this.hidden = false; this.className = ''; this.listeners = {};
    this.classList = {add: (...names) => {
      const set = new Set(this.className.split(' ').filter(Boolean));
      for (const name of names) set.add(name);
      this.className = [...set].join(' ');
    }};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelectorAll(selector) {
    return all(this).filter(n => selector === '[data-gc-mode]'
      ? n.dataset.gcMode !== undefined : selector.split(',').some(s => n.className.split(' ').includes(s.trim().replace(/^\./, ''))));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const all = (node, out = []) => {
  for (const child of node.children || []) {
    if (typeof child !== 'object') continue;
    out.push(child); all(child, out);
  }
  return out;
};
const textOf = node => (node.textContent || '') + (node.children || []).map(c => typeof c === 'object' ? textOf(c) : String(c)).join('');

let focused = null;
class Field extends Element {
  constructor() { super('textarea'); }
  dispatchEvent(ev) { this.listeners[ev.type]?.(ev); return true; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  setRangeText(text, start, end) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    const at = start + text.length; this.setSelectionRange(at, at);
  }
  focus() { focused = this; }
}

const TERMS = [
  {id: 'explain', term: '자세히 · 구체적 설명', definition: '일의 목적과 동기, 실행 방법을 구체적으로 설명합니다.', aliases: ['자세히', '상세히'], reference: '[[term:#explain]]'},
  {id: 'research', term: '자세히 · 추가 탐색', definition: '현재 모르는 일에 대해 브라우저 검색을 포함해 추가 탐색합니다.', aliases: ['자세히'], reference: '[[term:#research]]'},
];
function analyze(text) {
  const references = [...text.matchAll(/\[\[term:([^\]]+)\]\]/g)].map(m => {
    const row = TERMS.find(t => '#' + t.id === m[1]);
    return {literal: m[0], start: Array.from(text.slice(0, m.index)).length,
      end: Array.from(text.slice(0, m.index + m[0].length)).length,
      status: row ? 'resolved' : 'ambiguous', ...(row ? {term: row} : {candidates: TERMS})};
  });
  const mentions = [...text.matchAll(/자세히/g)]
    .filter(m => !text.slice(0, m.index).endsWith('[[term:'))
    .map(m => ({literal: '자세히', start: Array.from(text.slice(0, m.index)).length,
      end: Array.from(text.slice(0, m.index + 3)).length, candidates: TERMS}));
  return {text, references, mentions, workspace: 'claunch'};
}
async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const body = opts.body ? JSON.parse(opts.body) : null;
  if (!url.pathname.endsWith('/glossary')) throw Error('unexpected request ' + raw);
  if (!body) return {ok: true, json: async () => ({terms: TERMS, workspace: 'claunch'})};
  if (body.action === 'analyze') return {ok: true, json: async () => analyze(body.text)};
  if (body.action === 'compose') return {ok: true, json: async () => ({ready: true, delivery: body.text})};
  throw Error('unexpected body ' + JSON.stringify(body));
}

const store = new Map();
const localStorage = {
  getItem: key => store.has(key) ? store.get(key) : null,
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: key => store.delete(key),
};
const field = new Field();
field.parentElement = new Element('form');
const trigger = new Element('button');
const form = field.parentElement;
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag),
    getElementById: id => id === 'term-glossary-open' ? trigger : null},
  api, localStorage, Event: class { constructor(type, opts) { this.type = type; this.bubbles = !!(opts || {}).bubbles; } },
  setTimeout, clearTimeout, console,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/glossary-composer.js'), 'utf8'), ctx);
const composer = ctx.GlossaryComposer;
const KEY = 'claunch.glossary.visibility.';
let currentName = 's1';
const panel = () => all(form).find(n => n.tag === 'section');
const buttons = () => all(form).filter(n => n.tag === 'button');
const labelled = label => buttons().find(b => textOf(b) === label);
const choice = needle => buttons().find(b => b.className.split(' ').includes('gc-choice') && textOf(b).includes(needle));
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setTimeout(resolve, 25)); };
const type = value => { field.value = value; field.setSelectionRange(value.length, value.length); composer.refresh(); };
const switchTo = name => { currentName = name; composer.refresh(); };

(async () => {
  composer.mount({field, session: () => currentName, resize: () => {}});
  assert(panel().hidden, 'mount leaves the panel hidden');

  // A mention appends at the end with a newline; the sentence itself stays.
  type('😀 자세히 설명해주세요');
  await settle();
  const mention = labelled('자세히 · 의미 2개');
  assert(mention, 'detected mention');
  mention.onclick();
  const picked = choice('추가 탐색');
  assert(picked, 'meaning choice listed');
  picked.onclick();
  assert.equal(field.value, '😀 자세히 설명해주세요\n[[term:#research]]');
  assert.equal(focused, field, 'a choice leaves focus in the field');

  // The included term is marked and its long definition stays folded.
  await settle();
  const included = all(panel()).find(n => n.tag === 'details' && n.className.split(' ').includes('gc-included'));
  assert(included, 'included term listed');
  assert(textOf(included).includes('포함됨'), 'inclusion is explicit');
  assert(textOf(included).includes(TERMS[1].definition), 'definition present but folded');
  assert(!included.attributes.open, 'definition collapsed by default');
  assert(panel().querySelectorAll('[data-gc-mode]').length === 3, 'three visibility modes offered');
  assert.equal(panel().querySelectorAll('[data-gc-mode]').find(b => b.dataset.gcMode === 'auto').attributes['aria-pressed'], 'true');

  // The same meaning is appended once.
  labelled('자세히 · 의미 2개').onclick();
  const again = choice('추가 탐색');
  assert(textOf(again).includes('이미 포함됨'));
  again.onclick();
  assert.equal(field.value, '😀 자세히 설명해주세요\n[[term:#research]]');

  // Closing stores the closed mode, returns focus, and typing does not reopen.
  labelled('닫기').onclick();
  assert(panel().hidden, 'close hides the panel');
  assert.equal(focused, field, 'close hands focus back to the field');
  assert.equal(store.get(KEY + 's1'), 'closed');
  type('😀 자세히');
  await settle();
  assert(panel().hidden, 'closed mode does not reopen while typing');
  assert.equal(store.get(KEY + 's1'), 'closed');

  // The visibility is per session: another session starts in auto.
  switchTo('s2'); type('자세히'); await settle();
  assert(!panel().hidden, 'the next session opens in auto');
  labelled('닫기').onclick();
  assert.equal(store.get(KEY + 's2'), 'closed');
  switchTo('s3'); type('자세히'); await settle();
  assert(!panel().hidden, 'stored mode is keyed by session');

  // An explicit [[term:...]] being completed moves to the end instead of
  // leaving an unfinished marker behind.
  type('[[term:자세'); await settle();
  choice('구체적 설명').onclick();
  assert.equal(field.value, '[[term:#explain]]');

  // The button opens the panel explicitly and keeps it open on an empty line.
  switchTo('s4'); type(''); await settle();
  trigger.onclick();
  await settle();
  assert.equal(store.get(KEY + 's4'), 'open');
  assert(!panel().hidden, 'the button opens the panel');
  type(''); await settle();
  assert(!panel().hidden, 'open mode survives an empty field');
  assert(labelled('자동'), 'mode control is still reachable');

  // Back to auto from the control.
  labelled('자동').onclick(); await settle();
  assert.equal(store.get(KEY + 's4'), 'auto');
  type(''); await settle();
  assert(panel().hidden, 'auto with nothing to show hides the panel');

  console.log('glossary composer: append, one copy per meaning, folded included terms, closed focus, per-session visibility passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
