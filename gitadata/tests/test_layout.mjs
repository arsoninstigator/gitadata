// node --test style assertions without a runner: plain node, plain asserts.
import assert from "node:assert/strict";
import { layout, truncate } from "../web/graph.js";

let pass = 0;
const failures = [];
function check(label, fn) {
  try {
    fn();
    pass += 1;
    console.log("  ok   " + label);
  } catch (err) {
    failures.push(label + " :: " + err.message);
    console.log("  FAIL " + label + " :: " + err.message);
  }
}

console.log("\n[layout] linear history");
const linear = [
  { sha: "c3", parents: ["c2"] },
  { sha: "c2", parents: ["c1"] },
  { sha: "c1", parents: [] },
];
const l = layout(linear);
check("one lane", () => assert.equal(l.laneCount, 1));
check("rows follow input order", () => assert.deepEqual(l.nodes.map((n) => n.row), [0, 1, 2]));
check("two edges", () => assert.equal(l.edges.length, 2));
check("root frees its lane", () => assert.equal(l.bySha.get("c1").lane, 0));

console.log("\n[layout] merge and shared ancestor");
const merged = [
  { sha: "m", parents: ["a", "b"] },
  { sha: "a", parents: ["base"] },
  { sha: "b", parents: ["base"] },
  { sha: "base", parents: [] },
];
const m = layout(merged);
check("two lanes for the merge", () => assert.equal(m.laneCount, 2));
check("second parent gets its own lane", () => assert.equal(m.bySha.get("b").lane, 1));
check("shared ancestor returns to the trunk", () => assert.equal(m.bySha.get("base").lane, 0));
check("merge edge marked primary", () => {
  const fromM = m.edges.filter((e) => e.from.sha === "m");
  assert.equal(fromM.length, 2);
  assert.equal(fromM.filter((e) => e.primary).length, 1);
});

console.log("\n[layout] branch drawn beside trunk, parent outside the window");
const branched = [
  { sha: "t3", parents: ["t2"] },
  { sha: "b2", parents: ["t1"] },
  { sha: "t2", parents: ["t1"] },
  { sha: "t1", parents: ["missing"] },
];
const b = layout(branched);
check("two lanes", () => assert.equal(b.laneCount, 2, ));
check("edges to commits outside the window are dropped", () => assert.equal(b.edges.filter((e) => e.from.sha === "t1").length, 0));
check("no duplicate lanes for a shared parent", () => {
  const lanesOfT1 = b.nodes.filter((n) => n.sha === "t1").map((n) => n.lane);
  assert.equal(lanesOfT1.length, 1);
});

console.log("\n[truncate] subjects stay on one line");
check("collapses whitespace", () => assert.equal(truncate("a\n\nb   c", 20), "a b c"));
check("truncates with dots", () => assert.equal(truncate("abcdefghij", 6), "abc..."));
check("keeps short text", () => assert.equal(truncate("short", 10), "short"));
check("handles undefined", () => assert.equal(truncate(undefined, 5), ""));

console.log("\n[layout] empty input does not explode");
check("zero commits", () => {
  const empty = layout([]);
  assert.equal(empty.nodes.length, 0);
  assert.equal(empty.laneCount, 1);
});

console.log(`\n${pass} checks passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
