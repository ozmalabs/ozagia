// ozAGIa TUI brand for OpenCode 2.3: renders "[ + ozAGIa ]" (green) centred
// under the main logo on the home screen.
//
// OpenCode 2.3 publishes no logo slot (`ctx.ui.slot` has home.footer and
// friends, nothing at the logo), so this takes the render tree directly: it
// walks `ctx.renderer.root` (the OpenTUI root renderable) to find the home
// logo, then injects a text renderable -- the same class as the logo's own
// cells, borrowed from a live node -- absolutely positioned in the free row
// between the logo and the prompt box. It re-anchors when the logo moves
// (terminal resize) and re-injects when the home screen is rebuilt.
//
// Fails silent: every step is guarded; errors go to
// `.opencode/ozagia-brand.log` in the project directory and the base TUI is
// left untouched.
//
// API notes, verified empirically against the 2.3.0 binary:
// - renderables expose getChildren()/getChildrenCount()/add(child)/
//   remove(child); children are not a plain `children` property.
// - the text renderable constructor is (rendererCtx, { id, content, ... });
// - PositionType is an integer enum: 2 is absolute (0 static, 1 relative,
//   3 fixed, verified by where a probe node lands).
// - setPosition takes an object ({ left, top, ... }); a position set before
//   the node is attached zeroes its layout.
// - ctx.theme.hue.green["200"] is the theme green (#7FD88F in the default
//   theme); the host quantizes it to the terminal's palette.
import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";

const BRAND = "[ + ozAGIa ]";
const NODE_ID = "ozagia-brand";
const ABSOLUTE = 2; // OpenTUI PositionType enum, verified empirically on 2.3.0

function log(line) {
  try {
    const dir = path.join(process.cwd(), ".opencode");
    mkdirSync(dir, { recursive: true });
    appendFileSync(path.join(dir, "ozagia-brand.log"), line + "\n");
  } catch {}
}
const safe = (fn) => { try { return fn(); } catch (e) { return `!ERR ${e && e.message}`; } };

function childrenOf(node) {
  try {
    if (typeof node.getChildren === "function") {
      const c = node.getChildren();
      if (Array.isArray(c)) return c;
      if (c && typeof c[Symbol.iterator] === "function") return [...c];
    }
  } catch {}
  return [];
}
function walk(node, depth, fn, budget) {
  if (!node || depth > 16 || budget.n > 2000) return;
  budget.n++;
  fn(node, depth);
  for (const c of childrenOf(node)) walk(c, depth + 1, fn, budget);
}

// Find the home-screen logo. Returns { container, minX, maxX, mainMaxY } or
// null. container = the box that holds the logo AND the prompt box (has a
// textarea descendant). mainMaxY = bottom row of the logo, so the brand lands
// in the free row between logo and prompt box. minX..maxX is the logo's
// horizontal extent: the brand is centred under it.
//
// The logo is the only text drawn one renderable per character, so find it by
// shape: at least three stacked rows of cell-sized text, overlapping
// horizontally, centred in the pane that holds the prompt. It must not be
// found by screen position: the 2.3 home layout centres the logo, so on a
// large terminal it sits near the middle of the screen (e.g. x=129, y=39 at
// 297x90) while a small one puts it at (21, 6). An earlier fixed window of
// x 8..72, y <= 12 only matched small terminals and silently did nothing on
// large ones.
const MIN_LOGO_CELLS = 4;  // the compact logo's rows are 4 cells wide
const MIN_LOGO_ROWS = 3;   // compact layouts drop the logo's blank top row
const CENTER_SLACK = 6;    // cells the centred logo may drift

function findLogo(root) {
  const cells = [];
  const budget = { n: 0 };
  walk(root, 0, (node) => {
    try {
      const id = node.id ?? "";
      if (!id.startsWith("text-")) return;
      const x = node.x, y = node.y;
      if (typeof x !== "number" || typeof y !== "number") return;
      cells.push({ x, y, node });
    } catch {}
  }, budget);
  if (!cells.length) return null;
  const byRow = new Map();
  for (const c of cells) {
    if (!byRow.has(c.y)) byRow.set(c.y, []);
    byRow.get(c.y).push(c);
  }
  // Candidate logo rows: enough cells at distinct columns. (Several text
  // nodes can stack at one x while a surface is hidden; they do not count.)
  const rows = [];
  for (const [y, list] of byRow) {
    const xs = [...new Set(list.map((c) => c.x))];
    if (xs.length < MIN_LOGO_CELLS) continue;
    rows.push({ y, minX: Math.min(...xs), maxX: Math.max(...xs), node: list[0].node });
  }
  rows.sort((a, b) => a.y - b.y);
  // The logo is a run of consecutive rows that overlap in x. Keep every run:
  // the longest one is usually the logo, but a graphic elsewhere must not hide
  // a shorter valid logo.
  const runs = [];
  let run = [];
  const consider = () => {
    if (run.length < MIN_LOGO_ROWS) return;
    runs.push({
      rows: run.length,
      minX: Math.min(...run.map((r) => r.minX)),
      maxX: Math.max(...run.map((r) => r.maxX)),
      maxY: Math.max(...run.map((r) => r.y)),
      node: run[0].node,
    });
  };
  for (const r of rows) {
    const prev = run[run.length - 1];
    if (prev && r.y === prev.y + 1 && r.minX <= prev.maxX && r.maxX >= prev.minX) {
      run.push(r);
    } else {
      consider();
      run = [r];
    }
  }
  consider();
  runs.sort((a, b) => b.rows - a.rows || a.maxY - b.maxY);

  const hasTextarea = (n) => {
    const b = { n: 0 };
    let found = false;
    const go = (m) => {
      if (!m || b.n > 3000 || found) return;
      b.n++;
      if ((m.id ?? "").startsWith("textarea-")) { found = true; return; }
      for (const k of childrenOf(m)) go(k);
    };
    go(n);
    return found;
  };
  for (const best of runs) {
    let n = best.node;
    for (let i = 0; i < 8 && n; i++) {
      n = n.parent;
      if (!n) break;
      if (!hasTextarea(n)) continue;
      // Guard against a transcript that happens to stack cell-sized text
      // nodes: the home logo is centred in the pane that holds the prompt.
      const x0 = typeof n.x === "number" ? n.x : 0;
      const w = typeof n.width === "number" ? n.width : 0;
      if (w > 0 &&
          Math.abs((best.minX + best.maxX) / 2 - (x0 + w / 2)) > CENTER_SLACK) {
        break;
      }
      return { container: n, minX: best.minX, maxX: best.maxX, mainMaxY: best.maxY };
    }
  }
  return null;
}

function pickGreen(ctx) {
  try {
    const g = ctx && ctx.theme && ctx.theme.hue && ctx.theme.hue.green && ctx.theme.hue.green["200"];
    if (g) return g;
  } catch {}
  // OpenCode default success green #7FD88F
  return { buffer: { 0: 127, 1: 216, 2: 143, 3: 255 } };
}

const plugin = {
  id: "ozagia-brand",
  setup(ctx) {
    let timer = null;
    let brand = null;
    let brandParent = null;
    let lastPos = null;
    let textCtor = null;

    function reposition(node, x, y) {
      node.setPosition({ left: x, top: y });
    }

    function tick() {
      const r = ctx && ctx.renderer;
      if (!r || !r.root) return;
      const logo = findLogo(r.root);
      if (!logo) {
        // not on the home screen: drop the reference if the container died
        if (brand && brandParent) {
          try {
            if (brandParent._isDestroyed) { brand = null; brandParent = null; lastPos = null; }
          } catch { brand = null; brandParent = null; lastPos = null; }
        }
        return;
      }
      const { container, minX, maxX, mainMaxY } = logo;
      // setPosition is parent-relative; logo coordinates are absolute. The
      // brand is centred under the logo: the wordmark is 39 cells wide on a
      // large terminal, so starting at the logo's left edge left the 12-cell
      // label visibly off to one side. The extra cell of an odd difference
      // goes to the right (floor).
      const logoWidth = maxX - minX + 1;
      const indent = Math.max(0, Math.floor((logoWidth - BRAND.length) / 2));
      const x = minX + indent - (typeof container.x === "number" ? container.x : 0);
      const y = mainMaxY + 1 - (typeof container.y === "number" ? container.y : 0);
      const posKey = `${x},${y}`;
      const attached = brand && brandParent === container && !brand._isDestroyed;
      if (attached && lastPos === posKey) return; // in place, nothing to do

      if (brand && brandParent && brandParent !== container) {
        // the home screen was rebuilt around a new container: drop the old node
        try { if (!brand._isDestroyed) brandParent.remove(brand); } catch {}
        brand = null;
        brandParent = null;
        lastPos = null;
      }

      if (attached) {
        // the logo moved (e.g. resize): re-place the existing node
        try {
          reposition(brand, x, y);
          lastPos = posKey;
          return;
        } catch (e) {
          log("reposition failed: " + e.message);
          try { container.remove(brand); } catch {}
          brand = null;
          brandParent = null;
        }
      }
      // create a fresh node, borrowing the text renderable class from a live
      // cell (the class is not importable: OpenTUI is baked into the binary)
      if (!textCtor) {
        const probe = { n: 0 };
        let c = null;
        walk(r.root, 0, (node) => {
          if (c || probe.n > 800) return;
          probe.n++;
          if ((node.id ?? "").startsWith("text-")) c = node;
        }, probe);
        if (!c) return;
        textCtor = c.constructor;
      }
      let node;
      try {
        node = new textCtor(r, { id: NODE_ID, content: BRAND });
      } catch (e) {
        log("construct failed: " + e.message);
        return;
      }
      try {
        try { node.fg = pickGreen(ctx); } catch (e) { log("fg failed: " + e.message); }
        // add first: a position set before the node has a scene handle
        // zeroes its layout
        container.add(node);
        node.setPositionType(ABSOLUTE);
        node.setPosition({ left: x, top: y });
        brand = node;
        brandParent = container;
        lastPos = posKey;
        log(`injected at (${x},${y})`);
      } catch (e) {
        log("inject failed: " + e.message);
        try { node.destroy && node.destroy(); } catch {}
        brand = null;
        brandParent = null;
      }
    }

    // Poll: a bounded walk, only acts when the home logo is (re)present.
    const loop = () => {
      try { tick(); } catch (e) { log("tick: " + (e && e.stack ? e.stack : e.message)); }
      timer = setTimeout(loop, 800);
      if (timer && timer.unref) timer.unref();
    };
    timer = setTimeout(loop, 400);
    if (timer && timer.unref) timer.unref();
    log("setup done");
    return () => {
      try { if (timer) clearTimeout(timer); } catch {}
      try {
        if (brand && brandParent && !brand._isDestroyed) brandParent.remove(brand);
      } catch {}
      brand = null;
      brandParent = null;
    };
  },
};
export default plugin;
