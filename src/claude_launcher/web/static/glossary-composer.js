/* In-place glossary assistance. Detection never changes text without a click. */
globalThis.GlossaryComposer = (() => {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  /* Panel visibility is per session and survives a reload: `auto` opens it when
     a term is detected (the default), `open` keeps it on screen, and `closed`
     never opens it on its own — the 용어집 button is the way back in. */
  const STORE = 'claunch.glossary.visibility.';
  const MODES = [['auto', '자동'], ['open', '열림'], ['closed', '닫힘']];
  let field, panel, session, resize, ticket = 0, timer, composing = false;
  let snapshot = '', owner = '', choices = null;
  let mode = 'auto', modeOf = null;
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
  function storedMode(name) {
    try {
      const value = globalThis.localStorage?.getItem(STORE + name);
      return value === 'open' || value === 'closed' ? value : 'auto';
    } catch (error) { return 'auto'; }
  }
  function storeMode(name, value) {
    try { globalThis.localStorage?.setItem(STORE + name, value); } catch (error) { /* private mode */ }
  }
  function syncMode(name) {
    if (name !== modeOf) { modeOf = name; mode = name ? storedMode(name) : 'auto'; }
    return mode;
  }
  function setMode(value) {
    mode = value; modeOf = session();
    if (modeOf) storeMode(modeOf, value);
    if (panel) for (const b of panel.querySelectorAll('[data-gc-mode]'))
      b.setAttribute('aria-pressed', String(b.dataset.gcMode === value));
  }
  function hide() {
    ++ticket; clearTimeout(timer); choices = null;
    if (panel) { panel.hidden = true; panel.replaceChildren(); }
  }
  /* Programmatic dismissal (a sent line, a session switch): the stored mode
     stays as the reader left it. */
  function reset() { hide(); }
  /* The reader's own close: remember it and hand focus back to the field. The
     panel is removed before focus is set, so the click cannot leave the focus
     on a button that no longer exists. */
  function close() {
    setMode('closed'); hide();
    if (field && !field.disabled) field.focus();
  }
  function current() { return field && !field.disabled && field.value === snapshot && session() === owner; }
  /* A term selected here is appended to the end of the line with a newline in
     front of it — the sentence being written stays where it is. An explicit
     [[term:...]] the reader was completing is the same term, so it moves to
     the end instead of staying as a second copy. */
  function append(reference, range) {
    if (!current()) { reset(); return; }
    let value = field.value;
    const fragment = range ? value.slice(range.start, range.end) : '';
    if (fragment.startsWith('[[term:')) value = value.slice(0, range.start) + value.slice(range.end);
    if (!value.includes(reference)) {
      const gap = value === '' || value.endsWith('\n') ? '' : '\n';
      value += gap + reference;
    }
    field.value = value;
    const end = value.length;
    field.setSelectionRange(end, end);
    field.focus(); resize(field);
    field.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function modeControls() {
    const wrap = node('div', null, 'gc-modes');
    for (const [value, label] of MODES) {
      const b = button(label, () => chooseMode(value), 'gc-mode');
      b.dataset.gcMode = value;
      b.setAttribute('aria-pressed', String(syncMode(session()) === value));
      wrap.append(b);
    }
    return wrap;
  }
  function chooseMode(value) {
    setMode(value);
    if (value === 'closed') { hide(); if (field && !field.disabled) field.focus(); return; }
    schedule();
  }
  function heading(workspace, subtitle) {
    panel.replaceChildren(); panel.hidden = false;
    const head = node('div', null, 'gc-head');
    head.append(node('strong', '용어집' + (workspace ? ` · ${workspace}` : '')),
      modeControls(), button('닫기', close, 'gc-close'));
    panel.append(head, node('p', subtitle, 'gc-hint'));
  }
  /* Terms already in the line: the inclusion is explicit (a badge and the
     summary), while the long definition stays folded until it is asked for. */
  function renderIncluded(rows) {
    if (!rows.length) return;
    const list = node('div', null, 'gc-included-list');
    list.append(node('h3', '포함된 용어', 'gc-included-title'));
    for (const ref of rows) {
      const box = node('details', null, 'gc-included');
      const summary = node('summary');
      summary.append(node('strong', ref.term.term), node('span', '포함됨', 'gc-badge'));
      box.append(summary, node('p', ref.term.definition, 'gc-definition'));
      list.append(box);
    }
    panel.append(list);
  }
  function outside(value, range) {
    return range && value.slice(range.start, range.end).startsWith('[[term:')
      ? value.slice(0, range.start) + value.slice(range.end) : value;
  }
  function showChoices(items, start, end, title, workspace) {
    if (!current()) return;
    heading(workspace, title + ' — 전달할 의미를 선택하세요.');
    choices = {start, end};
    const list = node('div', null, 'gc-choices');
    const rest = outside(field.value, choices);
    for (const row of items) {
      const already = rest.includes(row.reference);
      const b = button(null, () => append(row.reference, choices), 'gc-choice');
      b.append(node('strong', row.term), node('span', row.definition),
        node('small', already ? '이미 포함됨' : '입력 끝에 덧붙이기 →'));
      if (already) b.classList.add('included');
      list.append(b);
    }
    if (!items.length) list.append(node('p', '일치하는 용어가 없습니다. 용어집에서 이름과 별칭을 확인하세요.', 'gc-hint'));
    panel.append(list);
  }
  function showOpen(workspace) {
    if (mode !== 'open') return;
    heading(workspace || '', '[[term: 이름]]을 입력하거나 용어집에서 검색하면 의미를 고를 수 있습니다.');
  }
  async function update() {
    if (composing || !field || field.disabled) return;
    const seq = ++ticket, name = session(), text = field.value;
    syncMode(name);
    if (!name || mode === 'closed') { hide(); return; }
    if (!text.trim()) { if (mode === 'open') showOpen(); else hide(); return; }
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
        const included = result.references.filter(r => r.status === 'resolved' && r.term);
        if (!result.mentions.length && !result.references.length) {
          if (mode === 'open') showOpen(result.workspace); else hide();
          return;
        }
        heading(result.workspace, '감지된 표현을 선택하면 의미를 확인하고 입력 끝에 참조로 덧붙일 수 있습니다.');
        const mentions = node('div', null, 'gc-mentions');
        for (const ref of [...unresolved, ...result.mentions]) {
          const count = ref.candidates.length;
          mentions.append(button(`${ref.literal} · ${count ? '의미 ' + count + '개' : '찾을 수 없음'}`,
            () => showChoices(ref.candidates, offset(text, ref.start), offset(text, ref.end), ref.literal, result.workspace)));
        }
        panel.append(mentions);
        renderIncluded(included);
      }
    } catch (error) {
      if (seq !== ticket || !current()) return;
      if (text.includes('[[term:')) {
        heading('조회 실패', error.message);
      } else if (mode === 'open') {
        heading('', error.message);
      } else { hide(); } // No workspace: ordinary typing continues normally.
    }
  }
  function schedule() {
    ++ticket; clearTimeout(timer);
    syncMode(session());
    if (panel && mode !== 'open') panel.hidden = true;
    timer = setTimeout(update, 160);
  }
  function insertReference() {
    if (!field || field.disabled || !session()) return;
    setMode('open');
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
    panel.addEventListener('keydown', ev => {
      if (ev.key === 'Escape') { ev.preventDefault(); close(); }
    });
    const trigger = document.getElementById('term-glossary-open');
    if (trigger) trigger.onclick = insertReference;
    field.addEventListener('input', schedule);
    field.addEventListener('click', schedule);
    field.addEventListener('compositionstart', () => { composing = true; hide(); });
    field.addEventListener('compositionend', () => { composing = false; schedule(); });
    field.addEventListener('keydown', ev => {
      if (ev.isComposing || ev.keyCode === 229 || panel.hidden) return;
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopImmediatePropagation(); close(); }
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
