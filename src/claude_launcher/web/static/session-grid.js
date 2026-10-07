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
   out of view and coming back -- and moves like any other; a move onto an
   occupied cell always swaps, so no assignment is lost to one.

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

  /* Moves every session assigned to row `id` -- in view or not -- into the
     default row, in column order, each into the default row's first empty
     cell or onto its end. The row itself stays, empty. Refused for the
     default row. Returns how many moved, or -1 when refused. */
  clearRow(id) {
    const row = this.row(id);
    const inbox = this.row(this.defaultId);
    if (!row || row === inbox) return -1;
    let moved = 0;
    for (const name of row.cells) {
      if (!name) continue;
      const hole = inbox.cells.indexOf(null);
      if (hole >= 0) inbox.cells[hole] = name;
      else inbox.cells.push(name);
      moved++;
    }
    row.cells = [];
    this.save();
    return moved;
  }

  /* clearRow, then remove the row. Refused for the default row. */
  clearAndRemoveRow(id) {
    if (this.clearRow(id) < 0) return false;
    this.rows = this.rows.filter((r) => r.id !== id);
    this.save();
    return true;
  }

  /* Moves row `id` up (delta < 0) or down (delta > 0) among the rows. Row
     order is also the order Auto-organize tries the conditions in. */
  moveRow(id, delta) {
    const from = this.rows.findIndex((r) => r.id === id);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= this.rows.length || to === from) return false;
    const [row] = this.rows.splice(from, 1);
    this.rows.splice(to, 0, row);
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

  /* Moves `name` to (rowId, col). Whatever session was assigned there --
     in view or not -- trades places with it, so no assignment is ever lost
     to a move. Nothing else moves. (`present` is accepted for callers that
     still pass it and is not needed.) */
  move(name, rowId, col, present = new Set()) {
    const target = this.row(rowId);
    const from = this.positionOf(name);
    if (!target || !from || !Number.isSafeInteger(col) || col < 0) return false;
    const source = this.rows[from.row];
    if (source === target && from.col === col) return false;
    const occupant = target.cells[col] || null;
    while (target.cells.length <= col) target.cells.push(null);
    source.cells[from.col] = occupant;
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

  /* The stretches of row `rowIndex` that can be folded away because the
     current view shows nothing in them: runs of at least `minLines`
     consecutive lines, at the top or anywhere after it, none holding a
     session in `present` and at least one holding a session out of view (a
     filter is what emptied them; lines that are merely empty keep their
     place on screen). The row's last line is never folded, so something can
     still be dropped into it. Returns [{ start, end }] in line numbers, end
     exclusive. */
  foldRuns(rowIndex, perLine, present = new Set(), minLines = 3) {
    const row = this.rows[rowIndex];
    if (!row) return [];
    const lines = this.span(rowIndex, perLine) / perLine;
    const runs = [];
    let start = -1;
    let absent = false;
    const close = (end) => {
      if (start >= 0 && absent && end - start >= minLines) runs.push({ start, end });
      start = -1;
      absent = false;
    };
    for (let line = 0; line < lines - 1; line++) {
      const slice = row.cells.slice(line * perLine, (line + 1) * perLine);
      if (slice.some((name) => name && present.has(name))) {
        close(line);
        continue;
      }
      if (start < 0) start = line;
      if (slice.some(Boolean)) absent = true;
    }
    close(lines - 1);
    return runs;
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

/* The lineage overlay: with a session selected, arrows run from its parent's
   cell to it and from it to each child's cell, in the spawn direction, the
   parent link and the child links in two colours (app.js draws them). These
   are the parts that need no page: which links there are, and the curve of
   one arrow between two cell boxes ({x, y, w, h}, in the grid's scrolled
   content coordinates). */
globalThis.SessionGridLineage = {
  /* The selected session's lineage as links, parent first, then the
     children in the order the sessions came. A session that names itself as
     its parent is no lineage. */
  links(selected, sessions) {
    if (!selected) return [];
    const list = sessions || [];
    const out = [];
    const me = list.find((s) => s && s.name === selected);
    if (me && me.parent && me.parent !== selected) {
      out.push({ from: me.parent, to: selected, kind: "parent" });
    }
    for (const s of list) {
      if (s && s.parent === selected && s.name !== selected) {
        out.push({ from: selected, to: s.name, kind: "child" });
      }
    }
    return out;
  },

  /* Where the line from the box's centre towards a point leaves the box. */
  edgePoint(box, toward) {
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    const dx = toward.x - cx;
    const dy = toward.y - cy;
    if (!dx && !dy) return { x: cx, y: cy };
    const t = Math.min(dx ? (box.w / 2) / Math.abs(dx) : Infinity,
                       dy ? (box.h / 2) / Math.abs(dy) : Infinity);
    return { x: cx + dx * t, y: cy + dy * t };
  },

  /* One arrow from box a to box b: a quadratic curve bowed to one side, so
     an arrow between cells on one line clears the cells between them. It starts
     inside a, halfway from its centre to its border (at its centre when
     that leaves less than 24px to go), and ends on b's border,
     each end along the curve's own tangent so the arrowhead points into b.
     Starting inside matters for neighbours: the gap between two cells is a
     few pixels, and a curve from border to border would be nothing but its
     arrowhead. Null for one box. */
  arrow(a, b) {
    const ax = a.x + a.w / 2, ay = a.y + a.h / 2;
    const bx = b.x + b.w / 2, by = b.y + b.h / 2;
    const dx = bx - ax, dy = by - ay;
    const dist = Math.hypot(dx, dy);
    if (dist < 1) return null;
    // Gentle for neighbours, so the curve stays inside the cells' band and
    // enters the target from its side, not from below; up to 24px for long
    // arrows so they clear the cells they pass.
    const bend = Math.max(6, Math.min(24, dist * 0.15));
    // Bow upwards (leftwards for a vertical arrow): a link along a line then
    // arcs over the row heading's side of the cells, clear of the arrows
    // that leave the same cell for the line below.
    let px = -dy / dist, py = dx / dist;
    if (py > 0 || (py === 0 && px > 0)) { px = -px; py = -py; }
    const control = { x: (ax + bx) / 2 + px * bend, y: (ay + by) / 2 + py * bend };
    const end = this.edgePoint(b, control);
    const rim = this.edgePoint(a, control);
    let start = { x: (ax + rim.x) / 2, y: (ay + rim.y) / 2 };
    // Cells one above the other leave even less: start from the centre.
    if (Math.hypot(end.x - start.x, end.y - start.y) < 24) start = { x: ax, y: ay };
    const r = (n) => Math.round(n * 10) / 10;
    return {
      start, end, control,
      d: `M${r(start.x)} ${r(start.y)} Q${r(control.x)} ${r(control.y)} ${r(end.x)} ${r(end.y)}`,
    };
  },
};
