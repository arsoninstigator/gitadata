// lane layout for a commit dag. pure: no dom, no fetch, testable under node.
//
// input: commits newest first, each {sha, parents: [sha, ...]}
// output: nodes with {sha, row, lane}, edges child -> parent, lane count

export function layout(commits) {
  const nodes = [];
  const bySha = new Map();
  const lanes = [];

  commits.forEach((commit, row) => {
    let lane = lanes.indexOf(commit.sha);
    if (lane === -1) {
      lane = lanes.indexOf(null);
      if (lane === -1) lane = lanes.length;
      lanes[lane] = commit.sha;
    }
    const node = { sha: commit.sha, row, lane, commit };
    nodes.push(node);
    bySha.set(commit.sha, node);

    const [first, ...rest] = commit.parents || [];
    lanes[lane] = first ?? null;
    rest.forEach((parent) => {
      if (lanes.indexOf(parent) === -1) {
        let free = lanes.indexOf(null);
        if (free === -1) free = lanes.length;
        lanes[free] = parent;
      }
    });
  });

  const edges = [];
  commits.forEach((commit) => {
    const from = bySha.get(commit.sha);
    (commit.parents || []).forEach((parent, index) => {
      const to = bySha.get(parent);
      if (!to) return; // parent outside the window we asked for
      edges.push({ from, to, primary: index === 0 });
    });
  });

  const laneCount = nodes.reduce((max, node) => Math.max(max, node.lane + 1), 1);
  return { nodes, edges, laneCount, bySha };
}

export function truncate(text, max) {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return clean.slice(0, Math.max(0, max - 3)) + "...";
}
