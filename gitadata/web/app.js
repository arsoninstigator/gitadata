// gitadata ui: open a repo, read the graph, edit commit metadata, preview, apply.
// plain modules, no build step. all copy is lowercase on purpose.

import { layout, truncate } from "./graph.js";

const ROW = 34;
const LANE = 22;
const PAD = 18;
const GRAPH_LEFT = 8;

const state = {
  session: null,
  repo: null,
  defaultBranch: null,
  graph: null,
  refs: [],          // [refs/heads/main, ...]
  selected: null,
  draft: {},         // sha -> fields
  plan: null,
  snapshot: null,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function status(message, kind = "") {
  const bar = $("#status");
  bar.textContent = message;
  bar.className = "status " + kind;
}

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------

async function openRepo(name) {
  if (!name) return;
  status("cloning " + name + " ...");
  try {
    const session = await api("/api/sessions", { method: "POST", body: { repo: name } });
    state.session = session.session;
    state.repo = session.repo;
    state.defaultBranch = session.default_branch;
    state.draft = {};
    state.plan = null;
    state.snapshot = null;
    $("#repo-label").textContent = session.repo;
    if (session.empty) {
      status("this repository has no commits yet", "warn");
      $("#graph").replaceChildren(el("p", { class: "empty", text: "nothing to draw yet" }));
      $("#refs").replaceChildren();
      $("#inspector").classList.add("idle");
      return;
    }
    await loadGraph();
    status("loaded " + state.graph.commits.length + " commits");
  } catch (err) {
    status(err.message, "error");
  }
}

async function loadGraph() {
  const graph = await api(`/api/sessions/${state.session}/graph`);
  state.graph = graph;
  state.refs = [graph.default_branch ? `refs/heads/${graph.default_branch}` : null].filter(Boolean);
  renderGraph();
  renderRefs();
  renderDirty();
}

// ---------------------------------------------------------------------------
// graph rendering
// ---------------------------------------------------------------------------

function renderGraph() {
  const commits = [...state.graph.commits].reverse(); // newest first, top of screen
  const model = layout(commits);
  const scroller = $("#graph");
  const width = Math.max(360, (scroller.clientWidth || 900) - 2);
  const height = commits.length * ROW + PAD * 2;

  // columns: lanes, subject, author, ref badges. the subject takes whatever is left.
  const laneWidth = model.laneCount * LANE + 24;
  const metaWidth = 150;
  const badgeWidth = commits.reduce((widest, c) => {
    const total = (c.refs || []).reduce((sum, label) => sum + label.length * 6.6 + 12, 0);
    return Math.max(widest, total);
  }, 0);
  const subjectWidth = Math.max(120, width - laneWidth - metaWidth - badgeWidth - 32);
  const maxChars = Math.max(16, Math.floor(subjectWidth / 6.9));

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "dag");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const xOf = (lane) => GRAPH_LEFT + lane * LANE + LANE / 2;
  const yOf = (row) => PAD + row * ROW + ROW / 2;

  for (const edge of model.edges) {
    const x1 = xOf(edge.from.lane);
    const y1 = yOf(edge.from.row);
    const x2 = xOf(edge.to.lane);
    const y2 = yOf(edge.to.row);
    let d;
    if (x1 === x2) {
      d = `M ${x1} ${y1} L ${x2} ${y2}`;
    } else {
      const mid = (y1 + y2) / 2;
      d = `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`;
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("class", "edge" + (edge.primary ? "" : " secondary"));
    svg.append(path);
  }

  for (const node of model.nodes) {
    const commit = node.commit;
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", "node" + (node.sha === state.selected ? " selected" : ""));
    group.setAttribute("transform", `translate(0, ${yOf(node.row) - ROW / 2})`);

    const hit = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    hit.setAttribute("x", 0);
    hit.setAttribute("y", 0);
    hit.setAttribute("width", width);
    hit.setAttribute("height", ROW);
    hit.setAttribute("class", "hit");
    group.append(hit);

    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", xOf(node.lane));
    dot.setAttribute("cy", ROW / 2);
    dot.setAttribute("r", 5.5);
    dot.setAttribute("class", "dot");
    group.append(dot);

    const title = document.createElementNS("http://www.w3.org/2000/svg", "text");
    title.setAttribute("x", laneWidth);
    title.setAttribute("y", ROW / 2 + 4);
    title.setAttribute("class", "subject");
    title.textContent = truncate(commit.subject, maxChars);
    group.append(title);

    const meta = document.createElementNS("http://www.w3.org/2000/svg", "text");
    meta.setAttribute("x", laneWidth + subjectWidth);
    meta.setAttribute("y", ROW / 2 + 4);
    meta.setAttribute("class", "meta");
    meta.textContent = commit.author.name + "  " + commit.author.date.slice(0, 10);
    group.append(meta);

    let offset = laneWidth + subjectWidth + metaWidth;
    for (const label of commit.refs || []) {
      const badge = document.createElementNS("http://www.w3.org/2000/svg", "text");
      badge.setAttribute("x", offset);
      badge.setAttribute("y", ROW / 2 + 4);
      badge.setAttribute("class", "badge");
      badge.textContent = label;
      group.append(badge);
      offset += label.length * 6.6 + 12;
    }

    group.addEventListener("click", () => select(node.sha));
    group.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      select(node.sha);
      openMenu(event.clientX, event.clientY, node.commit);
    });
    svg.append(group);
  }

  scroller.replaceChildren(svg);
}

// ---------------------------------------------------------------------------
// refs and edit scope
// ---------------------------------------------------------------------------

function renderRefs() {
  const box = $("#refs");
  box.replaceChildren(
    ...state.graph.refs
      .filter((ref) => ref.kind === "commit" || ref.name.startsWith("refs/heads/"))
      .map((ref) =>
        el("label", { class: "chip" }, [
          el("input", {
            type: "checkbox",
            checked: state.refs.includes(ref.name) ? "checked" : null,
            onchange: (event) => {
              state.refs = event.target.checked
                ? [...new Set([...state.refs, ref.name])]
                : state.refs.filter((entry) => entry !== ref.name);
              renderDirty();
            },
          }),
          el("span", { text: ref.short }),
        ])
      )
  );
}

// ---------------------------------------------------------------------------
// inspector
// ---------------------------------------------------------------------------

function commitBySha(sha) {
  return state.graph.commits.find((c) => c.sha === sha);
}

function select(sha) {
  state.selected = sha;
  renderGraph();
  renderInspector();
}

function currentValues(commit) {
  const draft = state.draft[commit.sha] || {};
  return {
    subject: draft.title ?? commit.subject,
    body: draft.body ?? "",
    authorName: draft.author_name ?? commit.author.name,
    authorEmail: draft.author_email ?? commit.author.email,
    authorDate: (draft.author_date ?? commit.author.date).slice(0, 19),
    committerDate: (draft.committer_date ?? commit.committer.date).slice(0, 19),
  };
}

function renderInspector() {
  const panel = $("#inspector");
  const commit = state.selected && commitBySha(state.selected);
  if (!commit) {
    panel.className = "inspector idle";
    panel.replaceChildren(el("p", { class: "empty", text: "select a commit" }));
    return;
  }
  panel.className = "inspector";
  const values = currentValues(commit);

  const field = (label, key, value, type = "text") =>
    el("div", { class: "field" }, [
      el("label", { text: label }),
      el("input", {
        type,
        value,
        oninput: (event) => setDraft(commit, key, event.target.value),
      }),
    ]);

  panel.replaceChildren(
    el("div", { class: "inspector-head" }, [
      el("code", { class: "sha", text: commit.sha.slice(0, 10) }),
      el("span", { class: "parents", text: (commit.parents || []).map((p) => p.slice(0, 7)).join(" ") }),
    ]),
    field("title", "title", values.subject),
    el("div", { class: "field" }, [
      el("label", { text: "description" }),
      el("textarea", {
        rows: "5",
        oninput: (event) => setDraft(commit, "body", event.target.value),
      }, [document.createTextNode(values.body)]),
    ]),
    field("author name", "author_name", values.authorName),
    field("author email", "author_email", values.authorEmail),
    field("author date", "author_date", values.authorDate, "datetime-local"),
    field("committer date", "committer_date", values.committerDate, "datetime-local"),
    el("div", { class: "actions" }, [
      el("button", { class: "ghost", text: "reset this commit", onclick: () => resetCommit(commit.sha) }),
      el("button", {
        class: "ghost",
        text: "drop claude co-author",
        onclick: () => {
          state.draft[commit.sha] = { ...(state.draft[commit.sha] || {}), strip_trailers_matching: "^co-authored-by:.*claude" };
          renderDirty();
          status("will strip co-authored-by lines mentioning claude on this commit");
        },
      }),
    ])
  );
}

function setDraft(commit, key, value) {
  const original = {
    title: commit.subject,
    body: "",
    author_name: commit.author.name,
    author_email: commit.author.email,
    author_date: commit.author.date.slice(0, 19),
    committer_date: commit.committer.date.slice(0, 19),
  };
  const draft = { ...(state.draft[commit.sha] || {}) };
  if (String(original[key] ?? "") === String(value)) delete draft[key];
  else draft[key] = value;
  if (Object.keys(draft).length) state.draft[commit.sha] = draft;
  else delete state.draft[commit.sha];
  renderDirty();
}

function resetCommit(sha) {
  delete state.draft[sha];
  renderInspector();
  renderDirty();
}

// ---------------------------------------------------------------------------
// context menu
// ---------------------------------------------------------------------------

function openMenu(x, y, commit) {
  closeMenu();
  const menu = el("div", { class: "menu", id: "menu" }, [
    el("button", { text: "edit metadata", onclick: () => { closeMenu(); $("#inspector input")?.focus(); } }),
    el("button", {
      text: "remove claude co-author trailer",
      onclick: () => {
        state.draft[commit.sha] = { ...(state.draft[commit.sha] || {}), strip_trailers_matching: "^co-authored-by:.*claude" };
        closeMenu();
        renderInspector();
        renderDirty();
      },
    }),
    el("button", {
      text: "copy sha",
      onclick: () => { navigator.clipboard?.writeText(commit.sha); closeMenu(); status("sha copied"); },
    }),
  ]);
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  document.body.append(menu);
  setTimeout(() => document.addEventListener("click", closeMenu, { once: true }), 0);
}

function closeMenu() {
  document.getElementById("menu")?.remove();
}

// ---------------------------------------------------------------------------
// edits -> api shape
// ---------------------------------------------------------------------------

function editsPayload() {
  const edits = {};
  for (const [sha, draft] of Object.entries(state.draft)) {
    const commit = commitBySha(sha);
    const fields = {};
    if (draft.title !== undefined || draft.body !== undefined) {
      const title = draft.title ?? commit.subject;
      const body = draft.body ?? "";
      fields.message = body.trim() ? `${title}\n\n${body.trim()}\n` : `${title}\n`;
    }
    for (const key of ["author_name", "author_email", "author_date", "committer_date", "strip_trailers_matching"]) {
      if (draft[key] !== undefined) fields[key] = draft[key];
    }
    if (Object.keys(fields).length) edits[sha] = fields;
  }
  return edits;
}

function renderDirty() {
  const edits = editsPayload();
  const count = Object.keys(edits).length;
  const bar = $("#dirty");
  bar.classList.toggle("has", count > 0);
  bar.replaceChildren(
    el("span", {
      text: count
        ? `${count} commit${count === 1 ? "" : "s"} edited, scope: ${state.refs.map((r) => r.replace("refs/heads/", "")).join(", ") || "none"}`
        : "no pending edits",
    }),
    el("input", { type: "checkbox", id: "push-toggle", checked: "checked" }),
    el("label", { for: "push-toggle", text: "push to github" }),
    el("button", { class: "primary", text: "preview", disabled: count === 0, onclick: preview }),
    el("button", { class: "primary solid", text: "apply", disabled: count === 0, onclick: apply })
  );
}

async function preview() {
  try {
    status("building plan ...");
    const plan = await api(`/api/sessions/${state.session}/plan`, {
      method: "POST",
      body: { edits: editsPayload(), refs: state.refs },
    });
    state.plan = plan;
    const lines = [
      el("h3", { text: plan.summary }),
      el("p", { class: "muted", text: `${plan.rewritten_count} commits change sha` }),
    ];
    if (plan.noop) lines.push(el("p", { class: "warn", text: "nothing would move" }));
    for (const ref of plan.refs.filter((r) => r.changed)) {
      lines.push(el("p", { class: "ref-change", text: `${ref.ref}  ${ref.old.slice(0, 10)} -> ${ref.new.slice(0, 10)}` }));
    }
    for (const warning of plan.warnings) lines.push(el("p", { class: "warn", text: warning }));
    $("#report").replaceChildren(...lines);
    status("plan ready, nothing has moved yet");
  } catch (err) {
    status(err.message, "error");
  }
}

async function apply() {
  const push = $("#push-toggle")?.checked ?? true;
  const label = push ? "rewriting and pushing" : "rewriting locally (no push)";
  status(label + " ...");
  try {
    const result = await api(`/api/sessions/${state.session}/apply`, {
      method: "POST",
      body: { edits: editsPayload(), refs: state.refs, push },
    });
    state.snapshot = result.snapshot;
    state.draft = {};
    const lines = [el("h3", { text: result.plan.summary })];
    lines.push(el("p", { class: "muted", text: `snapshot ${result.snapshot}${result.pushed ? ", pushed" : ", not pushed"}` }));
    for (const note of result.prechecks?.notes || []) lines.push(el("p", { class: "warn", text: note }));
    if (result.push_output) lines.push(el("pre", { class: "out", text: result.push_output }));
    lines.push(el("button", { class: "ghost", text: "undo this rewrite", onclick: undo }));
    $("#report").replaceChildren(...lines);
    await loadGraph();
    status(result.pushed ? "rewritten and pushed" : "rewritten in the clone only", "ok");
  } catch (err) {
    status(err.message, "error");
  }
}

async function undo() {
  if (!state.snapshot) return;
  try {
    const ts = state.snapshot.split("/").pop();
    await api(`/api/sessions/${state.session}/undo`, { method: "POST", body: { ts } });
    state.snapshot = null;
    await loadGraph();
    status("restored the snapshot", "ok");
  } catch (err) {
    status(err.message, "error");
  }
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

async function boot() {
  $("#open").addEventListener("submit", (event) => {
    event.preventDefault();
    openRepo($("#repo-input").value.trim());
  });
  try {
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      if (!state.graph) return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(renderGraph, 120);
    });

    const me = await api("/api/github/me");
    $("#who").textContent = me.login;
    const { repos } = await api("/api/github/repos?limit=100");
    $("#repo-options").replaceChildren(
      ...repos.map((repo) => el("option", { value: repo.full_name, text: `${repo.full_name} (${repo.size_kb} kb)` }))
    );
  } catch (err) {
    status(err.message, "error");
  }
  renderInspector();
  renderDirty();

  const wanted = new URLSearchParams(location.search).get("repo");
  if (wanted) {
    $("#repo-input").value = wanted;
    openRepo(wanted);
  }
}

boot();
