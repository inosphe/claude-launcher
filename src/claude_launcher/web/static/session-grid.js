/* Browser-local grid placement for the session rail's Grid view.

   The list view orders sessions by lineage and state, so a session's place in
   it moves whenever anything above it changes. People find things by where
   they left them, so the grid gives every session a fixed (row, column) cell
   that only an explicit move changes: a session leaving (5,3) leaves (5,3)
   empty, and (5,4) stays where it is.

   Rows carry a stable id and a human-editable name (alpha, bravo, ... by
   default); the name is the row's grouping label and is unique among rows.
   A row holds any number of cells; a cell holds a session name or nothing.
   A cell whose session is not in the current view (archived, filtered out,
   cleared) keeps its assignment -- the placement survives the session going
   out of view and coming back -- until something is moved onto it.

   One row is the default row: every new session lands there. A row may carry
   a condition (a mesh, a workspace, or both); organize() moves the sessions
   sitting in the default row to the first row, in row order, whose condition
   they meet. Nothing outside the default row is ever moved by it. */
globalThis.SessionGridLayout = class SessionGridLayout {
  static ROW_NAMES = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu",
  ];

  constructor(storage, key) {
    this.storage = storage;
    this.key = key;
    this.rows = [];
    this.nextId = 1;
    this.defaultId = null;
    try {
      const saved = JSON.parse(storage.getItem(key) || "null");
      if (saved && Array.isArray(saved.rows)) this.load(saved);
    } catch {}
    if (!this.rows.length) this.addRow();
    if (!this.row(this.defaultId)) this.defaultId = this.rows[0].id;
    delete this.row(this.defaultId).rule;
  }

  /* Takes what storage held, dropping whatever does not parse: a row without
     an id or a usable name, a duplicate row name, and a session placed twice
     (the first cell keeps it). */
  load(saved) {
    const seenNames = new Set();
    const seenIds = new Set();
    const placed = new Set();
    for (const row of saved.rows) {
      if (!row || typeof row.id !== "string" || !row.id || seenIds.has(row.id)) continue;
      const name = SessionGridLayout.cleanName(row.name);
      if (!name || seenNames.has(name.toLowerCase())) continue;
      const cells = [];
      for (const cell of Array.isArray(row.cells) ? row.cells : []) {
        if (typeof cell === "string" && cell && !placed.has(cell)) {
          placed.add(cell);
          cells.push(cell);
        } else {
          cells.push(null);
        }
      }
      seenIds.add(row.id);
      seenNames.add(name.toLowerCase());
      const entry = { id: row.id, name, cells };
      const rule = SessionGridLayout.cleanRule(row.rule);
      if (rule) entry.rule = rule;
      this.rows.push(entry);
      SessionGridLayout.trim(this.rows[this.rows.length - 1]);
    }
    if (typeof saved.defaultId === "string") this.defaultId = saved.defaultId;
    const n = Number(saved.nextId);
    this.nextId = Number.isSafeInteger(n) && n > 0 ? n : 1;
    for (const id of seenIds) {
      const m = /^r(\d+)$/.exec(id);
      if (m) this.nextId = Math.max(this.nextId, Number(m[1]) + 1);
    }
  }

  static cleanName(name) {
    return typeof name === "string" ? name.trim().replace(/\s+/g, " ").slice(0, 40) : "";
  }

  /* A row's condition: which mesh, which workspace, or both (both must then
     hold). Anything else, or neither, is no condition at all. */
  static cleanRule(rule) {
    if (!rule || typeof rule !== "object") return null;
    const out = {};
    for (const key of ["mesh", "workspace"]) {
      if (typeof rule[key] === "string" && rule[key].trim()) out[key] = rule[key].trim();
    }
    return Object.keys(out).length ? out : null;
  }

  /* Trailing empty cells carry no position, so they are not stored; interior
     ones are the holes that keep the cells after them in place. */
  static trim(row) {
    while (row.cells.length && !row.cells[row.cells.length - 1]) row.cells.pop();
  }

  save() {
    try {
      this.storage.setItem(this.key, JSON.stringify({
        nextId: this.nextId, defaultId: this.defaultId, rows: this.rows,
      }));
    } catch {}
  }

  /* The column count every row is drawn with: one past the furthest occupied
     cell of any row, so the columns line up across rows and there is always
     an empty column to move a session into. */
  columns() {
    return Math.max(0, ...this.rows.map((r) => r.cells.length)) + 1;
  }

  row(id) {
    return this.rows.find((r) => r.id === id) || null;
  }

  nameTaken(name, exceptId = null) {
    const low = name.toLowerCase();
    return this.rows.some((r) => r.id !== exceptId && r.name.toLowerCase() === low);
  }

  nextRowName() {
    for (const name of SessionGridLayout.ROW_NAMES) {
      if (!this.nameTaken(name)) return name;
    }
    for (let i = this.rows.length + 1; ; i++) {
      if (!this.nameTaken(`row-${i}`)) return `row-${i}`;
    }
  }

  addRow() {
    const row = { id: `r${this.nextId++}`, name: this.nextRowName(), cells: [] };
    this.rows.push(row);
    this.save();
    return row;
  }

  /* Returns null on success, otherwise the reason the name was refused. */
  renameRow(id, name) {
    const row = this.row(id);
    if (!row) return "no such row";
    const clean = SessionGridLayout.cleanName(name);
    if (!clean) return "a row name cannot be empty";
    if (this.nameTaken(clean, id)) return `another row is already named ${clean}`;
    row.name = clean;
    this.save();
    return null;
  }

  /* A row is removed only while no session in `present` sits in it; the
     assignments of sessions that are out of view go with it. The default row
     stays, so there is always somewhere to place a new session. */
  removeRow(id, present = new Set()) {
    const row = this.row(id);
    if (!row || id === this.defaultId) return false;
    if (row.cells.some((name) => name && present.has(name))) return false;
    this.rows = this.rows.filter((r) => r !== row);
    this.save();
    return true;
  }

  isDefault(id) {
    return id === this.defaultId;
  }

  setDefault(id) {
    const row = this.row(id);
    if (!row) return false;
    this.defaultId = id;
    delete row.rule;   // the default row is where unsorted sessions wait
    this.save();
    return true;
  }

  /* Sets (or, with an empty rule, clears) a row's condition. Returns null on
     success, otherwise the reason it was refused. */
  setRule(id, rule) {
    const row = this.row(id);
    if (!row) return "no such row";
    if (id === this.defaultId) return "the default row takes every new session and has no condition";
    const clean = SessionGridLayout.cleanRule(rule);
    if (clean) row.rule = clean;
    else delete row.rule;
    this.save();
    return null;
  }

  /* Moves each session in `present` that sits in the default row to the end
     of the first row, in row order, whose condition `matches(name, rule)`
     accepts. Sessions no condition accepts stay where they are; the cells
     the moved ones leave are empty, so the rest of the default row does not
     shift. Returns how many moved. */
  organize(matches, present = new Set()) {
    const inbox = this.row(this.defaultId);
    if (!inbox) return 0;
    let moved = 0;
    inbox.cells.forEach((name, col) => {
      if (!name || !present.has(name)) return;
      const target = this.rows.find((r) => r !== inbox && r.rule && matches(name, r.rule));
      if (!target) return;
      target.cells.push(name);
      inbox.cells[col] = null;
      moved++;
    });
    if (moved) {
      SessionGridLayout.trim(inbox);
      this.save();
    }
    return moved;
  }

  positionOf(name) {
    for (let r = 0; r < this.rows.length; r++) {
      const col = this.rows[r].cells.indexOf(name);
      if (col >= 0) return { row: r, col };
    }
    return null;
  }

  at(rowIndex, col) {
    const row = this.rows[rowIndex];
    return (row && row.cells[col]) || null;
  }

  /* Gives every session without a cell one in the default row: its first
     empty cell, else a new one at the end. Only the default row's holes are
     reused -- it is a waiting area, and organize() empties cells in it -- and
     a cell still assigned to a session out of view is not a hole. Returns
     whether anything was placed. */
  place(sessions) {
    let changed = false;
    const inbox = this.row(this.defaultId);
    for (const s of sessions || []) {
      if (!s || !s.name || this.positionOf(s.name)) continue;
      const hole = inbox.cells.indexOf(null);
      if (hole >= 0) inbox.cells[hole] = s.name;
      else inbox.cells.push(s.name);
      changed = true;
    }
    if (changed) this.save();
    return changed;
  }

  /* Moves `name` to (rowId, col). A session in `present` already there
     trades places with it; an out-of-view assignment there is dropped (that
     session is placed afresh if it comes back). Nothing else moves. */
  move(name, rowId, col, present = new Set()) {
    const target = this.row(rowId);
    const from = this.positionOf(name);
    if (!target || !from || !Number.isSafeInteger(col) || col < 0) return false;
    const source = this.rows[from.row];
    if (source === target && from.col === col) return false;
    const occupant = target.cells[col] || null;
    while (target.cells.length <= col) target.cells.push(null);
    source.cells[from.col] = occupant && present.has(occupant) ? occupant : null;
    target.cells[col] = name;
    SessionGridLayout.trim(source);
    SessionGridLayout.trim(target);
    this.save();
    return true;
  }

  /* How many cells row `rowIndex` is drawn with when its cells wrap onto
     lines of `perLine`: whole lines, enough to hold every occupied cell and
     one empty one after them. Cell `col` sits on line floor(col / perLine)
     at slot col % perLine, so a cell's place on screen follows from its
     column alone and never from what else is in the row. */
  span(rowIndex, perLine) {
    const row = this.rows[rowIndex];
    const used = (row ? row.cells.length : 0) + 1;
    return Math.ceil(used / perLine) * perLine;
  }

  /* The cell one step from (rowIndex, col), or null past an edge. Without
     `perLine` rows are single lines and up/down keep the column. With it,
     up/down go line by line: to the next line of the same row while there is
     one, then to the first line of the next row (or the last line of the
     previous one), keeping the slot. Left/right stay inside the row. */
  neighbor(rowIndex, col, dRow, dCol, perLine = 0) {
    const rows = this.rows.length;
    if (dCol) {
      const next = col + dCol;
      if (next < 0 || (perLine && next >= this.span(rowIndex, perLine))) return null;
      return { row: rowIndex, col: next };
    }
    if (!perLine) {
      const next = rowIndex + dRow;
      return next < 0 || next >= rows ? null : { row: next, col };
    }
    if (dRow > 0) {
      if (col + perLine < this.span(rowIndex, perLine)) return { row: rowIndex, col: col + perLine };
      return rowIndex + 1 < rows ? { row: rowIndex + 1, col: col % perLine } : null;
    }
    if (col - perLine >= 0) return { row: rowIndex, col: col - perLine };
    if (rowIndex === 0) return null;
    return { row: rowIndex - 1, col: this.span(rowIndex - 1, perLine) - perLine + (col % perLine) };
  }

  /* Keyboard form of move: one step in a direction (see neighbor). Refused
     at the edges rather than wrapping. */
  moveBy(name, dRow, dCol, present = new Set(), perLine = 0) {
    const from = this.positionOf(name);
    if (!from) return false;
    const to = this.neighbor(from.row, from.col, dRow, dCol, perLine);
    if (!to) return false;
    return this.move(name, this.rows[to.row].id, to.col, present);
  }
};
