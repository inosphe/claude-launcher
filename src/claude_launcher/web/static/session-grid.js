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
   out of view and coming back -- until something is moved onto it. */
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
    try {
      const saved = JSON.parse(storage.getItem(key) || "null");
      if (saved && Array.isArray(saved.rows)) this.load(saved);
    } catch {}
    if (!this.rows.length) this.addRow();
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
      this.rows.push({ id: row.id, name, cells });
      SessionGridLayout.trim(this.rows[this.rows.length - 1]);
    }
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

  /* Trailing empty cells carry no position, so they are not stored; interior
     ones are the holes that keep the cells after them in place. */
  static trim(row) {
    while (row.cells.length && !row.cells[row.cells.length - 1]) row.cells.pop();
  }

  save() {
    try {
      this.storage.setItem(this.key, JSON.stringify({ nextId: this.nextId, rows: this.rows }));
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
     assignments of sessions that are out of view go with it. The last row
     stays, so there is always somewhere to place a new session. */
  removeRow(id, present = new Set()) {
    const row = this.row(id);
    if (!row || this.rows.length < 2) return false;
    if (row.cells.some((name) => name && present.has(name))) return false;
    this.rows = this.rows.filter((r) => r !== row);
    this.save();
    return true;
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

  /* Gives every session without a cell one at the end of a row: its parent's
     row when the parent is placed, so a spawned child lands beside the one
     that made it, otherwise the last row. Holes are never filled here -- a
     hole is somebody's former place, and only a person decides to reuse it.
     Returns whether anything was placed. */
  place(sessions) {
    let changed = false;
    for (const s of sessions || []) {
      if (!s || !s.name || this.positionOf(s.name)) continue;
      const parent = s.parent ? this.positionOf(s.parent) : null;
      const row = this.rows[parent ? parent.row : this.rows.length - 1];
      row.cells.push(s.name);
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

  /* Keyboard form of move: one step in a direction. Refused at the left edge
     and past the first or last row rather than wrapping. */
  moveBy(name, dRow, dCol, present = new Set()) {
    const from = this.positionOf(name);
    if (!from) return false;
    const row = from.row + dRow;
    const col = from.col + dCol;
    if (row < 0 || row >= this.rows.length || col < 0) return false;
    return this.move(name, this.rows[row].id, col, present);
  }
};
