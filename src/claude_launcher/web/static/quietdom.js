/* Same-value DOM writes become no-ops (claunch-v87).

   The dashboard's pollers repaint by assignment: every tick writes each
   rail row's title, aria-pressed, class and text again, whether or not the
   value moved. The browser cannot tell an unchanged write from a change, so
   each one is a mutation record -- 34,519 of them in 30 s on a 389-row rail,
   about 1,150 a second. A browser extension that watches the page
   (1Password's MutationObserver walks the document with a TreeWalker on
   every batch) pays for all of them, and that walk was the input lag: ~1 s
   every few seconds, gone the moment the extension was switched off.

   Writing the same value again has no effect anyone can see except that
   record, so the setters below return early when nothing would change. Only
   writes whose repetition is inert are guarded: an iframe's src (which
   navigates again) and innerHTML (which a caller may reuse to drop the
   listeners on the nodes it replaces) are left exactly as they were.

   installQuietDom takes the global object so the node check can hand it a
   fake DOM; in the page it runs once, before any other script. */
function installQuietDom(g) {
  if (!g || !g.Element || g.Element.prototype.__quietDom) return false;
  const E = g.Element.prototype;
  E.__quietDom = true;

  // Attributes whose re-assignment does something even with the same value.
  const ACTIVE = new Set(["src", "srcdoc", "data"]);
  const setAttribute = E.setAttribute;
  E.setAttribute = function (name, value) {
    const key = String(name).toLowerCase();
    if (!ACTIVE.has(key) && this.getAttribute(name) === String(value)) return;
    return setAttribute.call(this, name, value);
  };

  // A reflected property whose setter writes `attr`: skip the write when the
  // attribute already reads what the setter would leave there.
  const guard = (proto, prop, same) => {
    if (!proto) return;
    const d = Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.set || !d.get) return;
    Object.defineProperty(proto, prop, {
      configurable: true, enumerable: d.enumerable,
      get: d.get,
      set(value) { if (!same(this, value)) d.set.call(this, value); },
    });
  };
  const sameString = (attr) => (el, v) => el.getAttribute(attr) === String(v);
  const sameFlag = (attr) => (el, v) => el.hasAttribute(attr) === !!v;
  // aria* properties take null to mean "remove the attribute".
  const sameAria = (attr) => (el, v) => v === null || v === undefined
    ? !el.hasAttribute(attr) : el.getAttribute(attr) === String(v);

  guard(E, "className", sameString("class"));
  guard(E, "id", sameString("id"));
  const H = g.HTMLElement && g.HTMLElement.prototype;
  guard(H, "title", sameString("title"));
  guard(H, "hidden", (el, v) => v !== "until-found" && sameFlag("hidden")(el, v));
  for (const name of ["Button", "Input", "Select", "TextArea", "FieldSet", "OptGroup", "Option"]) {
    const C = g[`HTML${name}Element`];
    guard(C && C.prototype, "disabled", sameFlag("disabled"));
  }
  for (const prop of Object.getOwnPropertyNames(E)) {
    if (!/^aria[A-Z]/.test(prop)) continue;
    const attr = "aria-" + prop.slice(4).toLowerCase();
    guard(E, prop, sameAria(attr));
  }

  // Text: an element whose only child is a text node holding the same string
  // keeps that node; a text node keeps its data.
  const N = g.Node && g.Node.prototype;
  const TEXT = 3;
  guard(N, "textContent", (node, v) => {
    const text = v === null || v === undefined ? "" : String(v);
    if (node.nodeType === TEXT) return node.data === text;
    if (node.nodeType !== 1) return false;
    const first = node.firstChild;
    if (!first) return text === "";
    return first === node.lastChild && first.nodeType === TEXT && first.data === text;
  });
  const C = g.CharacterData && g.CharacterData.prototype;
  guard(C, "data", (node, v) => node.data === String(v));

  // classList.add/remove/toggle rewrite the class attribute even when the
  // token was already there (or already gone).
  const L = g.DOMTokenList && g.DOMTokenList.prototype;
  if (L) {
    const add = L.add, remove = L.remove, toggle = L.toggle;
    L.add = function (...tokens) {
      if (tokens.every((t) => this.contains(t))) return;
      return add.apply(this, tokens);
    };
    L.remove = function (...tokens) {
      if (!tokens.some((t) => this.contains(t))) return;
      return remove.apply(this, tokens);
    };
    L.toggle = function (token, force) {
      if (force !== undefined && this.contains(token) === !!force) return !!force;
      return force === undefined ? toggle.call(this, token) : toggle.call(this, token, force);
    };
  }
  return true;
}

if (typeof window !== "undefined") installQuietDom(window);
if (typeof module !== "undefined") module.exports = { installQuietDom };
