// ui smoke test over the chrome devtools protocol. no dependencies.
//
// needs a chrome with a debugging port and a running server, ideally the fixture
// server so the repository has branches and a merge:
//   python3 gitadata/tests/fixture_server.py 8099 &
//   google-chrome --headless=new --no-sandbox --remote-debugging-port=9222 about:blank &
//   node gitadata/tests/test_ui.mjs "http://localhost:8099/?repo=fixture/repo"
//
// it never pushes: the push checkbox is explicitly turned off before apply, and
// the result is expected to say "not pushed".

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

const commitCount = () => evaluate("return document.querySelectorAll('svg.dag circle').length");
const graphRequests = () =>
  evaluate(`
    return performance.getEntriesByType('resource')
      .map((entry) => entry.name)
      .filter((name) => name.includes('/graph'));
  `);

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: target });

let circles = 0;
for (let i = 0; i < 80; i += 1) {
  await sleep(250);
  circles = await commitCount();
  if (circles > 0) break;
}

console.log("\n[render]");
check("graph drew commits", circles > 0, "circles=" + circles);
const edges = await evaluate("return document.querySelectorAll('svg.dag path.edge').length");
check("edges drawn", edges >= circles - 1, `edges=${edges} commits=${circles}`);
const merges = await evaluate("return document.querySelectorAll('svg.dag path.edge.secondary').length");
check("merge parents get their own edge style", merges >= 1, "secondary=" + merges);
check("repo shown in the header", (await evaluate("return document.querySelector('#repo-label').textContent")).includes("/"));

console.log("\n[theme] dark surface, poppins, lucide icons");
const theme = await evaluate(`
  await document.fonts.ready;
  const body = getComputedStyle(document.body);
  const html = getComputedStyle(document.documentElement);
  return {
    bg: body.backgroundColor,
    htmlBg: html.backgroundColor,
    font: body.fontFamily,
    poppins: document.fonts.check('13px Poppins'),
    icons: document.querySelectorAll('svg.lucide').length,
    chipIcons: document.querySelectorAll('#refs .chip svg.lucide').length,
    dark: getComputedStyle(document.documentElement).colorScheme,
  };
`);
check("page background is #121212", theme.bg === "rgb(18, 18, 18)" && theme.htmlBg === "rgb(18, 18, 18)", theme.bg + " / " + theme.htmlBg);
check("body text is poppins", /Poppins/i.test(theme.font), theme.font);
check("poppins actually loaded", theme.poppins === true, String(theme.poppins));
check("icons rendered as svg", theme.icons >= 4, "icons=" + theme.icons);
check("branch chips carry icons", theme.chipIcons >= 3, "chipIcons=" + theme.chipIcons);
check("the document declares a dark color scheme", theme.dark === "dark", String(theme.dark));

console.log("\n[columns] text must not overlap or spill out of the window");
const columns = await evaluate(`
  const rows = [...document.querySelectorAll('svg.dag .node')].slice(0, 8);
  return rows.map((row) => {
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
`);
check("subject never runs into the author column", columns.every((row) => row.subjectRight <= row.metaLeft + 1), JSON.stringify(columns[0]));
check("author column is not pushed off screen", columns.every((row) => row.window - row.metaRight >= 40), JSON.stringify(columns[0]));
check("ref badges clear the author column", columns.every((row) => row.badgeLeft === null || row.metaRight <= row.badgeLeft + 1), JSON.stringify(columns[0]));

console.log("\n[branches] the switcher");
const chips = await evaluate("return [...document.querySelectorAll('#refs .chip')].map((c) => c.textContent)");
check("a chip per branch plus all refs", chips.length >= 3, JSON.stringify(chips));
check("all refs is offered", chips.includes("all refs"), JSON.stringify(chips));
const activeBefore = await evaluate("return document.querySelector('#refs .chip.active')?.textContent");
check("the default branch is selected to begin with", activeBefore !== "all refs" && Boolean(activeBefore), String(activeBefore));

const everyCommit = await commitCount();
const featureView = await evaluate(`
  const chip = [...document.querySelectorAll('#refs .chip')].find((c) => c.textContent === 'feature');
  if (!chip) return { missing: true };
  chip.click();
  await new Promise((r) => setTimeout(r, 2500));
  return {
    count: document.querySelectorAll('svg.dag circle').length,
    active: document.querySelector('#refs .chip.active')?.textContent,
    label: document.querySelector('#view-label').textContent,
    status: document.querySelector('#status').textContent,
    requests: performance.getEntriesByType('resource').map((e) => e.name).filter((n) => n.includes('/graph')),
  };
`);
check("feature branch exists in the fixture", !featureView.missing);
check("switching narrows the graph", featureView.count > 0 && featureView.count < everyCommit, `${featureView.count} of ${everyCommit}`);
check("the chip becomes active", featureView.active === "feature", String(featureView.active));
check("the header says which branch", featureView.label === "feature", String(featureView.label));
check("the request carried the ref", featureView.requests.some((url) => url.includes("ref=refs%2Fheads%2Ffeature") || url.includes("ref=refs/heads/feature")), JSON.stringify(featureView.requests.slice(-2)));
check("status names the branch", featureView.status.includes("feature"), featureView.status);

const backToAll = await evaluate(`
  [...document.querySelectorAll('#refs .chip')].find((c) => c.textContent === 'all refs').click();
  await new Promise((r) => setTimeout(r, 2500));
  return document.querySelectorAll('svg.dag circle').length;
`);
check("all refs shows the whole repository again", backToAll === everyCommit, `${backToAll} vs ${everyCommit}`);

await evaluate(`
  [...document.querySelectorAll('#refs .chip')].find((c) => c.textContent === 'main').click();
  await new Promise((r) => setTimeout(r, 2500));
  return true;
`);

console.log("\n[right-click] opens the editor in the side panel");
const rightClicked = await evaluate(`
  const node = document.querySelectorAll('svg.dag .node')[3] || document.querySelector('svg.dag .node');
  node.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 300, clientY: 300 }));
  await new Promise((r) => setTimeout(r, 200));
  const inputs = [...document.querySelectorAll('#inspector input')];
  return {
    idle: document.querySelector('#inspector').className.includes('idle'),
    fields: inputs.map((i) => i.value),
    hasBody: !!document.querySelector('#inspector textarea'),
    focused: document.activeElement === inputs[0],
    buttons: [...document.querySelectorAll('#inspector .actions button')].map((b) => b.textContent),
    sha: document.querySelector('#inspector .sha')?.textContent,
  };
`);
check("the side panel is no longer idle", rightClicked.idle === false);
check("it shows the commit sha", /^[0-9a-f]{10}$/.test(rightClicked.sha || ""), String(rightClicked.sha));
check("title and author are editable", rightClicked.fields.length >= 4 && Boolean(rightClicked.fields[1]), JSON.stringify(rightClicked.fields));
check("description is editable", rightClicked.hasBody === true);
check("the title field takes focus", rightClicked.focused === true);
check("actions sit in the panel", rightClicked.buttons.includes("drop claude co-author"), JSON.stringify(rightClicked.buttons));

console.log("\n[edit] a pending change is counted");
const dirty = await evaluate(`
  const input = document.querySelectorAll('#inspector input')[0];
  input.value = 'chore: retitled from the ui';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 120));
  return { bar: document.querySelector('#dirty').textContent, dot: document.querySelector('#inspector .dirty-dot')?.textContent };
`);
check("the bar counts the edit", /1 commit/.test(dirty.bar), dirty.bar);
check("the panel marks the commit edited", dirty.dot === "edited", String(dirty.dot));
check("the bar names the scope", dirty.bar.includes("main"), dirty.bar);

console.log("\n[plan]");
const report = await evaluate(`
  const preview = [...document.querySelectorAll('#dirty button')].find((b) => b.textContent === 'preview');
  preview.click();
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    const text = document.querySelector('#report').textContent;
    if (text.includes('rewrite')) return text;
  }
  return document.querySelector('#report').textContent;
`);
check("plan reported", /rewrite/.test(report), report.slice(0, 140));

console.log("\n[apply without push]");
const applied = await evaluate(`
  const box = document.getElementById('push-toggle');
  box.checked = false;
  [...document.querySelectorAll('#dirty button')].find((b) => b.textContent === 'apply').click();
  for (let i = 0; i < 60; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    const text = document.querySelector('#report').textContent;
    if (text.includes('snapshot')) return { text, push: box.checked };
  }
  return { text: document.querySelector('#report').textContent, push: box.checked };
`);
check("push stayed off", applied.push === false);
check("rewrite applied in the clone only", /not pushed/.test(applied.text), applied.text.slice(0, 140));

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
