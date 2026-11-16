// ozAGIa TUI brand for OpenCode 2.3: renders "[ + ozAGIa ]" (green) under the
// main logo on the home screen.
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

// Find the home-screen logo. Returns { container, minX, mainMaxY } or null.
// container = the box that holds the logo AND the prompt box (has a textarea
// descendant). mainMaxY = bottom of the substantial logo rows (>= 10 cells),
// so the brand lands in the free row between logo and prompt box.
function findLogo(root) {
  const cells = [];
  const budget = { n: 0 };
  walk(root, 0, (node) => {
    try {
      const id = node.id ?? "";
      if (!id.startsWith("text-")) return;
      const x = node.x, y = node.y;
      if (typeof x !== "number" || typeof y !== "number") return;
      if (y < 0 || y > 12 || x < 8 || x > 72) return;
      cells.push({ x, y, node });
    } catch {}
  }, budget);
  if (!cells.length) return null;
  const byRow = new Map();
  for (const c of cells) {
    if (!byRow.has(c.y)) byRow.set(c.y, []);
    byRow.get(c.y).push(c);
  }
  const main = [];
  for (const list of byRow.values()) if (list.length >= 10) main.push(...list);
  if (!main.length) return null;
  const minX = Math.min(...main.map((c) => c.x));
  const mainMaxY = Math.max(...main.map((c) => c.y));
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
  let n = main[0].node;
  for (let i = 0; i < 8 && n; i++) {
    n = n.parent;
    if (!n) break;
    if (hasTextarea(n)) return { container: n, minX, mainMaxY };
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
      const { container, minX, mainMaxY } = logo;
      const x = minX;
      const y = mainMaxY + 1;
      const posKey = `${x},${y}`;
      const attached = brand && brandParent === container && !brand._isDestroyed;
      if (attached && lastPos === posKey) return; // in place, nothing to do

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
