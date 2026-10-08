/* In-place glossary assistance. Detection never changes text without a click. */
globalThis.GlossaryComposer = (() => {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  let field, panel, session, resize, ticket = 0, timer, composing = false;
  let snapshot = '', owner = '', choices = null;
  const endpoint = name => `/api/sessions/${encodeURIComponent(name)}/glossary`;
  const offset = (text, at) => Array.from(text).slice(0, at).join('').length;
  async function request(name, body, query) {
    const response = await api(endpoint(name) + (query == null ? '' : '?q=' + encodeURIComponent(query)),
      body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  function button(text, action, cls = '') {
    const b = node('button', text, 'gc-button ' + cls); b.type = 'button';
    b.onclick = action; return b;
  }
  function reset() {
    ++ticket; clearTimeout(timer); choices = null;
    if (panel) { panel.hidden = true; panel.replaceChildren(); }
  }
  function current() { return field && !field.disabled && field.value === snapshot && session() === owner; }
  function replace(start, end, reference) {
    if (!current()) { reset(); return; }
    field.setRangeText(reference, start, end, 'end');
    field.focus(); resize(field);
    field.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function heading(workspace, subtitle) {
    panel.replaceChildren(); panel.hidden = false;
    const head = node('div', null, 'gc-head');
    head.append(node('strong', `용어집 · ${workspace}`), button('닫기', reset, 'gc-close'));
    panel.append(head, node('p', subtitle, 'gc-hint'));
  }
  function showChoices(items, start, end, title, workspace) {
    if (!current()) return;
    heading(workspace, title + ' — 전달할 의미를 선택하세요.');
    choices = {start, end};
    const list = node('div', null, 'gc-choices');
    for (const row of items) {
      const b = button(null, () => replace(start, end, row.reference), 'gc-choice');
      b.append(node('strong', row.term), node('span', row.definition), node('small', '이 의미로 바꾸기 →'));
      list.append(b);
    }
    if (!items.length) list.append(node('p', '일치하는 용어가 없습니다. 용어집에서 이름과 별칭을 확인하세요.', 'gc-hint'));
    panel.append(list);
  }
  async function update() {
    if (composing || !field || field.disabled) return;
    const seq = ++ticket, name = session(), text = field.value;
    if (!name || !text.trim()) { reset(); return; }
    snapshot = text; owner = name; choices = null;
    const caret = field.selectionStart;
    const prefix = text.slice(0, caret);
    const match = /(^|[^\\])\[\[term:([^\[\]\n]*)$/.exec(prefix);
    try {
      if (match) {
        const query = match[2].trim();
        if (query.length > 200) return;
        const result = await request(name, null, query);
        if (seq !== ticket || !current()) return;
        const start = caret - match[2].length - 7;
        const tail = text.slice(caret).match(/^[^\[\]\n]*\]\]/);
        showChoices(result.terms, start, caret + (tail ? tail[0].length : 0), query || '용어 검색', result.workspace);
      } else {
        const result = await request(name, {text, action: 'analyze'});
        if (seq !== ticket || !current()) return;
        const unresolved = result.references.filter(r => r.status !== 'resolved');
        if (!result.mentions.length && !result.references.length) { reset(); return; }
        heading(result.workspace, '감지된 표현을 선택하면 의미를 확인하고 참조로 바꿀 수 있습니다.');
        const mentions = node('div', null, 'gc-mentions');
        for (const ref of [...unresolved, ...result.mentions]) {
          const count = ref.candidates.length;
          mentions.append(button(`${ref.literal} · ${count ? '의미 ' + count + '개' : '찾을 수 없음'}`,
            () => showChoices(ref.candidates, offset(text, ref.start), offset(text, ref.end), ref.literal, result.workspace)));
        }
        panel.append(mentions);
        for (const ref of result.references.filter(r => r.status === 'resolved')) {
          const row = node('div', null, 'gc-resolved');
          row.append(node('strong', '전달할 의미 · ' + ref.term.term), node('span', ref.term.definition));
          panel.append(row);
        }
      }
    } catch (error) {
      if (seq !== ticket || !current()) return;
      if (text.includes('[[term:')) {
        heading('조회 실패', error.message);
      } else { reset(); } // No workspace: ordinary typing continues normally.
    }
  }
  function schedule() {
    ++ticket; clearTimeout(timer);
    if (panel) panel.hidden = true;
    timer = setTimeout(update, 160);
  }
  function insertReference() {
    if (!field || field.disabled || !session()) return;
    const start = field.selectionStart, end = field.selectionEnd;
    const selected = field.value.slice(start, end);
    field.setRangeText('[[term:' + selected + ']]', start, end, 'end');
    field.setSelectionRange(start + 7 + selected.length, start + 7 + selected.length);
    field.focus(); resize(field); schedule();
  }
  function mount(options) {
    field = options.field; session = options.session; resize = options.resize;
    if (!field) return;
    panel = node('section', null, 'glossary-composer'); panel.hidden = true;
    panel.setAttribute('aria-label', '입력 중인 메시지의 용어집');
    field.parentElement.append(panel);
    const trigger = document.getElementById('term-glossary-open');
    if (trigger) trigger.onclick = insertReference;
    field.addEventListener('input', schedule);
    field.addEventListener('click', schedule);
    field.addEventListener('compositionstart', () => { composing = true; reset(); });
    field.addEventListener('compositionend', () => { composing = false; schedule(); });
    field.addEventListener('keydown', ev => {
      if (ev.isComposing || ev.keyCode === 229 || panel.hidden) return;
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopImmediatePropagation(); reset(); }
      if (ev.key === 'ArrowDown' && choices) {
        ev.preventDefault(); ev.stopImmediatePropagation(); panel.querySelector('.gc-choice')?.focus();
      }
    }, true);
  }
  async function prepare(name, text) {
    // Escaped markers are literal text, as in the backend parser.
    if (!/(^|[^\\])\[\[term:/.test(text)) return text;
    const result = await request(name, {text, action: 'compose'});
    if (!result.ready) throw Error(result.error);
    return result.delivery;
  }
  return {mount, reset, prepare, refresh: schedule};
})();
