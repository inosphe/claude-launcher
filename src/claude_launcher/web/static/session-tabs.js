/* Browser-local session history. Display order and LRU order are independent. */
globalThis.SessionTabHistory = class SessionTabHistory {
  constructor(storage, key, pins = []) {
    this.storage = storage;
    this.key = key;
    this.entries = [];
    try {
      const saved = JSON.parse(storage.getItem(key) || "[]");
      if (Array.isArray(saved)) for (const item of saved) {
        if (item && typeof item.name === "string" && item.name &&
            Number.isSafeInteger(item.used) && item.used >= 0 &&
            !this.entries.some(e => e.name === item.name)) {
          this.entries.push({ name: item.name, used: item.used });
        }
      }
    } catch {}
    this.syncPins(pins);
  }
  save() {
    try { this.storage.setItem(this.key, JSON.stringify(this.entries)); } catch {}
  }
  trim() {
    const recent = this.entries.filter(e => !this.pins.includes(e.name));
    const evicted = new Set(recent.slice().sort((a, b) => b.used - a.used).slice(5).map(e => e.name));
    this.entries = this.entries.filter(e => !evicted.has(e.name));
    this.save();
  }
  syncPins(pins) {
    this.pins = pins.slice();
    for (const name of pins) {
      if (!this.entries.some(e => e.name === name)) this.entries.push({ name, used: 0 });
    }
    this.trim();
  }
  visit(name) {
    if (!name) return;
    let entry = this.entries.find(e => e.name === name);
    const used = Math.max(0, ...this.entries.map(e => e.used)) + 1;
    if (!entry) { entry = { name, used }; this.entries.push(entry); }
    entry.used = used;
    this.trim();
  }
  close(name) {
    this.entries = this.entries.filter(e => e.name !== name);
    this.save();
  }
  names() {
    return [...this.pins, ...this.entries.filter(e => !this.pins.includes(e.name)).map(e => e.name)];
  }
  latest() {
    return this.entries.slice().sort((a, b) => b.used - a.used)[0]?.name || null;
  }
};
