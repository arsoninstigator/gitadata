// ui smoke test over the chrome devtools protocol. no dependencies.
//
// needs a running server and headless chrome:
//   python3 -m uvicorn gitadata.server:app --port 8081 &
//   google-chrome --headless=new --no-sandbox --remote-debugging-port=9222 about:blank &
//   node gitadata/tests/test_ui.mjs "http://localhost:8081/?repo=owner/name"
//
// it never pushes: the push checkbox is explicitly turned off before apply, and
// the apply is expected to be refused by that same check.

const target = process.argv[2];
if (!target) {
  console.error("usage: node test_ui.mjs <url>");
  process.exit(2);
}

let pass = 0;
const failures = [];
function check(label, cond, detail = "") {
  if (cond) {
    pass += 1;
    console.log("  ok   " + label);
  } else {
    failures.push(label + (detail ? " :: " + detail : ""));
    console.log("  FAIL " + label + " " + detail);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
const page = pages.find((p) => p.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve));

let nextId = 0;
const pending = new Map();
ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  }
});
function send(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const res = await send("Runtime.evaluate", {
    expression: `(async () => { ${expression} })()`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (res.result?.exceptionDetails) {
    throw new Error(JSON.stringify(res.result.exceptionDetails.exception?.description || res.result.exceptionDetails));
  }
  return res.result?.result?.value;
}

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: target });

let circles = 0;
for (let i = 0; i < 60; i += 1) {
  await sleep(250);
  circles = await evaluate("return document.querySelectorAll('svg.dag circle').length");
  if (circles > 0) break;
}

console.log("\n[render]");
check("graph drew commits", circles > 0, "circles=" + circles);
const edges = await evaluate("return document.querySelectorAll('svg.dag path.edge').length");
check("edges drawn", edges >= circles - 1, `edges=${edges} commits=${circles}`);
const label = await evaluate("return document.querySelector('#repo-label').textContent");
check("repo shown in the header", label.includes("/"), label);

console.log("\n[columns] text must not overlap or spill out of the window");
const columns = await evaluate(`
  const rows = [...document.querySelectorAll('svg.dag .node')].slice(0, 8);
  const boxes = rows.map((row) => {
    const subject = row.querySelector('.subject')?.getBoundingClientRect();
    const meta = row.querySelector('.meta')?.getBoundingClientRect();
    const badge = row.querySelector('.badge')?.getBoundingClientRect();
    return {
      subjectRight: subject ? subject.right : 0,
      metaLeft: meta ? meta.left : 0,
      metaRight: meta ? meta.right : 0,
      badgeLeft: badge ? badge.left : null,
      window: window.innerWidth,
    };
  });
  return boxes;
`);
const overlapping = columns.filter((row) => row.subjectRight > row.metaLeft + 1);
check("subject never runs into the author column", overlapping.length === 0, JSON.stringify(overlapping[0]));
const spilled = columns.filter((row) => row.window - row.metaRight < 40);
check("author column is not pushed off screen", spilled.length === 0, JSON.stringify(spilled[0]));
const badgeOverlap = columns.filter((row) => row.badgeLeft !== null && row.metaRight > row.badgeLeft + 1);
check("ref badges clear the author column", badgeOverlap.length === 0, JSON.stringify(badgeOverlap[0]));

console.log("\n[select and edit]");
const first = await evaluate(`
  const node = document.querySelectorAll('svg.dag .node')[3] || document.querySelector('svg.dag .node');
  node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  return true;
`);
check("clicked a commit", first === true);
const inspector = await evaluate(`
  return {
    fields: [...document.querySelectorAll('#inspector input')].map((i) => i.value),
    hasBody: !!document.querySelector('#inspector textarea'),
    idle: document.querySelector('#inspector').className.includes('idle')
  };
`);
check("inspector opened", inspector.idle === false);
check("author name prefilled", Boolean(inspector.fields[1]), JSON.stringify(inspector.fields));
check("description field present", inspector.hasBody === true);

const dirtyAfterEdit = await evaluate(`
  const input = document.querySelectorAll('#inspector input')[0];
  input.value = input.value + ' (edited in ui)';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  return document.querySelector('#dirty').textContent;
`);
check("pending edit counted", /1 commit/.test(dirtyAfterEdit), dirtyAfterEdit);

console.log("\n[context menu]");
const menu = await evaluate(`
  const node = document.querySelectorAll('svg.dag .node')[2];
  node.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 300, clientY: 300 }));
  await new Promise((r) => setTimeout(r, 50));
  const items = [...document.querySelectorAll('#menu button')].map((b) => b.textContent);
  const present = !!document.getElementById('menu');
  document.body.click();
  return { present, items };
`);
check("menu opened on right click", menu.present === true);
check("menu has the edits", menu.items.length === 3, JSON.stringify(menu.items));

console.log("\n[plan]");
const report = await evaluate(`
  const buttons = [...document.querySelectorAll('#dirty button')];
  const preview = buttons.find((b) => b.textContent === 'preview');
  preview.click();
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    const text = document.querySelector('#report').textContent;
    if (text.includes('rewrite') || text.includes('nothing')) return text;
  }
  return document.querySelector('#report').textContent;
`);
check("plan reported", /rewrite/.test(report), report.slice(0, 120));
const movedNothing = await evaluate(`
  const res = await fetch('/api/sessions');
  const data = await res.json();
  return data.sessions.length;
`);
check("session still open after planning", movedNothing >= 1);

console.log("\n[apply without push]");
const applied = await evaluate(`
  const box = document.getElementById('push-toggle');
  box.checked = false;
  const apply = [...document.querySelectorAll('#dirty button')].find((b) => b.textContent === 'apply');
  apply.click();
  for (let i = 0; i < 60; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    const text = document.querySelector('#report').textContent;
    if (text.includes('snapshot')) return { text, push: box.checked };
  }
  return { text: document.querySelector('#report').textContent, push: box.checked };
`);
check("push stayed off", applied.push === false);
check("rewrite applied in the clone only", /not pushed/.test(applied.text), applied.text.slice(0, 140));
check("undo offered", /undo this rewrite/.test(applied.text));

console.log("\n[undo]");
const undone = await evaluate(`
  const button = [...document.querySelectorAll('#report button')].find((b) => b.textContent.includes('undo'));
  button.click();
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    const text = document.querySelector('#status').textContent;
    if (text.includes('restored')) return text;
  }
  return document.querySelector('#status').textContent;
`);
check("undo restored the snapshot", /restored/.test(undone), undone);

console.log(`\n${pass} checks passed, ${failures.length} failed`);
for (const f of failures) console.log("  - " + f);
ws.close();
process.exit(failures.length ? 1 : 0);
