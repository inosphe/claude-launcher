/* The Chat panel (chat.js), its page, its modal and its announcements.

   What has to hold:
     identity      -- a stored key is kept with its name; a missing or broken
                      one is replaced by a fresh 32-hex key and a guest name.
     unread        -- a room seen for the first time starts read at its end
                      (history is not news); after that it counts what
                      arrived past the read position.
     alerts        -- only rooms that grew since the previous poll are
                      fetched, only other people's messages alert, and the
                      plan raises a toast/tone only when the room is not on
                      screen and a desktop notification only for a hidden tab.
     text only     -- chat.js never assigns HTML.
     wired         -- route, view, nav entry beside Mesh with its badge,
                      script and stylesheet, the floating button, and posts
                      go to the chat API carrying the browser's key and name. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(root, "chat.js"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");

function slice(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name}`);
  let depth = 0;
  for (let j = source.indexOf("{", start); j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}") { depth--; if (!depth) return source.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

let failures = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL ${label}\n  got      ${a}\n  expected ${e}`); }
}

const ctx = {};
vm.createContext(ctx);
vm.runInContext(src.match(/^const KEY_RE = .*$/m)[0].replace("const ", "var "), ctx);
for (const name of ["mintKey", "identityFrom", "unreadOf", "grown", "alertable", "alertPlan"]) {
  vm.runInContext(slice(src, name), ctx);
}
const fill = (v) => (bytes) => bytes.fill(v);

check("a fresh key is 32 hex characters",
      ctx.mintKey(fill(171)), "ab".repeat(16));
check("a stored identity is kept",
      ctx.identityFrom(JSON.stringify({key: "abcdef0123", name: " Kim "}), fill(0)),
      {key: "abcdef0123", name: "Kim", fresh: false});
check("a stored key without a name gets a guest name",
      ctx.identityFrom(JSON.stringify({key: "abcdef0123"}), fill(0)).name, "guest-abcd");
check("nothing stored mints a fresh identity",
      ctx.identityFrom(null, fill(1)), {key: "01".repeat(16), name: "guest-0101", fresh: true});
check("a broken store mints a fresh identity",
      [ctx.identityFrom("{nope", fill(2)).fresh, ctx.identityFrom(JSON.stringify({key: "bad key!"}), fill(2)).fresh],
      [true, true]);

const rooms = [{mesh: "a", last_seq: 10}, {mesh: "b", last_seq: 4}];
check("a room seen for the first time is read up to its end",
      ctx.unreadOf(rooms, {}), {read: {a: 10, b: 4}, counts: {a: 0, b: 0}, total: 0});
check("after that, unread is what arrived past the read position",
      ctx.unreadOf(rooms, {a: 7, b: 4}), {read: {a: 7, b: 4}, counts: {a: 3, b: 0}, total: 3});
check("a read position past the end is never negative",
      ctx.unreadOf([{mesh: "a", last_seq: 2}], {a: 5}).counts, {a: 0});

check("only rooms that grew since the last poll are fetched, from where they were",
      ctx.grown(new Map([["a", 8], ["b", 4]]), rooms), [{mesh: "a", after: 8}]);
check("a room not seen at the previous poll is not news",
      ctx.grown(new Map([["a", 10]]), rooms), []);
check("my own messages do not alert",
      ctx.alertable([{key: "me", body: "x"}, {key: "you", body: "y"}], "me").map((m) => m.body), ["y"]);
check("room on screen: no toast, no tone",
      ctx.alertPlan({roomOnScreen: true, tabVisible: true, sound: true, desktop: true}),
      {toast: false, desktop: false, sound: false});
check("another room: toast and tone, no desktop notification while the tab is visible",
      ctx.alertPlan({roomOnScreen: false, tabVisible: true, sound: true, desktop: true}),
      {toast: true, desktop: false, sound: true});
check("hidden tab: everything, minus what the person switched off",
      ctx.alertPlan({roomOnScreen: false, tabVisible: false, sound: false, desktop: true}),
      {toast: true, desktop: true, sound: false});

check("chat.js never assigns HTML", /innerHTML|outerHTML|insertAdjacentHTML/.test(src), false);
check("the route parses", app.includes('if (parts[0] === "chat") return { page: "chat", name: parts[1] || "" };'), true);
check("the route opens the page", app.includes('case "chat": showView("chat"); globalThis.ChatPanel.open(r.name); break;'), true);
check("leaving the page stops its fast poll", app.includes('if (r.page !== "chat") globalThis.ChatPanel?.stop();'), true);
check("the view is registered", app.includes('chat: "chat-view",'), true);
check("the view exists", html.includes('<section id="chat-view"'), true);
check("the nav entry sits right after Mesh and carries the badge",
      /data-page="meshes">[^\n]*\n\s*<a href="#\/chat" data-page="chat">[^\n]*id="chat-nav-badge"/.test(html), true);
check("script and stylesheet are loaded",
      [html.includes('<script src="static/chat.js"></script>'), html.includes('href="static/chat.css"')], [true, true]);
check("the modal opens from anywhere through the floating button",
      [src.includes("document.body.append(fab)"), src.includes("fab.onclick = () => modal.open ? modal.close() : openModal();")], [true, true]);
check("a post carries this browser's key and name to the chat API",
      [src.includes("api/chat/rooms/${encodeURIComponent(mesh)}/messages"), src.includes("{key: me.key, name: me.name, body: text}")],
      [true, true]);
check("nothing is sent to a terminal",
      /api\/sessions\/[^"`]*\/(deliver|keys)|api\/mesh\/[^"`]*\/messages/.test(src), false);
check("the identity lives in localStorage under its own key",
      [src.includes('const ID_KEY = "claunch-chat-identity";'), src.includes("store.set(ID_KEY,")], [true, true]);
check("every localStorage access is guarded",
      (src.match(/localStorage\.\w+\(/g) || []).length, 3);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("chat_check: ok");
