# Ozma Protocol v5.13 — Authoring device profiles

**Status:** RFC (draft, revision 2)
**Slot:** additive to v5.12 at the grammar level (flat verb-line body shape preserved); adds new ref sigils, new verb, and new device-profile category.
**Depends on:** v5.12 (unified MOTION grammar + `§SIGIL` ref dialects), existing `{@HASH}` content-addressed chunk references (§anaphoric references), existing shim-declared renderers (`mermaid-to-png`, `latex-to-png`, `plotly-to-png`, `svg-to-raster`, `html-to-image`, `vrm-to-video`), GRAPH / PLOTLY / LATEX / SVG / HTML structured output types.

## 1. Motivation

LLMs already try to produce images and documents today — by writing SVG, LaTeX, Mermaid, Markdown, and occasionally hand-building HTML. Results are poor because the production is *always one-shot into a textual format*, with no edit round-trip, no layout engine behind the output, and no linkage back to existing structured data the model has already produced in-session. The same model also can't produce presentations, spreadsheets, or 3D scenes at all, except by emitting file formats it is not trained to emit cleanly.

v5.12 just locked the MOTION grammar into one shape: `motion [<device>]:` with a device-profile-declared body. That shape is already the right container for *authoring* — a canvas, a document, a slide deck, a spreadsheet, and a 3D scene are each "a controllable system with a manipulator, an input stream, and an observation surface," which is the MOTION contract.

v5.13 adds **five authoring device profiles** under MOTION, plus new ref sigils (`§ID`, `§SLIDE`) and one new verb (`embed`), and retrofits authoring into the existing shim-declared-renderer pattern instead of inventing a parallel one. The body shape stays flat verb-line family — no nested blocks, no multi-line string continuation — so v5.12's grammar carries unchanged.

## 2. Summary of additions

- **Six new device profiles** under MOTION:
  - `motion [canvas]:` — single-page vector image (PNG / SVG / single-page PDF)
  - `motion [document]:` — multi-page flowing document (`.docx` canonical)
  - `motion [slides]:` — presentation (`.pptx` canonical)
  - `motion [sheet]:` — spreadsheet (`.xlsx` canonical)
  - `motion [scene]:` — 3D scene (glTF/GLB canonical)
  - `motion [diagram]:` — graph diagram (`.drawio` / `.drawio.svg` canonical; draw.io / mxGraph backend)
- **Seven new ref sigils:**
  - `§ID("name")` — element handle within an artifact *or within a sense observation* (region ids); joins `§SOM[N]` / `§CSS("…")` / `§XPath("…")` / `§AID("…")` as peer.
  - `§SLIDE(N)` — optional scope filter for slide-addressing (does not create a new namespace; see §4).
  - `§IMG(N)` — turn-scoped image-payload index, for sense profiles (§3.7).
  - `§CONV("handle")` — session-scoped handle for an in-flight or recently-completed convergence loop (§3.9).
  - `§SECRET("name")` — tier-0 opaque handle; model never sees the value, shim dereferences only at declared egress scope (§3.10.1).
  - `§PII("handle", kind=…[, jurisdiction=…])` — tier-1 processable-but-egress-filtered classified value; label propagates through every operation (§3.10.2).
  - `§CONF("handle"[, class=…][, tenant=…])` — tier-2 internal-routed classified value; tenant label enforces cross-tenant prohibition (§3.10.3).
- **New cross-device primitive `converge` (§3.9)** — the one load-bearing primitive every future world-interface adapter reuses (robot / nav / game / 3d / map / retrieval / grounding / optimization). Shim owns the inner closed-loop; model sets goal + termination criteria + optional method hint; terminal observation attaches on completion with outcome, final state, trajectory digest, system-identification summary, and cost. Ancillary verbs `abort` / `status` / `await` for async handles. Methods declared in `shim_profile.yaml` with `applicable_to` / `goal_kinds` / `certifiable` — deployments pick methods matching their regulatory context (DO-178C / ISO 26262 / IEC 61508 / IEC 62304). Biological parallel: neocortex reads terminal state; spinal reflex owns the loop.
- **Information-flow control lattice (§3.10).** Every value the protocol carries acquires a classification: `§SECRET` (tier 0 opaque — model never sees the value, shim dereferences at egress only; precedent: PCI/EMV tokenisation), `§PII` (tier 1 processable + jurisdiction-gated — model reads, shim enforces destination allowlist and propagates the label through paraphrase / summary / translation), `§CONF` (tier 2 internal — tenant-scoped routing). Labels propagate as lattice-join across shim-mediated operations; declassification is shim-owned and always logged. New `validate [classify]:` sibling enforces at egress. Labels propagate through `trajectory_digest`, training-corpus extraction, and `compress_context` / Ozma IR literal emission. Makes GDPR / HIPAA / PCI compliance structural rather than procedural; makes multi-tenant isolation a shim concern rather than a prompt-engineering prayer. Positioning against 2025 prior art (CaMeL, FIDES) in §3.10.6: not first IFC-for-agents, but first to treat sense / validate / trajectory / training / compression as a single lattice-enforcement surface.
- **Efficiency-surface family opened (§3.7 + §3.8).** Symmetric shim passes around the model: `sense [...]` on input (§3.7), `validate [...]` on output (§3.8). Both already ship in ozma (compress_context + hints on input; checks + rules on output); v5.13 names them in the protocol so the schema becomes a training contract.
  - **Sense (input) — §3.7.1 `sense [prompt]:`** names what ozma's hints / compress_context / classify_task already do (intent classification, entity extraction, chunk hashing + Ozma-IR stub view, hint firing, migration detection). Observation schema JSON. Reserved verbs: `expand §ID("c1")`, `reclassify intent=…`, `suppress hint=…`.
  - **Sense (input) — §3.7.2 `sense [vision]:`** first pixel-modality adapter. Tier ladder (0 Pillow-only → 1 cheap CV → 2 model-backed → 3 VLM). Regions with mandatory per-field `confidence`; `available: true|false` envelope.
  - **Validate (output) — §3.8.1 `validate [checks]:`** regex checks with `warn` / `resubmit` / `fix` / `run` / `block` actions. 15 in production.
  - **Validate (output) — §3.8.2 `validate [rules]:`** extended-action rules — `suggest` / `fetch_spec` / `score` / `run`. 64 in production.
  - **Reserved siblings:** `sense [audio]:` / `sense [video]:` / `sense [log]:` / `sense [sensor]:` (follow-on RFCs). Sense blocks auto-attach; validate blocks auto-attach. Backends (detectors, OCR, check definitions, rule actions) are declared in `shim_profile.yaml`; the protocol fixes only verbs, schema, and tier/confidence/null conventions.
- **One new verb:** `embed {@HASH} at <rect>` — composites a content-addressed chunk (GRAPH, PLOTLY, LATEX, image/*, table, glTF) into the artifact by dispatching to an existing shim renderer. Dispatch mapping is declared in `shim_profile.yaml`, not spec-fixed.
- **One new `shim_profile.yaml` section:** `authoring:` — declares canvas / document / slides / sheet / scene authoring backends with the same cost/latency envelope shape as existing `renderers:`. Existing `renderers:` entries gain an `accepts:` field declaring content types they handle.
- **Edit semantics:** artifacts are durable, keyed state; verbs mutate them via `§ID` references across turns.
- **Observation auto-attachment:** each authoring profile declares `auto_preview: none | tree | render | render+overlay`; the shim attaches the declared observation to each turn's echo, matching v5.12's SOM-after-desktop-action pattern.
- **Flat verb-line body.** No nested blocks. Composition uses `parent=§ID("…")` attribute on children. Same body family as desktop/browser/mobile in v5.12.
- **String escapes.** All quoted strings follow JSON-string rules: `\"`, `\\`, `\n`, `\t`, `\u<hex>`. One delimiter (`"…"`); no alternative forms.
- **Compound verb naming.** Multi-word verbs use underscores, matching v5.12 convention (`tab_new`, `launch_app`): `slide_new`, `slide_edit`, `slide_delete`, `slide_reorder`, `sheet_new`, `sheet_delete`, `sheet_rename`.
- **Style and material are attributes, not verbs.** `fill`, `stroke`, `font`, `opacity`, `dash`, `gradient` (canvas) and `color`, `metallic`, `roughness`, `emissive`, `texture` (scene) are attributes on the element-creating verb, not standalone stateful verbs. Keeps the interpreter stateless and LLM reasoning local.

Format choice (`.docx` vs `.pdf` vs Google Doc vs `.odt` vs print; `.glb` vs `.usd`) is **out of protocol scope** — frontend concern at save/export time.

## 3. Device profiles

### 3.1 `motion [canvas]:` — single-page vector image

**Backend:** Cairo (draw) + Pango (text) + Taffy via `stretchable` (layout math).
**Canonical output:** PNG / SVG / **single-page** PDF. Multi-page PDFs live in `document`.
**Ref dialect:** `§ID("name")`.
**Body shape:** flat verb-line.

Verbs fall into four groups; **style is expressed as attributes on each verb**, not as standalone verbs:

| Group | Verbs |
|---|---|
| **Drawing** | `rect`, `circle`, `ellipse`, `line`, `polygon`, `path`, `arrow`, `star`, `arc`, `text`, `image` |
| **Layout** | `stack`, `columns`, `grid`, `center`, `pad`, `flow` |
| **Transform** | `group`, `clip` (containers that apply transform/clip to children) |
| **Embed** | `embed {@HASH} at <rect>` |

**Style attributes** (apply to any drawing verb unless noted):

| Attribute | Form | Meaning |
|---|---|---|
| `fill=` | `fill="#rrggbb"` / `fill="none"` / `fill="url(gradient-id)"` / `fill="pattern(src)"` | Interior color, gradient, or image pattern |
| `stroke=` | `stroke="#rrggbb"` / `stroke="none"` | Outline color |
| `stroke_width=` | `stroke_width=2` | Outline width in units |
| `stroke_cap=` | `butt\|round\|square` | Cairo line cap |
| `stroke_join=` | `miter\|round\|bevel` | Cairo line join |
| `opacity=` | `opacity=0.5` | 0.0–1.0 (composite-level) |
| `blend=` | `over\|multiply\|screen\|overlay\|darken\|lighten\|…` | Cairo composite operator |
| `dash=` | `dash="4,2"` | Dash pattern |
| `font=` / `size=` / `align=` | text only | Pango family, font size / box size, alignment |
| `markup=true` | text only | Interpret the body as Pango markup |
| `letter_spacing=` / `line_height=` | text only | Pango letter-spacing (1/1024 pt) / leading |
| `text_stroke=` / `text_shadow=` | text only | Stroke + fill text; `dx dy blur color` shadow |
| `radius=` | rect only | Corner radius (rounded rectangles) |
| `inner_radius=` / `points=` | star only | Star inner radius + number of points |
| `start=` / `end=` / `closed=` | arc only | Start/end angles (radians) + pie-slice flag |
| `brightness=` / `contrast=` / `saturation=` / `hue=` / `grayscale=` / `sepia=` / `invert=` / `crop=` | image only | Pillow-backed filters |

**Transform attributes** (apply to any element — drawing, layout, or container):

| Attribute | Form | Meaning |
|---|---|---|
| `rotation=` | `rotation=30` | Degrees clockwise about `pivot` (default: element centre) |
| `scale=` | `scale=1.5` / `scale=(1.5,2.0)` | Uniform or (sx, sy) scale about `pivot` |
| `flip=` | `flip=h\|v\|hv` | Mirror horizontally, vertically, or both |
| `pivot=` | `pivot=(cx,cy)` | Override transform origin (absolute canvas coords) |
| `shadow=` | `shadow="dx dy blur color"` | Drop shadow (offscreen pass; shadow colour accepts alpha) |
| `blur=` | `blur=4` | Gaussian blur radius in pixels (Pillow) |
| `glow=` | `glow="blur color"` | Outer glow (shadow with dx=dy=0) |
| `z=` | `z=10` | Stacking order within the same sibling group (higher draws later) |
| `frame=` | `frame=§ID("mask")` | Clip this element to another element's shape |

**Gradients and patterns.** Gradients are declared separately and referenced by id:

```
gradient id="sky" linear from=(0,0) to=(0,100) stops=[(0,"#9cf"),(1,"#fff")]
gradient id="sun" radial center=(50,50) r=80 stops=[(0,"#fff7"),(1,"#fb03")]
rect at 0 0 size=800x200 fill="url(sky)"
```

Patterns use `fill="pattern(src)"` with `pattern_src="<path-or-{@hash}>"`; the source image tiles with `EXTEND_REPEAT`.

**Containers.** Layout verbs (`stack`, `columns`, `grid`, `center`, `pad`, `flow`) size and place children; children declare their parent via `parent=§ID("container")`. `group` is a transform-only container (no layout) — children position themselves, but transforms on the group apply to the whole group. `clip` is the same shape but clips rendering to its own bounding shape (`shape=rect\|circle\|ellipse`, optionally `radius=`).

**Export.** `save path="<file>"` dispatches on extension: `.png` (Cairo ImageSurface), `.svg` (hand-emitted with `<g id="<element-id>">` per element — see §8), `.pdf` (Cairo PDFSurface, single-page), `.jpeg`/`.jpg` (via PNG→Pillow). See §7 for placement semantics (absolute vs flow).

Example:

```
motion [canvas, artifact="arch-diag", size=800x600]:
  text "System architecture" at 400 30 size=24 align=center id="title"
  stack id="body" vertical gap=20 at 50 80 size=700x500
  embed {@a4f9c1d2} parent=§ID("body") size=700x300      # GRAPH → mermaid-to-png
  columns id="legend" parent=§ID("body") count=2 gap=40
  text "Frontend: React SPA"  parent=§ID("legend") size=12
  text "Backend: FastAPI + Postgres" parent=§ID("legend") size=12
  embed {@c9b2e4a1} parent=§ID("body") size=700x200      # PLOTLY → plotly-to-png
```

### 3.2 `motion [document]:` — multi-page flowing document

**Backend:** python-docx (canonical `.docx` object tree). PDF via ReportLab Platypus for write-only emission.
**Canonical output:** `.docx`. `.pdf` on explicit frontend request.
**Ref dialect:** `§ID("name")`.
**Body shape:** flat verb-line.

Primitives:

| Group | Verbs |
|---|---|
| **Structure** | `heading <N>`, `paragraph`, `list`, `page_break`, `section` |
| **Inline** | `bold`, `italic`, `link`, `code`, `footnote` (applied via `style=` attribute on parent) |
| **Block** | `table`, `image`, `blockquote`, `hr` |
| **Edit** | `edit §ID(x) …`, `insert before\|after §ID(x) …`, `delete §ID(x)`, `move §ID(x) to …` |
| **Embed** | `embed {@HASH} width=…` |

Long-form paragraph text uses a single double-quoted string; no multi-line continuation (see §12 EBNF).

Example (create + edit across two turns):

```
# Turn 1 — create
motion [document, artifact="q2-report"]:
  heading 1 "Q2 Report" id="title"
  paragraph "This report summarises Q2 performance across three business lines." id="intro"
  embed {@a4f9c1d2} width=500 id="revenue_chart"
  heading 2 "Methodology" id="methodology"
  paragraph "Revenue is recognised on a cash basis." id="method-1"

# Turn 2 — edit after user feedback
motion [document, artifact="q2-report"]:
  edit §ID("intro") replace "This report summarises Q2 performance across four business lines, including the new Federal segment."
  insert after §ID("revenue_chart") paragraph "Federal contributed 12% of Q2 revenue." id="federal-note"
  delete §ID("method-1")
```

Pagination, orphan/widow control, TOC rendering, column balancing are handled by the adapter — the LLM does not manage page boundaries.

**Bidirectional ingest.** The document adapter ships the inverse path: `Document.from_docx(path)` reads an existing `.docx` back into the block model — `.docx` → Document → motion body. Supported with full fidelity: headings (level 1–6), paragraphs, bulleted/numbered lists, blockquote, hr (bottom-bordered paragraph), page_break, section orientation, tables (cells grid), inline images (blob + sha256 hash, registered via a `chunk_sink` callback so the caller owns persistence; the returned `image` block carries `src=("hash", h)` so the LLM sees a `{@hash}` ref), and paragraph-wide run style (`bold`, `italic`, `underline`, `code`) *when every run in the paragraph agrees on the token*. Mixed-run paragraphs collapse to plain text and carry `_fidelity="partial_runs"` so callers can audit lossy ingests. `Document.to_motion_body()` then emits the block sequence as re-parseable motion verb-line text — inverse of `apply()`. This closes the round-trip: motion → .docx → motion produces an equivalent tree, so an LLM handed an existing user `.docx` can edit it with full §ID reference fidelity instead of receiving a text blob stripped of structure.

### 3.3 `motion [slides]:` — presentation

**Backend:** python-pptx (canonical `.pptx` object tree).
**Canonical output:** `.pptx`. `.pdf` / images via LibreOffice export.
**Ref dialect:** `§ID("name")` (per-artifact unique); `§SLIDE(N)` as optional scope filter.
**Body shape:** flat verb-line.

Primitives:

| Group | Verbs |
|---|---|
| **Deck** | `theme`, `master`, `slide_size` |
| **Slide** | `slide_new`, `slide_edit`, `slide_delete`, `slide_reorder` |
| **Content** | `title`, `subtitle`, `bullet`, `text`, `image`, `table`, `shape`, `chart` |
| **Embed** | `embed {@HASH} at …` |

Slide operations use underscored compound-verb names (matching v5.12's `tab_new` / `launch_app` convention), so the grammar stays a single-token-per-verb flat family.

Example:

```
motion [slides, artifact="kickoff-deck", theme="dark"]:
  slide_new layout=title id="cover"
  title "Q2 Kickoff" parent=§ID("cover")
  subtitle "Planning + priorities" parent=§ID("cover")

  slide_new layout=content id="roadmap"
  title "Roadmap" parent=§ID("roadmap")
  bullet "Launch v2" parent=§ID("roadmap")
  bullet "Migrate legacy" parent=§ID("roadmap")
  bullet "Federal segment onboarding" parent=§ID("roadmap")

  slide_new layout=chart id="revenue"
  title "Revenue split" parent=§ID("revenue")
  embed {@c9b2e4a1} parent=§ID("revenue") size=900x500

  # Edit across turns
  insert after §ID("roadmap-bullet-3") bullet "Contingency buffer" parent=§ID("roadmap")
```

`parent=§SLIDE(N)` is also accepted as an alternative to `parent=§ID(...)` when the LLM knows the slide index but not its assigned id — e.g., `bullet "..." parent=§SLIDE(2)` places the bullet at the top level of slide 2.

### 3.4 `motion [sheet]:` — spreadsheet

**Backend:** openpyxl (canonical `.xlsx` workbook).
**Canonical output:** `.xlsx`. CSV / TSV via adapter; `.pdf` via LibreOffice export.
**Ref dialect:** A1 notation (`A1`, `B2:D10`) — no `§ID` needed; A1 is already universal.
**Body shape:** flat verb-line.

Primitives:

| Group | Verbs |
|---|---|
| **Sheets** | `sheet_new "name"`, `sheet_delete "name"`, `sheet_rename old=… new=…` |
| **Cells** | `set <A1> = value`, `set <range> = {values}`, `clear <range>` |
| **Formulas** | `formula <A1> = "=SUM(...)"` |
| **Format** | `format <range> bold\|italic\|…`, `format <range> num_fmt="…"`, `color`, `align`, `border` |
| **Structure** | `merge <range>`, `unmerge`, `freeze <cell>`, `width col=A val=20`, `height row=1 val=30` |
| **Data** | `paste <A1> = {@HASH}` — writes a TABLE/tsv chunk starting at cell |

Example:

```
motion [sheet, artifact="q2-budget"]:
  sheet_new "Expenses"
  set A1 = "Category"
  set B1 = "Amount"
  format A1:B1 bold
  paste A2 = {@a9d4f1c7}
  formula B20 = "=SUM(B2:B19)"
  format B2:B20 num_fmt="$#,##0.00"
  format B20 border=top bold
```

### 3.5 `motion [scene]:` — 3D scene

**Backend:** `trimesh` (geometry composition) for glTF/GLB canonical; `pyrender` for offscreen preview; `Blender --background` as quality fallback.
**Canonical output:** `.glb` primary; `.gltf` / `.obj` / `.usd` / `.stl` via adapter (LibreOffice does not apply).
**Ref dialect:** `§ID("name")`.
**Body shape:** flat verb-line.

**Coordinate convention.** Right-handed, Y-up by default (glTF native). Units are meters. Override via artifact header: `motion [scene, up=z, units=cm]:`. On save, the adapter applies the matching glTF axis/unit transform.

Verbs (material is attributes, not verbs):

| Group | Verbs |
|---|---|
| **Geometry** | `cube`, `sphere`, `cylinder`, `cone`, `torus`, `plane`, `mesh` |
| **Transform** | `translate`, `rotate`, `scale` (applied to any element-creating verb via attributes; or as standalone verbs targeting `§ID(x)` for edits) |
| **Light** | `light_point`, `light_directional`, `light_ambient` |
| **Camera** | `camera` |
| **Edit** | `edit §ID(x) …`, `insert …`, `delete §ID(x)` |
| **Embed** | `embed {@HASH} at=(x,y,z)` — glTF chunk composites as sub-scene; image chunk becomes a textured plane |

**Material / transform attributes** (apply to any geometry verb):

| Attribute | Form | Meaning |
|---|---|---|
| `color=` | `color="#c33"` | Base/albedo color |
| `metallic=` | `metallic=0.3` | PBR metallic factor (0–1) |
| `roughness=` | `roughness=0.4` | PBR roughness factor (0–1) |
| `emissive=` | `emissive="#ffaa00"` | Emission color |
| `texture=` | `texture={@HASH}` | Image chunk as albedo texture |
| `at=` | `at=(x,y,z)` | Absolute world translation |
| `rotation=` | `rotation=(rx,ry,rz)` | Euler rotation (radians) |
| `scale=` | `scale=(sx,sy,sz)` or `scale=1.5` | Per-axis or uniform |
| `parent=` | `parent=§ID("group")` | Parent in scene graph |

Example:

```
motion [scene, artifact="robot-demo"]:
  light_ambient intensity=0.3 id="amb"
  light_directional dir=(-1,-1,-1) intensity=1.0 id="sun"

  cube size=(10,0.1,10) color="#888" id="floor"
  sphere radius=1 at=(0,1,0) color="#c33" metallic=0.3 roughness=0.4 id="ball"

  embed {@b7e2a9d1} at=(2,0,0) id="robot"                  # a glTF chunk → sub-scene

  camera position=(5,3,5) target=(0,1,0) fov=50 id="cam1"
```

Animation, physics, and skinning are out of scope for v5.13 (static scenes only; see §14).

### 3.6 `motion [diagram]:` — graph diagram (draw.io backend)

**Backend:** mxGraph / draw.io. Canonical output is `.drawio` (mxfile XML); `.drawio.svg` (SVG carrying the mxfile embedded in the root `content=` attribute — visually renderable *and* drawio-editable) is also emitted.
**Canonical output:** `.drawio` / `.drawio.svg`.
**Ref dialect:** `§ID("name")`.
**Body shape:** flat verb-line.

Diagrams are graph-native, not vector-native — the primitives are *nodes* and *edges with source/target references*, not rectangles and lines. Canvas can draw a diagram, but it can't reason about relationships; diagram can. Style is passed through as raw drawio style strings so the LLM uses the full drawio palette without the protocol inventing abstractions.

| Group | Verbs |
|---|---|
| **Nodes** | `node`, `swimlane`, `group`, `label` |
| **Edges** | `edge §ID(src) §ID(tgt) label=… style=…` |
| **Decl** | `style_preset id=… style=…` (reference later via `style="@preset"`) |
| **Edit** | `edit`, `insert`, `delete`, `move` (same as other profiles) |

```
motion [diagram, artifact="arch"]:
  style_preset "service" style="rounded=1;fillColor=#dae8fc;strokeColor=#6c8ebf"  id="service"
  style_preset "store"   style="shape=cylinder3;fillColor=#ffe6cc;strokeColor=#d79b00" id="store"

  node "Frontend"  at 100 100 size=120x60 style="@service" id="fe"
  node "Backend"   at 300 100 size=120x60 style="@service" id="be"
  node "Postgres"  at 500 100 size=120x60 style="@store"   id="db"

  edge §ID("fe") §ID("be") label="HTTP"  id="e1"
  edge §ID("be") §ID("db") label="SQL"   id="e2"
```

**Rich-client round-trip.** Canvas's round-trip uses `hit_map()` + id-tagged SVG so a rich client can bind pointer events; diagram takes this further — the client can embed an actual **draw.io instance** (`transport.rich_client: drawio-embed`) and hand it the emitted `.drawio` file. The user manipulates nodes and edges in the full draw.io editor (drag, connect, restyle, add waypoints); the client reserialises to mxfile XML and ships it back. The shim calls `Diagram.from_drawio()` to re-ingest into the block model, and the LLM sees the user's visual edits as standard motion verbs via read-back. User edits made in the editor land as ordinary `edit §ID(x) set at=(…)` / `set style="…"` verb lines — no round-trip-specific verbs. This turns AI-assisted diagram editing into a collaborative loop: the AI drafts structure and relationships, the user refines visuals in the real drawio UI, the AI extends structure with the visual context intact.

**Bidirectional ingest.** `Diagram.from_drawio(path)` accepts both `.drawio` / `.xml` (raw mxfile) and `.drawio.svg` (SVG with `content=` attribute) — unwraps the embedded mxfile and recovers vertices (classified by style string into `node` / `swimlane` / `group` / `label`), edges with source/target refs, geometry, and parent composition (swimlane children, group children). `to_motion_body()` emits re-parseable motion verb-line text — inverse of `apply()`.

### 3.7 `sense [...]:` — input-side efficiency surface

*Opens the efficiency-surface family around the model. Input side is §3.7 (`sense`); output side is §3.8 (`validate`). Both sides surround the model with cheap deterministic shim passes so the model's capacity is reserved for the irreducibly probabilistic work. Authoring profiles (§3.1–3.6) sit alongside this as the artifact-mutation half of the output side.*

**Thesis.** The LLM is the expensive probabilistic oracle. Every token it spends on mechanically-decidable work — *where are the function boundaries, where is the text in this screenshot, is this a Python or Django question, does the output call `pickle.loads()`* — is a token not spent on the question. Move the mechanical work into the shim; the shim is cheap, fast, and deterministic. Freed capacity = faster + more accurate + more capable, with zero architectural change to the model.

Ozma ships this pattern in production *today* — just not yet named under a single protocol contract:

| Pass | What it does | Status |
|---|---|---|
| `compress_context` | Tree-sitter AST stubs + Ozma IR; 50–97% token reduction on code, zero quality loss | ✓ production |
| Hints (136 YAML) | Regex-pattern-detected conditional guidance injection; zero tokens when irrelevant | ✓ production |
| `classify_task` | Mechanical classification of difficulty / clarity / category | ✓ production |
| `sense [vision]` | Image decomposition — regions, text, palette, composition (this RFC) | v5.13 extension |

§3.7 names the input-side of this surface as a protocol contract so:
- the schema is a **training contract**, not just a runtime optimisation (train against the schema ⇒ schema-portable capability);
- new modalities (`sense [audio]:`, `sense [video]:`, `sense [log]:`, `sense [sensor]:`) slot in as concrete adapters against a stable spec;
- backends (which detector / which compressor / which hint rule) are shim-level declarations, not protocol-level fixtures.

§3.7.1 specs `sense [prompt]:` (natural-language user input — already partially shipping as hints + compress_context + classify_task). §3.7.2 specs `sense [vision]:` (first pixel-modality adapter).

Human analogue: pre-attentive vs attentive vision. Robotics / AV / doc-AI analogue: cheap→expensive sensor cascades. The cheap pass does not *replace* the expensive one; it hands the expensive one a pre-structured view so the expensive one spends its capacity on what actually matters.

### 3.7.1 `sense [prompt]:` — natural-language user input (ships today via hints + compress)

*Mostly already in production; §3.7.1 names it in the protocol and makes the schema a training contract.*

Any turn whose payload contains natural-language user input receives a `sense [prompt]:` observation. The shim extracts:

- **Intents** (classification: ask / create / refactor / migrate / fix / debug / review / plan / other)
- **Entities**: file paths, symbols, URLs, error messages, stack traces, dates, quoted strings
- **Pasted chunks**: code / errors / logs / config → content-addressed `{@HASH}` + detected language + compressed stub view (via `compress_context`)
- **Context signals**: detected frameworks / languages / domains (drives the hint engine — which of the 136 hints fire)
- **References**: `@filename`, `§ID(...)`, prior-turn refs, cross-session chunk refs
- **Migration detection**: if user intent matches a migration pattern (Django→FastAPI, Python→Rust, etc.), the migration hint fires and the porting pipeline becomes available

Observation schema (wire form JSON):

```json
{
  "device": "prompt",
  "intent": {"primary": "fix", "confidence": 0.84, "alternates": ["debug", "review"]},
  "entities": [
    {"id": "p1", "kind": "path",   "text": "backend/ozma/models.py", "confidence": 1.0},
    {"id": "s1", "kind": "symbol", "text": "compute_budget",        "confidence": 0.92},
    {"id": "e1", "kind": "error",  "text": "TypeError: 'NoneType' object is not subscriptable", "confidence": 1.0}
  ],
  "chunks": [
    {"id": "c1", "hash": "a4f9c1d2", "language": "python", "kind": "code",
     "stub_view": "…compressed stub…", "bytes": 4820, "compressed_bytes": 410}
  ],
  "context": {
    "frameworks": ["django", "fastapi"],
    "languages":  ["python"],
    "domains":    ["web", "auth"]
  },
  "hints_fired": ["django-to-fastapi", "python-async-traps", "auth-jwt-oauth"],
  "migration": {"detected": true, "from": "django", "to": "fastapi", "pipeline_available": true}
}
```

The model reads this observation alongside the raw user message. The hint layer (already shipping) fires on top of the structured decomposition; `compress_context` produces the `stub_view` for each code chunk; `classify_task` produces the intent classification.

Verbs the model may emit (reserved; most not required for v5.13 baseline):

| Verb | Effect |
|---|---|
| `expand §ID("c1")` | Request full (un-stubbed) source for a chunk the shim compressed. |
| `reclassify intent="debug"` | Override the shim's intent classification if wrong. |
| `suppress hint="django-to-fastapi"` | Ignore a fired hint (false positive). |

**Prior art.** Structural code compression parallels: cAST (arXiv:2506.15655), Repomix, Annotated-AST-for-LLM — none framed as a training contract. Stochastic token compression: LLMLingua / LongLLMLingua / LLMLingua-2 (up to 20× with 1.5pt drop on GSM8K/BBH); RECOMP (extractive+abstractive for RAG). Structured extraction from NL: spaCy-LLM, OpenAI Structured Outputs, function-calling corpora; ToolLLM / Gorilla train models against tool-schema inputs (closest existing training-contract neighbour — output-side schema only). Query decomposition / rewriting (HyDE, Query2Doc, Self-Ask, Plan-and-Solve) has mixed results — "Not All Queries Need Rewriting" (arXiv:2603.13301) documents cases where rewriting hurts when queries are already lexically aligned; v5.13 leaves rewriting off the default pipeline.

**Multi-pass prompt analysis** is open research territory (see §13) — the v5.13 `sense [prompt]:` schema is designed to accommodate additional passes (LLMLingua-style stochastic compression as a higher tier, query decomposition, retrieval preprocessing) as additional observation fields without grammar change. The named contribution is *unifying* deterministic preprocessing into a training contract, not any one pass in isolation.

### 3.7.2 `sense [vision]:` — structural preprocessing for image inputs

*Second concrete sense adapter; first pixel-modality.* Same thesis as §3.7.1: shim extracts structure once, cheaply, deterministically; model reads it. Every token a VLM spends on *where is the text*, *how many people are in the frame*, *what colours dominate* is a token not spent on the question. A detector does not forget a stop sign; a VLM probabilistically might.

**Sense vs motion.** Motion blocks are authored by the model — the model emits verbs, the shim mutates state. Sense blocks are authored by the *shim* — the shim attaches a structured observation to the turn's echo; the model reads it. The model may additionally emit a sense block to *request* re-observation at a different tier or scope. Both sides share the same flat verb-line grammar.

**Auto-attachment.** Any turn whose input payload contains `image/*` receives a `sense [vision, image=N]:` observation block per image, at the shim's default tier. This is the SOM-after-desktop-action pattern (v5.12) generalised to free-standing images.

**Observation schema (wire form is JSON):**

```json
{
  "device": "vision",
  "image": 0,
  "tier": 1,
  "dimensions": [1024, 768],
  "regions": [
    {"id": "t1", "kind": "text",   "bbox": [120, 440, 210, 32],
     "text": "Submit", "confidence": 0.96},
    {"id": "f1", "kind": "face",   "bbox": [300, 100, 120, 160],
     "confidence": 0.88},
    {"id": "o1", "kind": "object", "label": "car",
     "bbox": [40, 500, 320, 220], "confidence": 0.71},
    {"id": "e1", "kind": "edge_cluster", "bbox": [0, 0, 1024, 768],
     "density": "high"}
  ],
  "palette": [
    {"rgb": [34, 44, 61],  "fraction": 0.41},
    {"rgb": [231, 220, 180], "fraction": 0.22}
  ],
  "composition": {"horizon_y": 384, "rule_of_thirds": [[341, 256], [682, 256]]},
  "depth":       {"available": false},
  "exif":        {"available": true, "datetime": "2025-09-12T14:03:21", "gps": null}
}
```

Region ids live in the same `§ID` namespace as authoring artifacts and are unique *within the image*. A region may always be omitted (`regions: []`); an empty list means *shim ran the detector and found nothing*, distinct from the absent case (*detector not available at this tier*). Per-field `"confidence"` is mandatory on every probabilistic field. The `"available": true|false` envelope on optional sub-observations (`depth`, `exif`, `ocr`) separates *not measured* from *measured and empty*.

**Verbs the model may emit:**

| Verb | Effect |
|---|---|
| `upgrade tier=<N>` | Re-run observation at tier N on the current image (or all images in the turn). |
| `focus §ID("r1") ocr=true` | Re-run OCR at native resolution on the crop defined by a region. |
| `describe §ID("r3")` | VLM description on a specific region (tier 3). |
| `compare §IMG(0) §IMG(1)` | Pairwise sense observation across two images in the turn. |
| `sense §IMG(0) ocr=true depth=true` | Explicit re-observation with named sub-modules enabled. |

Example:

```
# Shim auto-attaches:
sense [vision, image=0, tier=1]:
  # (observation JSON attached to turn echo — tree listing as rendered above)

# Model response (in the same turn) may request an upgrade:
sense [vision, image=0]:
  focus §ID("t1") ocr=true
  upgrade tier=2
```

**Tiering.** The `tier` axis is the sense equivalent of the authoring `auto_preview` ladder — a cost/capability knob declared by the shim, not fixed by the protocol:

| Tier | Budget | Typical content |
|---|---|---|
| 0 | <10ms/MP | Pillow-only: dimensions, palette, EXIF, simple edge density, aspect class. |
| 1 | <100ms/MP | + OCR + face/object detection + basic composition (cheap classical CV + small detector). |
| 2 | <1s/MP | + segmentation, depth estimation, richer object vocabulary (model-backed detectors, e.g. SAM-family / RT-DETR / MiDaS class). |
| 3 | 1–10s/image | VLM escalation — region captioning, grounded description, visual question answering. |

**Backends are shim-level, not protocol-level.** The protocol fixes the verbs, the observation schema, tier semantics, and confidence/null conventions. Which detector ships in tier 2 (YOLO-NAS-S vs RT-DETR vs a bespoke model) is a `shim_profile.yaml` declaration, same shape as `renderers:` and `authoring:`. Different shims can back the same verbs with different backends; a model trained against the schema is portable across shims.

**Why this matters for training.** The sense schema is not just a runtime contract — it is the **training contract**. Ozma's code precedent is unambiguous on this: a model trained to read tree-sitter-compressed source is both faster (fewer tokens) *and* more accurate (capacity spent on reasoning, not extraction). The same result is available to vision models trained against this schema:

- Training data generates mechanically by running the shim over any image corpus — the shim *is* the annotator.
- A model trained against the schema is portable to any protocol-conformant shim, with zero re-annotation.
- The protocol stops being inbound plumbing and becomes the **unit of transferable capability**.

This is the same load-bearing property that makes `compress_context` work for code, extended to pixels. (Prior art on the runtime side — Set-of-Marks, OmniParser, LayoutLMv3, SeeClick — validates the token-reduction and accuracy direction; none of those ships a schema the model is *trained against* as a first-class citizen.)

**Out of scope for v5.13.** `sense [audio]:` / `sense [video]:` profiles; training corpus shape; specific detector backends; model-side schema conformance tests. Each is natural follow-on once the family is seeded.

### 3.8 `validate [...]:` — output-side efficiency surface

*Symmetric mirror of §3.7. Sense decomposes inputs before the model reads; validate validates outputs after the model produces. Both are cheap deterministic shim passes; both keep probabilistic capacity focused on the irreducible core.*

A model that writes code should not spend capacity re-checking whether it used `pickle.loads()`, whether it left an f-string SQL query, whether `ruff` will reject the file on formatting, or whether its migration recipe re-creates a known anti-pattern. The regex fires, the linter runs, the formatter fixes — deterministically, in milliseconds, without model involvement. The model spends its capacity on *writing the right code*, not re-verifying mechanical properties.

Ozma ships this side in production *today* — the v5.13 contribution is naming it in the protocol and making the validation schema a training contract:

| Pass | What it does | Status |
|---|---|---|
| Checks (15 built-in) | Regex validators over model output; triggers `warn` / `resubmit` / `fix` / `run` / `block` | ✓ production |
| Rules (64 built-in) | Extended-action YAML — `suggest` / `fetch_spec` / `score` / same engine as hints | ✓ production |
| Renderers | `{@HASH}` → PNG dispatch (GRAPH / LATEX / Plotly / HTML / SVG / glTF) | ✓ production |
| Authoring verbs | Model emits `edit §ID(x) set at=(…)`; shim performs the deterministic artifact mutation | ✓ §3.1–3.6 |

§3.8.1 specs `validate [checks]:`; §3.8.2 specs `validate [rules]:`. Both are YAML-declared in `shim_profile.yaml` (the existing format carries unchanged), and both emit a structured observation — a `validate` block — attached to the turn echo immediately after the model's output.

**Observation schema (wire form JSON):**

```json
{
  "device": "checks",
  "turn_output_hash": "a4f9c1d2",
  "fired": [
    {"id": "f-string-sql",       "severity": "critical", "action": "resubmit",
     "match": "cursor.execute(f\"SELECT …", "line": 42,
     "feedback": "Use parameterized queries. f-string SQL is injection-prone.",
     "confidence": 1.0},
    {"id": "ruff-unused-import", "severity": "style",    "action": "fix",
     "fix_applied": true, "fix_summary": "removed unused `import json`",
     "confidence": 1.0}
  ],
  "suppressed": [],
  "outcome": "resubmit"     // one of: accept | resubmit | fixed | blocked
}
```

Field semantics:

- `fired` — ordered list of rules that matched the model's output. Each carries severity, action, match evidence, and (for `resubmit`) the corrective feedback that will be fed back into the model's next turn.
- `action` — one of `warn` (log only) / `resubmit` (feedback to model) / `fix` (autoformatter / auto-patch applied) / `run` (linter or typechecker invoked, output captured) / `block` (reject).
- `outcome` — turn-level result. `accept` (nothing fired or all `warn`); `resubmit` (at least one `resubmit` fired, model gets corrective feedback and retries); `fixed` (shim auto-fixed, model does not retry); `blocked` (hard reject).
- `confidence` — on regex-matched checks, `1.0`; on pattern-classified rules, the classifier score.

**Verbs the model may emit:**

| Verb | Effect |
|---|---|
| `ack` | Acknowledge outcome; no remediation needed. Implicit default on `accept` / `fixed`. |
| `suppress id="<check-id>" reason="<why>"` | Dismiss a false-positive check (e.g., intentional `eval` in test fixture). |
| `remediate` | Signal that the next output will address the `resubmit` feedback. Implicit default on `resubmit`. |

Explicit emission is optional — the shim applies sensible defaults based on `outcome`. Suppression requires a `reason=` so false-positive audits stay grounded.

### 3.8.1 `validate [checks]:` — regex + action checks (ships today)

The existing ozma checks engine. 15 built-in checks covering f-string SQL, `pickle.loads` / `eval` / `exec` / `os.system` shell injection, hardcoded secrets, AWS keys, weak hashes, and a handful of logic-flaw patterns. Declared as YAML; each check has a `detect:` regex (or small regex family), a `severity:`, an `action:`, and — for `resubmit` — a `feedback:` string fed back to the model.

Project-specific checks: drop YAML in `.checks/` at repo root. Same shape as hints; same engine. Cost: regex match, ~0.01ms per scan. Runs on every model output automatically; runs in CI on every commit independently.

### 3.8.2 `validate [rules]:` — extended actions (ships today)

Same YAML format as hints and checks; extended action space:

- `suggest` — propose a library / API / idiom ("did you mean httpx instead of requests for async?") as an inline suggestion in the resubmit feedback.
- `fetch_spec` — when the model references an external API, auto-fetch the OpenAPI spec and inject it into the next turn's context.
- `score` — complexity / risk / readability scoring emitted as a numeric signal, usable by routing and eval.
- `run` — invoke a command (linter / typechecker / formatter / test runner) and capture output as part of the `validate` observation. Used by dev-loop for `ruff`, `mypy`, `pytest`.

### Training contract

A model aware of which checks will fire on its output is:
- **Faster** — does not re-validate properties mechanically enforced downstream.
- **More first-try accurate** — output distribution shaped by knowledge of which patterns are rejected.
- **Safer by default** — security checks (f-string SQL, `pickle`, `eval`) form a hard constraint the model learns to avoid rather than rediscover.

Training data generates mechanically: run any model's historical output through the checks engine, label by `outcome`, include the `fired` list as auxiliary supervision. A model trained against this schema understands the output distribution that passes; a model trained against a new shim's checks gets the new shim's behavior, portable with zero re-annotation.

**Out of scope for v5.13.** Model-side rejection-learning recipes; reference check corpus; interaction between `validate [rules]:` fetch_spec and the `{@HASH}` caching layer.

### 3.9 `converge` — cross-device convergence primitive

*Not a device profile. A cross-cutting verb every `motion` and `sense` adapter can support. The shim owns the inner loop; the model sets the goal and reads the terminal result.*

**Thesis role.** The efficiency thesis demands that the model never run inner control loops. Classical control has 40+ years of adaptive-control theory (MRAC, gain scheduling, system identification, gain-scheduled PID, MPC with online identification) that is cheap, deterministic, kHz-rate, and certifiable (DO-178C / ISO 26262 / IEC 61508 / IEC 62304). All of it belongs in the shim. The protocol's job is to expose one primitive through which the model specifies *what* to converge to, not *how*. The same primitive handles PID loops in motor control, retrieval loops in RAG, grounding loops in fact-checking, optimization loops in hyperparameter search, and any other bounded-convergence shape.

This is the load-bearing primitive for every future world-interface adapter. `motion [robot]`, `motion [nav]`, `motion [game]`, `sense [3d]`, `sense [map]` — each reuses `converge` without re-specifying its own loop contract.

**Grammar.** Flat verb-line, same family as other verbs:

```
converge <target-ref> <goal-attrs> <termination-attrs> [method=<name>] [async=<bool>]
```

- `<target-ref>` — what is converging. A `§ID(...)` for artifact / world-state elements, a bareword for device-scoped targets (`chunks`, `claim`, `params`).
- `<goal-attrs>` — device-specific goal specification (`to=(x,y,z)`, `to="stable"`, `topk=5 relevance_min=0.8`, `minimize=cost`). Valid attrs per device are declared in that device's profile.
- `<termination-attrs>` — at least one of: `tolerance=<value>`, `max_iter=<N>`, `timeout=<duration>`, `cost_budget=<value>`. The shim terminates when any fires. If none specified, shim defaults from its profile.
- `method=<name>` — optional hint selecting from methods declared in `shim_profile.yaml` (`pid-adaptive-mrac`, `rerank`, `hyde-then-rerank`, `bayesian`, `taffy-layout`, ...). Absent → shim picks per device + goal shape.
- `async=true` — optional; returns a handle immediately (see *Asynchrony* below).

Examples:

```
# Physical control — grasp
motion [robot, artifact="arm-1"]:
  converge §ID("end_effector") to=(0.30,0.20,0.50) tolerance=0.005 timeout=2s method="pid-adaptive-mrac"

# Layout solve — canvas
motion [canvas, artifact="poster"]:
  converge §ID("layout-root") to="stable" tolerance=1px timeout=500ms

# Retrieval — sense[prompt]
sense [prompt]:
  converge chunks topk=5 relevance_min=0.8 timeout=500ms method="hyde-then-rerank"

# Grounding — sense[prompt]
sense [prompt]:
  converge §ID("claim-3") to="verified" confidence_min=0.85 timeout=2s cost_budget=0.02

# Hyperparameter optimization
motion [config, artifact="train-run"]:
  converge §ID("hparams") minimize=val_loss timeout=600s method="bayesian"

# Abort an in-flight async converge
motion [robot]:
  abort §CONV("grasp-23")
```

**Goal specification kinds** (which a device profile declares support for):

| Kind | Form | Used by |
|---|---|---|
| Point | `to=(x,y,z)` / `to=§ID("pose")` | robot, nav, canvas layout |
| Predicate | `to="stable"` / `to="verified"` | layout solvers, grounding |
| Top-K + threshold | `topk=N relevance_min=F` | retrieval, search |
| Optimization | `minimize=<metric>` / `maximize=<metric>` | hyperparameter tuning, resource allocation |
| Range / band | `to=(0.3±0.01, ...)` | control with tolerance inline |

**Observation schema (terminal, attached after convergence ends):**

```json
{
  "kind": "converge",
  "target": "end_effector",
  "handle": "grasp-23",
  "outcome": "converged",
  "final_state": {"position": [0.301, 0.199, 0.500], "error": 0.003},
  "iterations": 437,
  "duration_ms": 1840,
  "cost": {"ms": 1840, "energy_j": 0.3, "model_calls": 0, "usd": 0.0},
  "trajectory_digest": {
    "samples": 10,
    "max_error": 0.12,
    "min_error": 0.003,
    "oscillations": 0,
    "overshoot_pct": 2.1,
    "settling_time_ms": 1620
  },
  "method_used": "pid-adaptive-mrac",
  "system_identification": {
    "adapted": true,
    "regime_change": false,
    "plant_confidence": 0.91
  },
  "telemetry_hash": "a4f9c1d2"
}
```

Field semantics:
- `outcome` — one of `converged` / `timeout` / `cost_exhausted` / `aborted` / `blocked` / `bailed`. Blocked = safety gate or permission denied. Bailed = shim refused to continue (plant instability, identification failure, sensor fault).
- `final_state` — actual terminal state (may differ from goal when outcome ≠ converged).
- `trajectory_digest` — compressed behavioural summary. The model reads this to understand *how* the loop went without seeing every sample.
- `method_used` — which declared method the shim actually ran (useful when the model hinted a method and the shim fell back).
- `system_identification.adapted: true` is routine. `regime_change: true` is a signal the plant changed during the loop (damage, unmodeled nonlinearity, parameter drift) — worth the model investigating.
- `telemetry_hash` — full trajectory stored as a `{@HASH}` chunk. Fetchable for audit / post-mortem; not streamed to the model by default.
- `cost` — mechanical and monetary. Model-readable for budget-aware decisions.

**Method declaration in `shim_profile.yaml`:**

```yaml
converge_methods:
  pid-adaptive-mrac:
    applicable_to: [robot, nav]
    goal_kinds:   [point, range]
    adaptation:   online-sysid
    typical_latency: <2ms per iter
    certifiable:  [iso-26262, iec-61508]
  hyde-then-rerank:
    applicable_to: [prompt]
    goal_kinds:    [topk-threshold]
    backend:       "HyDE + cross-encoder/ms-marco-MiniLM"
    typical_cost:  0.002 USD per run
  taffy-layout:
    applicable_to: [canvas, document, slides]
    goal_kinds:    [predicate]
    backend:       stretchable
  bayesian:
    applicable_to: [config]
    goal_kinds:    [optimization]
    backend:       "optuna + GP"
```

**Asynchrony.**

Default: blocking. The model emits `converge`, the shim runs the loop, the terminal observation appears on the next echo. For long loops (builds, training runs, long-horizon physical tasks), `async=true` returns a handle; the model can later `status §CONV(handle)` or `abort §CONV(handle)`. Completion delivers the terminal observation on whichever turn it finishes.

New sigil: **`§CONV("handle")`** — opaque handle to an in-flight or recently-completed converge. Unique within the session.

Ancillary verbs:

| Verb | Effect |
|---|---|
| `abort §CONV("handle")` | Cancel an in-flight converge. Always permitted (emergency-stop semantics). |
| `status §CONV("handle")` | Mid-loop snapshot without terminating — iterations so far, current error, cost so far. |
| `await §CONV("handle") [timeout=<dur>]` | Block until the named converge terminates (or timeout). Returns the terminal observation. |

**Safety and certification.**

- `requires_confirm: [converge]` per device profile gates real-world converges behind operator confirmation (same mechanism as `save`).
- `abort` is never blocked, never requires confirmation.
- Methods declare `certifiable:` lists so deployments can pick methods that match regulatory context (a medical titration shim selects only IEC 62304-certifiable methods; an automotive one selects ISO 26262-certifiable).
- `outcome: "blocked"` carries a machine-readable reason (`safety_gate` / `permission_denied` / `infeasible_plant` / `sensor_fault`).
- Deterministic replay: `telemetry_hash` + method version + initial state is sufficient to replay deterministically modulo real-world noise. Audit-friendly.

**Side-effect semantics.**

- Physical devices (robot, nav): side effects are *not* unwound on non-converged outcomes. Model reads `final_state` to understand where things ended.
- Software authoring (canvas, document, …): transactional by default — on `bailed` / `aborted`, mutations are reverted unless the profile declares otherwise.
- Declared per-device in `shim_profile.yaml` under `converge.unwind_policy: commit | revert`.

**Training contract.**

A model trained against this schema:
- **Knows not to write inner loops.** The schema makes `converge` the canonical shape for closed-loop work; training data generates by running the shim's methods and recording terminal observations.
- **Reads trajectory digests natively.** `oscillations > 0` ⇒ "controller is ringing"; `regime_change: true` ⇒ "plant changed, investigate"; `outcome: "cost_exhausted"` ⇒ "try larger budget or cheaper method," not retry verbatim.
- **Budgets converges.** The `cost` field on every terminal observation is training signal; the model learns which methods are cheap and which are expensive for what goal shapes.
- **Escalates on `bailed` / `regime_change`.** These are the signals where model reasoning actually matters — interpreting novelty, choosing whether to retry with a different method, asking for human confirmation. The common case (converged within budget) requires no model reasoning at all.

This is the biological parallel landing cleanly: the neocortex doesn't monitor every spinal reflex; it reads the terminal state and intervenes only when the reflex system signals something unusual. `converge` makes that separation a first-class protocol primitive.

**Out of scope for v5.13.**
- Multi-agent / distributed converges (one shim running a loop across multiple physical agents with shared state).
- Hard-real-time delivery guarantees (the shim does real-time; the protocol-level contract is soft-real-time; tightening this is a v5.14 concern with certifiable-deployment semantics).
- Method learning (the shim adapting / discovering new methods at runtime).
- Composition: `converge` on top of `converge` (nested outer/inner loops at the protocol level rather than the shim level).

### 3.10 `§SECRET`, `§PII`, `§CONF` — information-flow control lattice

`§SECRET`, `§PII`, and `§CONF` introduce a **classification lattice** over every value the protocol carries. The shim enforces lattice integrity at every boundary: ingress (sense), egress (validate), persistence (trajectory), derivation (compression), and extraction (training corpus). The model operates on values whose classification has already been decided by the shim; it cannot launder, drop, or bypass labels — because, at tier 0, it never holds the unlabelled form at all, and at tiers 1–2, the label propagates through every subsequent operation.

**The precedent is older than LLMs by decades.** Credit-card tokenisation solved tier 0 at planetary scale: the merchant terminal never sees the real PAN, only a network token dereferenced by the issuer at the wire. HIPAA / PCI-DSS / diplomatic classification solved tiers 1–2 organisationally: every document carries its classification, and handling rules follow from the tag. Operating-system DIFC (Jif, Asbestos, HiStar, Flume) established the lattice semantics and runtime enforcement patterns. Biology solved labelled-line coding at the sensory-tract level (pain / touch / proprioception each routed to different cortical destinations) and self/non-self tagging via MHC at the immune layer. All four converge on the same answer: **labels live on the values, not in the processing element's behaviour.**

Ozma's contribution is to make this the LLM-agent architecture's structural backbone, not a bolted-on governance layer.

**The lattice (partial order, lattice-join on composition).**

| Tier | Sigil | Model sees value? | May emit to model output? | May persist to trace / corpus? | Examples |
|---|---|---|---|---|---|
| 0. Opaque | `§SECRET("h")` | **No** — handle only | No (dereferencing is a `block`) | Handle only; value never persisted | API keys, passwords, PAN, private keys, session tokens, SSH keys |
| 1. Processable, egress-filtered | `§PII("h", kind=…)` | Yes | Only to allowlisted destinations | Pseudonymised or excluded | Name, email, phone, address, DOB, PHI, financial figures |
| 2. Internal, routed | `§CONF("h", class=…)` | Yes | Allowlist by destination class | Tenant-scoped retention | Roadmap, internal docs, customer lists, proprietary code |
| 3. Public | (no sigil) | Yes | Anywhere | Anywhere | Published docs, OSS code, public APIs |

Any shim-mediated operation on labelled values produces a result whose label is the **lattice-join** (least upper bound) of its inputs. "Summarise this document" where the document contains `§PII("email-0")` produces a summary labelled `§PII`, regardless of whether the email literal survives into the summary verbatim. Declassification (label drop) is a shim-owned operation (§3.10.5) and is always logged.

---

#### 3.10.1 `§SECRET` — tier 0 opaque handles

**Grammar.**

```
§SECRET("name")
```

Handle-only. The model never sees the value, and dereferencing is not a verb the model can issue. The shim materialises the value exclusively at egress to an external system:

- **HTTP:** `Authorization: Bearer §SECRET("github_token")` — shim substitutes at request-send time; the literal bytes never appear in model context or trace.
- **SQL:** parameterised substitution only; shim rejects string-concat construction of SQL with a `§SECRET`.
- **SSH / git:** agent forwarding; private key material never materialises in model-accessible memory.
- **Shell:** secrets are not passed as argv; shim uses stdin / env / secret store on a per-launch basis.

**Registry** (new `secrets:` section in `shim_profile.yaml`):

```yaml
secrets:
  github_token:
    source: env:GITHUB_TOKEN            # env | keyring | vault | agent | hsm
    scope: [http.header, git.remote]    # permissible egress sites
    rotation: external
    audit: true
  db_password:
    source: keyring:prod-db
    scope: [sql.parameter]
  ssh_id_rsa:
    source: agent:ssh-agent
    scope: [ssh.auth]
```

Egress outside `scope` is a hard `block` at the validate layer regardless of model intent. **Rotation** is an edit to `shim_profile.yaml`; the handle is stable across rotations; no model retraining or prompt rewrite. **Compromise of the model context is a non-event for tier 0** — the key was never there to leak.

---

#### 3.10.2 `§PII` — tier 1 processable, egress-filtered

**Grammar.**

```
§PII("handle", kind="email" | "name" | "phone" | "address" | "dob" | "phi" | "pan" | …
              [, jurisdiction="EU" | "US-HIPAA" | "US-PCI" | …])
```

The model sees the value because it often needs to reason about it — compose an email, correct an address, interpret a medical note. The difference from tier 3 is that the label propagates through every subsequent operation, and egress is destination-gated.

**Ingress.** `sense [prompt]:` emits `§PII(...)` handles on entity extraction:

```
sense [prompt]:
  entity kind="email" value="alice@example.com" id=§PII("email-0")
  entity kind="name"  value="Alice Smith"       id=§PII("name-0")
```

Classification sources: built-in NER, explicit caller-supplied tagging (the upstream application knows it is shipping PII), regex for stable formats (email, phone, card), regulated-field lists (HIPAA PHI categories, PCI PAN, GDPR personal-data categories).

**Egress.** `validate [classify]:` (new sibling to `validate [checks]` and `validate [rules]`; see §3.10.6) evaluates every outbound request against the value's allowlist. Allowlists are declared on the destination side in `shim_profile.yaml`:

```yaml
destinations:
  anthropic.api:
    accepts: [public]                              # tier 3 only
  internal.datastore:
    accepts: [public, confidential, pii]
    jurisdictions: [US, EU]
  email.smtp:
    accepts: [public, "pii:email", "pii:name"]     # kind-scoped
```

If the model paraphrases a `§PII` value into prose ("send a message to Alice at alice@example.com"), the shim **re-classifies at egress** by regex + fuzzy match against the live label table and applies the same rules. Model output containing an unregistered high-entropy credential-shaped string triggers `secret-dereferenced` from `validate [checks]` — a hard block.

**Jurisdiction** is a product of the label. A `§PII(..., jurisdiction="EU")` can only egress to destinations with `EU` in their `jurisdictions` list — this is how GDPR data-residency becomes structural rather than procedural. HIPAA PHI only egresses to destinations under a BAA; PCI PAN only via tier-0 tokenisation (see §3.10.1).

---

#### 3.10.3 `§CONF` — tier 2 internal, destination-routed

**Grammar.**

```
§CONF("handle" [, class="roadmap" | "customer-list" | "internal-code" | …]
              [, tenant="tenant-42"])
```

Processable, but routed by destination class. Roadmap documents must not go to the public-facing chat backend; customer lists must not be summarised to a third-party LLM API. `tenant` labels enforce cross-tenant prohibition: a `§CONF(tenant="A")` cannot egress to a destination handling tenant B.

This is the tier that makes the architecture tractable for **B2B multi-tenant deployments**. The model does not need to know which tenant it is in; the shim enforces isolation at the label layer, not in prompt engineering.

---

#### 3.10.4 Label propagation

Every shim-mediated operation on labelled values produces a result whose label is the lattice-join of its inputs:

```
summarise(§PUBLIC, §PII)                  → §PII
diff(§CONF(tenant=A), §PUBLIC)            → §CONF(tenant=A)
concat(§SECRET, anything)                 → rejected (tier 0 never reaches the pipeline)
translate(§PII(…, jurisdiction="EU"), …)  → §PII(…, jurisdiction="EU")
```

**The training-contract implication.** A classification-preserving model is one that, when it paraphrases, summarises, translates, or re-encodes a labelled value, emits output labelled with the join of the inputs' labels. The shim *checks* this at validate time; the training distribution *prepares* for it. This is a measurable capability with a well-scoped eval: sample labelled inputs, request a transformation, assert that the shim's re-classification of the output matches the expected join.

Shim-side label propagation is mechanical. **Model-side classification-preserving generation is the hard, new, research-scale problem** — and the only path to regulatorily-defensible agentic systems in HIPAA / PCI / GDPR domains. The protocol specifies the contract; training against it is a follow-on RFC.

---

#### 3.10.5 Declassification

The only way a value drops a label is through an explicit shim-owned `declassify` operation, which is always logged and always gated:

- **Tier 0 → anything:** impossible. `§SECRET` values are never declassifiable through the protocol; they terminate at egress and never exist in cleartext in model context.
- **Tier 1 / 2 → tier 3:** requires an explicit shim-side policy (e.g., a `publish` workflow that strips PII fields, pseudonymises tenant IDs, or aggregates to k-anonymous form). **Not a model action** — the model has no `declassify` verb.
- **Audit.** Declassification events are persisted to trajectory with before/after labels, the operator identity, and the policy rule that authorised the drop.

Analogy: PCI tokenisation reversal is possible only at the issuer, never at the merchant. Declassification is possible only at the shim, never at the model.

---

#### 3.10.6 Interaction with trajectory, corpus, and compression

Labels propagate through every derived artifact:

- **`trajectory_digest` and `telemetry_hash`.** Trace entries carry the lattice-join of every value they touched. Redaction at read-time is a consumer-clearance join: an ops dashboard with tier-2 clearance sees redacted tier-1 values; an incident-response terminal with tier-1 clearance sees them raw. Trace-equality hashes are taken over labels and structural shape, not values — so two traces can be verified equivalent across differently-cleared views.
- **Training-corpus extraction.** Tier 0 is excluded (or retained only in `§SECRET(...)` handle form, so the model is trained to reference but never dereference). Tier 1 is pseudonymised at extraction (emails → stable per-corpus synthetic addresses; names → stable synthetic identities; dates shifted by per-identity offsets). Tier 2 is excluded unless the corpus is scoped to that tenant. Tier 3 is raw-safe.
- **`compress_context` and Ozma IR.** Literals inside code carrying classifiable values (hardcoded emails, embedded credentials, internal URLs, tenant IDs) are either pseudonymised or promoted to `§SECRET` / `§PII` handles before IR emission. The tree-sitter pass becomes classification-aware at the literal level — extending the string-embedded-language detection already shipping in `compress_context` (SQL in Python, Jinja in templates).

**`validate [classify]:` block shape.** New `validate` sibling, same YAML engine as `validate [checks]` and `validate [rules]`:

```
validate [classify]:
  fired id="pii-email-outbound" severity="block" \
        value=§PII("email-0") destination="anthropic.api" \
        reason="destination accepts only public; value is pii:email"
  outcome "blocked"
```

Actions: `block` (default for lattice violations), `redact` (strip the value from the payload before forward), `escalate` (route to a human approval queue), `pseudonymise` (substitute with a shim-generated synthetic for safe-to-egress kinds).

**Prior art.** Information-flow control has four decades of rigorous semantics in operating-systems and language research. Myers and Liskov's decentralized label model [Myers & Liskov, SOSP 1997; POPL 1999 (*Jif*/*JFlow*)] established the lattice and declassification primitives that subsequent kernel-level DIFC systems — *Asbestos* [Efstathopoulos et al., SOSP 2005], *HiStar* [Zeldovich et al., OSDI 2006], *Flume* [Krohn et al., SOSP 2007], *DStar* [Zeldovich et al., NSDI 2008] — carried into process and distributed-system boundaries. *Resin* [Yip et al., SOSP 2009] and *Laminar* [Roy et al., PLDI 2009] ported the discipline into language runtimes. Dynamic taint tracking is the industrial counterpart: Perl's taint mode, dynamic taint analysis [Newsome & Song, NDSS 2005], *TaintDroid* [Enck et al., OSDI 2010], and modern static taint engines (CodeQL, Semgrep, Meta Pysa, SonarQube) propagate source-to-sink labels through real code paths. Tier-0 opacity is a solved industrial problem at planetary scale: PCI DSS 4.0 and EMVCo's *Payment Tokenisation Specification*, plus the Apple Pay / Google Pay network-token specs, route card PANs through tokenisation so the secret never enters the processing surface.

Within LLM-agent systems the literature is young but non-empty. Indirect-prompt-injection defenses [Greshake et al., AISec 2023 — "Not What You've Signed Up For"; Perez & Ribeiro 2022] identified the data-vs-instructions confusion and popularised the informal *lethal trifecta* (read-untrusted + access-private + egress), which this section treats as an uncompiled, unenforced precursor to lattice propagation. Two 2025 papers explicitly take the typed-channel step:

- **CaMeL** [Debenedetti, Shumailov, Tramèr et al., arXiv:2503.18813, March 2025 — Google DeepMind] compiles the planner's output into a restricted program and attaches **capabilities** to every value, enforcing control- and data-flow integrity against injection. Capability-based, not lattice-based — an intentional, defensible alternative framing.
- **FIDES** [Costa, Köpf, Russinovich et al., arXiv:2505.23643, May 2025 — Microsoft Research] performs **dynamic taint-tracking of confidentiality and integrity labels** through LLM and tool calls, using a quarantined LLM for constrained inspection of tainted content. This is the closest existing analogue to §3.10's lattice.

Both target inference-time agent planning against prompt injection. Neither addresses (a) training-corpus extraction as a flow boundary, (b) trajectory / log persistence under multi-clearance read, (c) context compression (`compress_context` / Ozma IR) as a declassification pressure, (d) tier-0 tokenisation integration, or (e) classification-preserving paraphrase as a model-side training objective. Adjacent work — *LLM-IFT* hardware taint channels [arXiv:2504.07015, 2025], *AgentFuzz* taint-style agent fuzzing [USENIX Security 2025] — confirms interest but sits outside the semantic-label agenda.

Enterprise data-governance platforms (Collibra, Microsoft Purview, Atlan, BigID, Immuta) discover and tag data at rest and can gate retrieval for Copilot-style agents, but their labels are **erased at the LLM boundary**: once a Purview-labelled document enters a prompt, no downstream operation — tool call, summarisation, trajectory log, fine-tune extraction — preserves the label. They are catalogues, not runtime enforcers. Differential-privacy fine-tuning [Yu et al. 2022; Google user-level DP-SGD 2024] bounds memorisation statistically but expresses no per-value flow rules. Mainstream agent frameworks (LangChain, LangGraph, LlamaIndex, DSPy, MCP, ToolLLM, Gorilla) type tool calls but not value classifications; all value flow is untyped strings. Endpoint-only classifiers (Llama Guard 3, NeMo Guardrails, Azure AI Content Safety, OpenAI moderation, Anthropic classifier API) evaluate text at completion without label propagation.

**Positioning.** §3.10 is not the first IFC treatment of LLM agents — CaMeL and FIDES predate it by weeks. It is the first to (i) apply a **full classification lattice** across sense / validate / trajectory / training / compression as coequal enforcement boundaries, (ii) integrate tier-0 tokenisation with tiers 1–2 processable labels in a single schema, and (iii) make **classification-preserving paraphrase a training-contract objective** on the model side, eval-measurable against shim-side re-classification. The unclaimed surface is wide; the individual semantics are not novel.

---

**Out of scope for v5.13.**

- Model-side training recipes for classification-preserving generation (separate RFC + paper; the eval shape is scoped above).
- Covert-channel analysis. The DIFC literature extensively treats timing, termination, and cache side channels; v5.13 addresses *value flow* only.
- Automatic re-classification of historic trajectories when label definitions change (retroactive re-labelling semantics).
- Cryptographic enforcement of tier-2 tenant isolation (encryption at rest keyed by tenant label). Deferred; orthogonal to the flow-control layer.
- Formal soundness / non-interference proofs for the propagation rules. DIFC has mature proof techniques; applying them is a follow-on.

## 4. `§ID`, `§SLIDE`, `§IMG`, and `§CONV` ref sigils

New peers to the existing sigil family:

| Sigil | Scope | Backed by |
|---|---|---|
| `§SOM[N]` | desktop | Set-of-Marks overlay |
| `§CSS("…")` / `§XPath("…")` | browser | DOM |
| `§AID("…")` | mobile | Platform accessibility tree |
| **`§ID("name")`** | **authoring (canvas / document / slides / sheet / scene / diagram) + sense regions** | **Artifact element table / sense region table** |
| **`§SLIDE(N)`** | **slides only** | **Slide-index locator (not a namespace)** |
| **`§IMG(N)`** | **sense (vision)** | **Turn-scoped image payload index** |
| **`§CONV("handle")`** | **cross-device convergence (§3.9)** | **Session-scoped in-flight/completed converge handle** |
| **`§SECRET("name")`** | **information-flow control (§3.10.1)** | **Shim-side secrets registry; value never in model context** |
| **`§PII("handle", kind=…[, jurisdiction=…])`** | **information-flow control (§3.10.2)** | **Classified-value label table; tier-1 processable, egress-filtered** |
| **`§CONF("handle"[, class=…][, tenant=…])`** | **information-flow control (§3.10.3)** | **Classified-value label table; tier-2 internal, destination-routed** |

Semantics:

- **Declaration:** `id="name"` attribute on any element-creating verb. If omitted, the shim auto-assigns `<kind><N>` (e.g., `p1`, `p2` for paragraphs, `h1` for first heading, `e1` otherwise).
- **Uniqueness:** **per-artifact**. A name is unique within one artifact; collision is an error (`id_collision`). This applies to slides as well — `§ID("title")` refers to exactly one element, regardless of which slide it's on.
- **Stability:** stable across turns for the lifetime of the artifact.
- **§SLIDE(N) semantics:** a *locator*, not a namespace. `§SLIDE(3)` alone references the slide at index 3 as a whole (e.g., for `slide_delete §SLIDE(3)`). When combined with `§ID(x)` inside a verb's target, it acts as a filter/assertion: `edit §SLIDE(3) §ID("title")` asserts the element is on slide 3 and errors if it isn't. This is optional disambiguation — `edit §ID("title")` works too because IDs are per-artifact unique.
- **Sigil discipline:** bare `ID("name")` or `SLIDE(3)` (no sigil) do not match; same rule as `§SOM[N]` in v5.12. Prevents accidental capture from prose. Same rule binds `§SECRET` / `§PII` / `§CONF`: the `§` is load-bearing for every sigil family.
- **IFC sigil declarations:** `§SECRET("name")` entries are declared in `shim_profile.yaml`'s `secrets:` section (§3.10.1); `§PII(...)` / `§CONF(...)` entries are emitted by `sense [prompt]:` on entity extraction (§3.10.2–3) or registered by the calling application before the turn. The model never declares a new `§SECRET` / `§PII` / `§CONF` handle — it only references ones the shim has put into scope.

## 5. `embed {@HASH}` verb

The single data-embedding verb across all four visual/spatial device profiles (canvas, document, slides, scene). The chunk referenced by `{@HASH}` carries a declared content type; the shim dispatches to the first declared renderer in `shim_profile.yaml` whose `accepts:` list matches the content type.

**Dispatch is data-driven, not spec-fixed.** A deployment with no `plotly-to-png` declared simply fails `embed` of PLOTLY chunks with `no_renderer_for_content_type`. A deployment that adds `vega-lite-to-png` gains that embed type without any protocol change.

Forms:

```
embed {@a4f9c1d2} at 50 80 size=700x400          # canvas / slides — rect placement
embed {@a4f9c1d2} width=500                      # document — flow placement
embed {@b7e2a9d1} at=(2,0,0) scale=1.0           # scene — 3D placement
paste A2 = {@a9d4f1c7}                           # sheet — cell-range write (data, not render)
```

**Why no inline chunk bodies:**

- Same data reused across artifacts → one hash, no duplication.
- Cross-session reuse via SESSION-REF works for free.
- Trained anaphor handling (`{@HASH}` treated as expanded chunk by the LoRA) carries unchanged.
- Existing sha256 cache/retention layer pays for itself again.

The LLM produces the chunk in the normal way (a `graph:` output, a `plotly:` output, etc.), observes the assigned hash in the turn echo, and references that hash from subsequent authoring turns.

**Cache miss (`chunk_not_resident`):** if a referenced `{@HASH}` has been evicted before embed time, the shim returns `chunk_not_resident` with the hash and suggested re-emit path. No silent fallback.

## 6. Artifact lifecycle

Artifacts are **durable, keyed state held by the shim**, not per-turn outputs.

**Identity.** An artifact is identified by a name (string) within a session scope. The first `motion [<device>, artifact="foo"]:` turn creates an artifact; subsequent turns with the same `artifact="foo"` mutate it.

**URI scheme.**
- `memory://<name>` — session-scoped, held in shim memory. Default if no `path` attribute is given.
- `file://<path>` — backed by a file on disk. Adapter loads on first reference, saves on explicit `save` verb.
- Google / ODF / PDF / USD / STL targets are **not** URI schemes here; they are frontend export choices applied to the canonical backing at save time.

**State.** Each artifact holds three things:

1. Source representation (adapter-native: python-docx `Document`, pptx `Presentation`, openpyxl `Workbook`, canvas element list, trimesh `Scene`).
2. `§ID` → element table.
3. Render cache (invalidated on any mutation).

**Lifetime.** Session-scoped by default; persists across turns within a session. `save` flushes to `file://` backing. Cross-session access via `SESSION-REF` (already in protocol).

**Save semantics.**

```
motion [document, artifact="report", path="~/Drafts/q2.docx"]:
  save
```

`save` is the one authoring verb gated by `requires_confirm` when the target path exists (see §11 safety). `save` on a `memory://` artifact without a `path=` attribute errors as `save_needs_path` — explicit over implicit. To persist a memory-backed artifact, set `path=` on a subsequent turn's header and then `save`.

## 7. Placement and layout semantics

Three placement modes, selected by attribute:

| Form | Meaning | Applicable devices |
|---|---|---|
| `at X Y` | Absolute top-left placement in artifact coordinates | canvas, slides |
| `at=(X,Y,Z)` | Absolute placement in 3D world coordinates | scene |
| `parent=§ID("c")` | Flow placement inside a named layout container | canvas (via layout verbs), slides (via layout placeholders), scene (grouping) |
| *(neither)* | Flow placement at artifact root (current cursor / default layout) | document |

Rules:

- `at` on a top-level element: absolute.
- `at` on an element with a `parent=§ID(...)`: error (`at_in_flow_container`). Flow children are positioned by the container; override by removing `parent=`.
- `size=` is a **hint** to the layout engine when inside a flow container; it is a fixed dimension when at top level.
- Layout verbs (`stack`, `columns`, `grid`, `center`, `pad`, `flow`) create named containers that participate in Taffy's layout tree. Their children reference them by `parent=§ID("c")`. Containers may themselves have a `parent=`, enabling composition (a `columns` inside a `stack`).
- Flow in `document` is linear — elements emitted in order. `section` creates a sub-flow; `insert before|after §ID(x)` targets specific positions.

**Layout verbs retain their original design — we did not drop flow layout.** LLMs compose small orthogonal primitives well; the only thing we removed was the *nested-block grammar* around them.

## 8. Read-back (observation)

The LLM needs to see current artifact state to make edits. Read-back is **protocol-level observation**, not a verb — the shim attaches it to each turn's echo based on the profile's `auto_preview` field. This matches v5.12's SOM-overlay-after-desktop-action pattern.

**Two observation modes:**

**8.1 Tree listing (always available, free).**

Canonical wire form is **JSON** — stable, parseable, diffable:

```json
{
  "artifact": "q2-report",
  "device": "document",
  "pages": 3,
  "elements": [
    {"id": "title",          "kind": "heading", "level": 1, "text": "Q2 Report"},
    {"id": "intro",          "kind": "paragraph", "lines": 1},
    {"id": "revenue_chart",  "kind": "embed", "hash": "a4f9c1d2", "width": 500},
    {"id": "methodology",    "kind": "heading", "level": 2, "text": "Methodology"},
    {"id": "method-1",       "kind": "paragraph", "lines": 1}
  ]
}
```

The shim may render this as an ASCII tree for display, but the wire form the LLM reads back is JSON:

```
artifact "q2-report" (document, 3 pages, 5 elements)
├─ §ID("title")         heading 1   "Q2 Report"
├─ §ID("intro")         paragraph   (1 line)
├─ §ID("revenue_chart") image       embed @a4f9c1d2 w=500
├─ §ID("methodology")   heading 2   "Methodology"
└─ §ID("method-1")      paragraph   (1 line)
```

**8.2 Preview PNG (on request or `auto_preview: render`).**

Preview ladder:

| Device | Primary | Fallback |
|---|---|---|
| canvas | Cairo-native (native authoring → direct render, ~20ms) | n/a |
| document | LibreOffice headless (~1–3s warm, ~4–6s cold) | tree listing |
| slides | LibreOffice headless | tree listing |
| sheet | LibreOffice headless (scoped to viewport — see below) | tree listing |
| scene | pyrender offscreen GL (~50–200ms) | Blender `--background` (~2–5s) → tree listing |

**`auto_preview` policy (per profile):**

- `none` — no preview attached; tree listing only on explicit `preview` verb.
- `tree` (default) — tree listing attached after every turn. Zero render cost.
- `render` — preview PNG attached after every turn. Honors preview ladder.
- `render+overlay` — preview PNG with `§ID` badges overlaid on each element (same mechanism as SOM on a screenshot). For spatial edits. **Scene degrades to `render`** in v5.13 — camera-projection of 3D element positions to 2D badges with occlusion handling is deferred to v5.14; scene spatial reasoning uses tree listing in the meantime.

**Explicit `preview` verb** remains for one-off requests and for scoping large artifacts:

```
motion [slides, artifact="kickoff-deck"]:
  preview §SLIDE(3)                           # single-slide preview

motion [sheet, artifact="q2-budget"]:
  preview range=A1:Z100                       # scoped viewport preview
```

Preview scoping is mandatory for sheets > ~1000 cells and slide decks > ~10 slides — full-artifact render would dominate turn latency.

**8.3 Rich-client round-trip (canvas only, v5.13).**

When the shim ships to a rich client capable of rendering the artifact live (e.g. an Orville canvas panel), the client binds pointer events directly to the artifact's elements and syncs user edits back into the next turn as ordinary MOTION verb lines. Two protocol-level affordances make this work without extending the grammar:

- **`hit_map()`** — every canvas adapter exposes a `hit_map()` observation that returns one record per element: `{id, kind, z, rect, rotation?, scale?, flip?, pivot?, opacity?, parent?}`. Depth-first, z-ordered. The client uses it to mount hit-test regions and show selection handles without re-parsing the source.
- **Id-tagged SVG export.** `save path="x.svg"` emits `<g id="<element-id>" data-kind="<kind>" transform="…">` per element, so clients that embed the SVG directly can bind click/drag handlers straight to the DOM without a separate hit map.

User edits from the client (drag, resize, recolour, delete) are emitted back as standard `edit §ID(x) set at=(...)` / `set size=…` / `set fill=…` verb lines — they ride the existing edit grammar. No round-trip-specific verbs are added. The LLM sees the edited tree on the next turn via normal read-back.

## 9. `shim_profile.yaml` — authoring + content-type dispatch

Two retros on the existing profile shape:

**9.1 New `authoring:` section** — same envelope shape as `renderers:`:

```yaml
authoring:
  - id: canvas-author
    backend: cairo + pango + taffy
    outputs: [image/png, image/svg+xml, application/pdf]
    cost_usd_per_call: 0.00005                 # realistic: 500-element render
    p95_latency_ms: 200
    max_elements: 500
  - id: document-author
    backend: python-docx
    outputs: [application/vnd.openxmlformats-officedocument.wordprocessingml.document]
    cost_usd_per_call: 0.00010
    p95_latency_ms: 150
    max_pages: 200
  - id: slides-author
    backend: python-pptx
    outputs: [application/vnd.openxmlformats-officedocument.presentationml.presentation]
    cost_usd_per_call: 0.00010
    p95_latency_ms: 150
    max_slides: 100
  - id: sheet-author
    backend: openpyxl
    outputs: [application/vnd.openxmlformats-officedocument.spreadsheetml.sheet]
    cost_usd_per_call: 0.00008
    p95_latency_ms: 100
    max_sheets: 50
    max_cells: 500000
  - id: scene-author
    backend: trimesh
    outputs: [model/gltf-binary, model/gltf+json, model/obj, model/stl]
    cost_usd_per_call: 0.00010
    p95_latency_ms: 250
    max_primitives: 1000
```

**9.2 `accepts:` field on every renderer** — declares which chunk content types the renderer handles. Shim dispatches `embed {@HASH}` by first-match:

```yaml
renderers:
  - id: mermaid-to-png
    tool: mermaid-cli
    accepts: [text/vnd.mermaid, text/vnd.graphviz.dot, text/vnd.plantuml]
    output: image/png
    cost_usd_per_call: 0.0003
    p95_latency_ms: 420
  - id: plotly-to-png
    tool: plotly-orca
    accepts: [application/vnd.plotly.v1+json]
    output: image/png
    cost_usd_per_call: 0.0004
    p95_latency_ms: 600
  - id: latex-to-png
    tool: tectonic + pdf-render
    accepts: [text/x-latex, text/x-tex]
    output: image/png
    cost_usd_per_call: 0.0006
    p95_latency_ms: 900
  - id: svg-to-raster
    tool: resvg
    accepts: [image/svg+xml]
    output: image/png
    cost_usd_per_call: 0.00001
    p95_latency_ms: 18
  - id: html-to-image
    tool: playwright-chromium
    accepts: [text/html]
    output: image/png
    cost_usd_per_call: 0.0004
    p95_latency_ms: 650
  - id: office-preview
    tool: libreoffice-headless
    accepts:
      - application/vnd.openxmlformats-officedocument.wordprocessingml.document
      - application/vnd.openxmlformats-officedocument.presentationml.presentation
      - application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
      - application/vnd.oasis.opendocument.text
      - application/vnd.oasis.opendocument.spreadsheet
      - application/vnd.oasis.opendocument.presentation
    output: image/png
    cost_usd_per_call: 0.0002
    warm_p95_latency_ms: 400
    cold_p95_latency_ms: 3500
  - id: scene-preview
    tool: pyrender
    accepts: [model/gltf-binary, model/gltf+json]
    output: image/png
    cost_usd_per_call: 0.00005
    p95_latency_ms: 120
  - id: scene-preview-hq
    tool: blender-headless
    accepts: [model/gltf-binary, model/gltf+json, model/usd]
    output: image/png
    cost_usd_per_call: 0.002
    p95_latency_ms: 4500
```

Authoring backends and renderers participate in handshake negotiation the same way existing entries do. Absence of a backend disables the corresponding device profile or embed type (client sees `authoring_unavailable` or `no_renderer_for_content_type` accordingly).

## 10. Retro against existing capabilities

### 10.1 `GRAPH` (data type) — unchanged
`GRAPH` remains a bidirectional structured output type. No syntax change. A GRAPH chunk has a stable `{@HASH}` and can be `embed`ded from canvas / document / slides / scene (scene treats it as a textured plane).

### 10.2 Shim renderers — extended, not replaced
`mermaid-to-png`, `latex-to-png`, `plotly-to-png`, `svg-to-raster`, `html-to-image`, `vrm-to-video` stay as declared shim renderers. Each gains an `accepts:` list; embed dispatches by first-match against chunk content type.

### 10.3 `vrm-to-video` — shared glTF spine with `scene`
VRM is a glTF extension. The `scene` profile authors generic glTF; VRM continues to be a specialized character/avatar path via the existing `vrm-to-video` renderer. Future work may unify them (author a VRM character in `[scene]:` with an `extension=vrm` attribute) but v5.13 keeps them distinct.

### 10.4 `VISION-ANNOTATE` — verb naming shared, data path distinct
VISION-ANNOTATE's 8-primitive overlay grammar annotates *existing* imagery. Canvas authors *new* imagery. Semantically distinct; shim backends and safety envelopes stay separate. Verb names (`arrow`, `text`, `line`, `polygon`, `highlight`, `point`) overlap deliberately for shared mental model.

### 10.5 `{@HASH}` / SESSION-REF — reused as the single linking mechanism
Authoring adds no new reference mechanism for data chunks. `{@HASH}` for in-session refs, `{@session:sid/H…}` for cross-session refs (SESSION-REF, v5.9.2). The `§ID` sigil addresses *artifact elements*, not data chunks — the two are orthogonal.

## 11. Device profile YAMLs

Five new files in `ozmalib/devices/`:

**`canvas.yaml`:**

```yaml
type: canvas
schema: element-list
ref_dialect: ID
verbs:
  # drawing
  - rect
  - circle
  - line
  - polygon
  - path
  - arrow
  - text
  - image
  - gradient                               # gradient declaration (id + stops)
  # layout containers
  - stack
  - columns
  - grid
  - center
  - pad
  - flow
  # embed / edit / meta
  - embed
  - edit
  - insert
  - delete
  - move
  - preview
  - save
attributes:
  # style attributes — apply to any drawing verb, not standalone
  style: [fill, stroke, stroke_width, opacity, dash, font, size, align]
  layout: [at, parent, size, align]
edit_ops: [replace, append, prepend, set, unset, style]
observation:
  annotator: ozmalib.authoring.canvas.tree
  auto_preview: tree                       # none | tree | render | render+overlay
  max_elements: 500
transport:
  type: authoring-canvas
  backend: cairo + pango + taffy
safety:
  requires_confirm: [save]                 # save gated when target path exists
  requires_explicit_enable: false
  save_overwrite_policy: confirm           # confirm | deny | allow
  emergency_stop_action: abort-render
```

**`document.yaml`:**

```yaml
type: document
schema: flowable-list
ref_dialect: ID
verbs:
  - heading
  - paragraph
  - list
  - page_break
  - section
  - table
  - image
  - blockquote
  - hr
  - embed
  - edit
  - insert
  - delete
  - move
  - preview
  - save
attributes:
  inline: [bold, italic, link, code, footnote]   # applied via style= on element
  block:  [align, indent, page_break_before, page_break_after]
edit_ops: [replace, append, prepend, set, unset, style]
observation:
  annotator: ozmalib.authoring.document.tree
  auto_preview: tree
  max_elements: 2000
transport:
  type: authoring-document
  backend: python-docx
safety:
  requires_confirm: [save]
  requires_explicit_enable: false
  save_overwrite_policy: confirm
  emergency_stop_action: abort-render
```

**`slides.yaml`:**

```yaml
type: slides
schema: pptx-tree
ref_dialect: ID
verbs:
  - theme
  - master
  - slide_size
  - slide_new
  - slide_edit
  - slide_delete
  - slide_reorder
  - title
  - subtitle
  - bullet
  - text
  - image
  - table
  - shape
  - chart
  - embed
  - edit
  - insert
  - delete
  - move
  - preview
  - save
attributes:
  layout: [layout, parent, at, size, align]
  style:  [font, color, fill, align]
edit_ops: [replace, append, prepend, set, unset, style]
observation:
  annotator: ozmalib.authoring.slides.tree
  auto_preview: tree
  max_slides: 100
  max_elements_per_slide: 50
transport:
  type: authoring-slides
  backend: python-pptx
safety:
  requires_confirm: [save]
  requires_explicit_enable: false
  save_overwrite_policy: confirm
  emergency_stop_action: abort-render
```

**`sheet.yaml`:**

```yaml
type: sheet
schema: xlsx-tree
ref_dialect: A1
verbs:
  - sheet_new
  - sheet_delete
  - sheet_rename
  - set
  - clear
  - formula
  - format
  - merge
  - unmerge
  - freeze
  - width
  - height
  - paste
  - preview
  - save
attributes:
  format: [bold, italic, num_fmt, color, fill, align, border, font_size, font_family]
edit_ops: [set, clear, format, formula]
observation:
  annotator: ozmalib.authoring.sheet.tree
  auto_preview: tree
  max_sheets: 50
  max_cells: 500000
  max_cells_per_paste: 100000              # single paste chunk cap
transport:
  type: authoring-sheet
  backend: openpyxl
safety:
  requires_confirm: [save]
  requires_explicit_enable: false
  save_overwrite_policy: confirm
  emergency_stop_action: abort-render
```

**`scene.yaml`:**

```yaml
type: scene
schema: gltf-tree
ref_dialect: ID
coords:
  handedness: right
  up: y                                    # override via `motion [scene, up=z]:`
  units: meters
verbs:
  - cube
  - sphere
  - cylinder
  - cone
  - torus
  - plane
  - mesh
  - translate                              # standalone verb form for editing existing elements
  - rotate
  - scale
  - light_point
  - light_directional
  - light_ambient
  - camera
  - embed
  - edit
  - insert
  - delete
  - move
  - preview
  - save
attributes:
  material: [color, metallic, roughness, emissive, texture]
  transform: [at, rotation, scale, parent]
edit_ops: [replace, set, unset, style]
observation:
  annotator: ozmalib.authoring.scene.tree
  auto_preview: tree
  max_primitives: 1000
transport:
  type: authoring-scene
  backend: trimesh
safety:
  requires_confirm: [save]
  requires_explicit_enable: false
  save_overwrite_policy: confirm
  emergency_stop_action: abort-render
```

## 12. EBNF delta

Full additions against v5.12's grammar. Body shape remains **flat verb-line** (one verb per line); no nested blocks, no multi-line strings.

```
(* §4.1 MOTION_HEADER unchanged; DEVICE_ID extended with authoring names *)
DEVICE_ID   = (* existing *) | "canvas" | "document" | "slides"
            | "sheet" | "scene" ;

(* String literal — JSON-string rules: \" \\ \n \t \uHHHH *)
STRING      = '"' , { JSON_CHAR } , '"' ;

(* New ref sigils — peers to SOM / CSS / XPath / AID *)
ID_REF      = "§ID(" , STRING , ")" ;
SLIDE_REF   = "§SLIDE(" , UINT , ")" ;

(* REF alternatives extended *)
REF         = (* existing *) | ID_REF | SLIDE_REF ;

(* Target for edit/insert/delete/move/move-to verbs — optional SLIDE filter *)
TARGET      = [ SLIDE_REF , WS ] , ID_REF
            | SLIDE_REF ;                  (* SLIDE_REF alone addresses the slide itself *)

(* ID declaration attribute on any element-creating verb *)
ID_ATTR     = "id=" , STRING ;

(* Parent-reference attribute — ID or SLIDE for slides device *)
PARENT_ATTR = "parent=" , ( ID_REF | SLIDE_REF ) ;

(* Absolute placement attributes *)
AT_XY       = "at " , NUMBER , WS , NUMBER ;
AT_XYZ      = "at=(" , NUMBER , "," , NUMBER , "," , NUMBER , ")" ;
SIZE_WH     = "size=" , NUMBER , "x" , NUMBER ;
SIZE_XYZ    = "size=(" , NUMBER , "," , NUMBER , "," , NUMBER , ")" ;
WIDTH_ATTR  = "width=" , NUMBER ;

(* Generic attribute — key=value, value is number, string, or tuple *)
ATTR        = IDENT , "=" , VALUE ;
VALUE       = NUMBER | STRING | TUPLE | HASH_REF | IDENT ;
TUPLE       = "(" , VALUE , { "," , VALUE } , ")" ;

(* Tail of a verb line — the verb itself and its args; shared by INSERT *)
VERB_LINE_TAIL = VERB_NAME , { WS , ( VALUE | ATTR ) } ;
VERB_NAME   = IDENT ;                      (* the profile's verbs: rect, paragraph, bullet, etc. *)

(* Embed verb *)
EMBED_VERB  = "embed " , HASH_REF , [ WS , ( AT_XY | AT_XYZ | WIDTH_ATTR ) ]
            , [ WS , SIZE_WH | SIZE_XYZ ] , { WS , ATTR } , [ WS , ID_ATTR ] ;

(* Edit operation — one of a small core set, device-profile-constrained *)
EDIT_OP     = "replace" , WS , VALUE
            | "append"  , WS , VALUE
            | "prepend" , WS , VALUE
            | "set"     , WS , ATTR
            | "unset"   , WS , IDENT
            | "style"   , WS , ATTR ;

(* Edit / insert / delete / move verbs — target-first *)
EDIT_VERB   = "edit "   , TARGET , WS , EDIT_OP ;
INSERT_VERB = "insert " , ( "before" | "after" ) , WS , TARGET
            , WS , VERB_LINE_TAIL ;
DELETE_VERB = "delete " , TARGET ;
MOVE_VERB   = "move "   , TARGET , WS , "to" , WS , TARGET ;

(* Sheet A1 references — not sigil-prefixed by design; A1 is unambiguous *)
A1_REF      = UPPER_LETTERS , DIGITS ;
A1_RANGE    = A1_REF , ":" , A1_REF ;
```

Notes:

- Nested indented bodies (`stack … :` with indented children) are **not** in v5.13. Composition uses `parent=§ID(...)` on flat verbs.
- Strings use JSON rules — `\"`, `\\`, `\n`, `\t`, `\uHHHH`. No alternative quoting.
- `EDIT_OP` is device-profile-constrained. Each profile's YAML declares its supported ops under `edit_ops:`; profiles may decline ops (sheet declines `append`/`prepend` on cells; document declines `style` outside `set/unset`).
- `VERB_LINE_TAIL` after `INSERT_VERB` is the same syntax as a top-level verb line minus indentation — the inserted element is parsed identically to its standalone form.

## 13. Open questions

- **Element auto-naming format.** `<kind><N>` is chosen (readable, predictable). Alternative was opaque `e<N>`.
- **Sheet preview at scale.** LibreOffice struggles beyond ~10k cells. `preview range=A1:Z100` scoping is mandatory for large sheets; tree listing remains the unlimited fallback.
- **Concurrent edits.** Two agents mutating the same artifact via COORDINATION. Out of scope for v5.13; plausibly a v5.14 concern with the blocker-graph model.
- **LaTeX fidelity in `.docx`.** Embedding a LaTeX chunk into a `.docx` via PNG is correct for printing but loses equation fidelity Word supports natively via OMML. Lean: v5.13 does PNG-always; OMML is a v5.14 refinement.
- **Inline embed escape hatch.** Every embed goes through a separate chunk turn. If friction pushes back, `embed graph { ... }` as inline-hash-on-the-fly sugar can land later without breaking `embed {@HASH}`.
- **Cache miss on cross-session embed.** Handled as `chunk_not_resident` error; re-emit is the remedy. An alternative is durable chunk storage for referenced-across-sessions chunks — deferred.
- **Category rename.** v5.12 renamed "Embodied" → "Control" when MOTION absorbed UI. v5.13 extends to authoring, which is neither physical nor UI control — it's *production*. Candidates: "Effects" (broadest), "Output", "Control" retained with stretched meaning. Lean: **"Effects"** — proposed rename, deferrable to a later pass.
- **Scene animation / skinning / physics.** Not in v5.13. Would be a meaningful v5.14 addition; likely adds a `timeline` primitive and `animate` verb.
- **Sense-side conflict policy (§3.7).** When a tier-2 detector and a tier-3 VLM disagree on the same region (e.g., detector says "car", VLM says "truck"), which observation is canonical? v5.13 ships both facts with per-field confidences and lets the model arbitrate; whether the protocol should impose a conflict-resolution rule is open. Literature is unsettled.
- **`§IMG` scope lifetime.** Turn-scoped today (index into the current turn's payload). Cross-turn image references may want an `{@HASH}`-backed alternative when the same image recurs across turns; deferrable.
- **Multi-pass prompt preprocessing (§3.7.1).** Literature (LLMLingua-family, RECOMP, DSPy, cAST, HyDE/Query2Doc) occupies isolated slices; no unified framework trains a model against shim-emitted preprocessor output as the canonical training distribution. Questions: which passes belong in sense[prompt] tiers vs leaves; how much of the compression budget is structural (deterministic) vs stochastic (LLMLingua-style); interaction with prompt caching (naive full-context caching can *increase* agentic-loop latency — arXiv:2601.06007); whether query rewriting belongs in the default pipeline given mixed-result literature. Lean: structural compression (`compress_context`) + conditional hints + intent/entity extraction are default; stochastic token compression and query rewriting are opt-in higher tiers behind explicit verbs.
- **Cascaded routing vs preprocessing boundary.** FrugalGPT (arXiv:2305.05176) / RouteLLM (arXiv:2406.18665) / cascade-routing (arXiv:2410.10347) treat cheap→expensive model selection as the efficiency lever. Ozma's `classify_task` + `pick_model` overlap; the protocol boundary between `sense [prompt]:` classification outputs and routing decisions is an open design question. Lean: sense emits classification + confidence; routing is downstream shim policy, not protocol-level.
- **CoT / reasoning-chain compression.** "Adaptive CoT Compression" (arXiv:2509.14093) documents accuracy drops from compressing reasoning chains on small models. Whether prior-turn reasoning blocks go through `compress_context`-style stubbing or are preserved verbatim is a shim policy question; the protocol should stay neutral but the defaults need thought.
- **Classification-preserving paraphrase as training objective (§3.10.4).** The lattice requires the model to emit output whose labels equal the join of its inputs' labels, even when the value has been paraphrased / summarised / translated. Shim-side re-classification by regex + fuzzy match catches the obvious cases; subtle paraphrase (e.g., "the person who sent the email" as a referential substitute for the named individual) requires model-side awareness that the reference still carries the `§PII` label. Training recipe, eval construction, and acceptable-loss thresholds are open. Likely a separate RFC + paper.
- **Covert channels (§3.10 out of scope).** DIFC literature has 30 years of work on timing, termination, probabilistic, and cache side channels (Volpano-Smith-Irvine, Zdancewic, Sabelfeld-Myers). The v5.13 lattice addresses value flow only. Applying the non-interference proof techniques from Jif / FlowCaml to the shim+LLM boundary is open — plausibly a v5.15 concern when formal guarantees become a certification requirement.
- **Capability model vs lattice model (§3.10 vs CaMeL).** CaMeL (arXiv:2503.18813, Mar 2025) chose capabilities-per-value over a lattice; FIDES (arXiv:2505.23643, May 2025) chose taint labels. v5.13 chose a lattice because classification tiers compose via join cleanly and map to existing regulatory vocabulary (SECRET / CONFIDENTIAL / PUBLIC, tier 0/1/2/3). The capability model is more expressive for fine-grained planner-output restriction; the lattice model is more auditable and maps to existing compliance frameworks. Whether the two should coexist (capabilities for planner flow, lattice for data flow) is open.
- **Retroactive re-labelling.** When a label definition changes (a new kind of PII is recognised; a jurisdiction changes its data-residency rules), how do historic trajectories / corpora / compressed IR get re-classified? Deferred to v5.14+ with provenance graphs.

## 14. Out of scope

- **Format choice.** The protocol authors in canonical Office formats (`.docx`, `.pptx`, `.xlsx`), vector canvas, and `.glb`. Export to `.pdf` / Google Doc / `.odt` / `.usd` / `.stl` / print is a frontend concern at save/export time.
- **Collaborative editing signals.** Comments, suggestions, revision tracking, presence. Later profile extension.
- **Rich templating.** Corporate themes, style sheets, branded templates. Addressable via `theme` / `master` attributes but not standardized in v5.13.
- **Full bidirectional fidelity with Google Docs.** Export/import round-trip via `.docx` / `.xlsx` / `.pptx` is sufficient for authoring; full Google API fidelity is a later option.
- **Animation / physics / skinning for scene.** Static scenes only.
- **Audio / video timeline authoring.** Distinct enough from the spatial/layout profiles that they deserve their own device profile design; not in v5.13.
- **`sense [audio]:` / `sense [video]:` / `sense [log]:` / `sense [sensor]:`.** Reserved siblings to the vision adapter (§3.7); each follows the same shim-side-decomposition pattern but has its own observation schema and tier ladder. Out of scope for v5.13; follow-on RFCs.
- **Training corpus definition and schema-conformance tests.** The sense schema is a training contract, but v5.13 specs only the contract. Dataset shape, model-side conformance tests, and reference training recipes are a paper, not a protocol RFC.
- **IFC formal soundness.** §3.10 defines propagation rules but does not prove non-interference. Mature DIFC proof techniques (Volpano-Smith-Irvine; Zdancewic; Sabelfeld-Myers) exist; applying them across the shim+LLM boundary is a follow-on, likely paired with the certification-tier v5.14+ work.
- **Model-side classification-preserving generation recipes.** See §13 open question; separate RFC + paper.
- **Cryptographic tenant isolation.** Encryption-at-rest keyed by `§CONF(tenant=…)` label is orthogonal to the flow-control layer; v5.13 specifies the labels, not the crypto.

## 15. Execution plan

1. **This RFC note** — shipped as `research/docs/protocol-v5.13-authoring.md`.
2. **Canvas profile end-to-end** — first concrete implementation. Cairo + Pango spine; verb interpreter; `§ID` table; flow layout via Taffy; `embed` dispatcher against the `accepts:` table; one end-to-end test (draw a bar chart).
3. **Document profile** — python-docx adapter; edit-mode against `§ID`. Most intricate (flow / pagination interaction).
4. **Slides + sheet** — mechanical once 1–3 land; reuse shape.
5. **Scene profile** — trimesh + pyrender spine; landed after 2–4 because the geometry primitive set can stabilise against authoring patterns first.

Each step produces tests under `research/tests/test_authoring_*.py`, a device profile YAML, and one adapter module under `research/ozmalib/authoring/`.

## 16. Changelog summary (for `protocol-CHANGELOG.md`)

```
- v5.13 (TBD): Authoring device profiles — canvas / document / slides
  / sheet / scene under MOTION. New §ID ref sigil and §SLIDE(N) scope
  locator; new `embed {@HASH}` verb with shim-profile-declared
  content-type dispatch. Data/rendering split preserved: authoring
  never duplicates data, only references it; existing GRAPH / PLOTLY /
  LATEX / SVG / HTML / glTF chunks compose into artifacts via the
  declared-renderer pattern, which gains an `accepts:` list per
  renderer. New `authoring:` section in `shim_profile.yaml`; new
  `office-preview` (LibreOffice) and `scene-preview` (pyrender) /
  `scene-preview-hq` (Blender) renderers.

  Body shape remains flat verb-line family (no nested blocks, no
  multi-line strings); composition uses `parent=§ID(...)` attributes
  on children. Read-back is a protocol-level observation (tree
  listing default, preview PNG opt-in) — not a verb — matching
  v5.12's SOM-after-desktop-action pattern. `save` is the one
  authoring verb gated by `requires_confirm` with
  `save_overwrite_policy`. Frontend owns final format choice
  (.docx / .pdf / gdoc / .odt / .glb / .usd / print); protocol
  authors in canonical Office / glTF formats.

  Canvas profile ships with Canva-level primitive parity: ellipse /
  star / arc shapes, rounded rect (`radius=`), rotation / scale /
  flip / pivot transforms, shadow / blur / glow / blend / opacity
  effects, linear+radial gradients and image patterns, Pango markup
  with letter-spacing / line-height / text-stroke / text-shadow,
  image filters (brightness / contrast / saturation / hue /
  grayscale / sepia / invert / crop), grouping (`group`), clipping
  (`clip`, `frame=§ID`), explicit z-ordering, and multi-format
  export (PNG / SVG / single-page PDF / JPEG). Rich-client
  round-trip: `hit_map()` observation + id-tagged SVG (`<g id="…">`
  per element) let a rich client bind pointer events and sync user
  edits back via the existing `edit §ID(x) set …` grammar — no
  round-trip-specific verbs added.

  Document profile is bidirectional: `Document.from_docx(path)` reads
  an existing `.docx` back into the block model with full structural
  fidelity (headings 1–6, paragraphs, lists, tables, blockquote,
  page_break, section orientation, hr, inline images via sha256
  `chunk_sink` callback, paragraph-wide run style when every run
  agrees); mixed-run paragraphs collapse to plain text and carry
  `_fidelity="partial_runs"` for auditing. `to_motion_body()` emits
  the inverse of `apply()` — a motion verb-line body that reparses
  into an equivalent block sequence. An LLM handed a user `.docx`
  edits it with §ID reference fidelity instead of receiving a
  formatting-stripped text blob.

  New `motion [diagram]:` profile (§3.6) — draw.io / mxGraph backend.
  Graph-native primitives (`node` / `edge §ID(src) §ID(tgt)` /
  `swimlane` / `group` / `label`) with raw drawio style string
  passthrough. Canonical output `.drawio` (mxfile XML) and
  `.drawio.svg` (SVG with embedded `content=` — visually renderable
  AND drawio-editable). Rich-client round-trip via embedded draw.io
  instance (`transport.rich_client: drawio-embed`): the client runs
  the real drawio editor, user edits reserialise to mxfile,
  `Diagram.from_drawio()` re-ingests to the block model, and the LLM
  sees the user's visual edits as standard `edit §ID(x) set …`
  lines. Collaborative diagram design: AI drafts structure, user
  refines visuals in the real editor, AI extends structure with
  visual context intact.

  New **efficiency-surface family** opened (§3.7 + §3.8) — symmetric
  shim passes around the model: `sense [...]` on input, `validate
  [...]` on output. Thesis: the LLM is the expensive probabilistic
  oracle; surround it with cheap deterministic shim passes so its
  capacity is reserved for the irreducibly probabilistic core.
  Freed capacity = faster + more accurate + more capable, zero
  architectural change to the model. Ozma ships this pattern in
  production today — `compress_context` (tree-sitter AST stubs +
  Ozma IR, 50–97% token reduction, zero quality loss), hints (136
  YAML regex-driven conditional guidance injectors), `classify_task`
  on the input side; checks (15 regex validators), rules (64
  extended-action YAML), renderers, and the §3.1–3.6 authoring
  verbs on the output side. v5.13 names these in the protocol so
  the schema becomes a training contract on both sides. Literature
  survey (LLMLingua-family compression; RECOMP retrieval
  compression; DSPy prompt optimisation; FrugalGPT / RouteLLM
  cascaded routing; ToolLLM / Gorilla schema-aware training;
  OpenAI Structured Outputs; cAST / Repomix structural code
  compression) confirms the individual passes exist — the
  unification into a single shim-owned training contract across
  input AND output does not.

  §3.7.1 `sense [prompt]:` (natural-language user input, ships today
  via hints + compress_context + classify_task) — observation
  schema with intents, entities (paths / symbols / URLs / errors /
  stack traces), pasted chunks → `{@HASH}` + detected language +
  Ozma-IR stub view, framework/language/domain context, hints
  fired, migration detection. Reserved verbs: `expand §ID(c1)`,
  `reclassify intent=…`, `suppress hint=…`. Multi-pass prompt
  analysis (stochastic compression, query decomposition, retrieval
  preprocessing) slots in as additional tiers / observation fields
  without grammar change.

  §3.7.2 `sense [vision]:` (first pixel-modality adapter) — any turn
  whose payload contains `image/*` receives an auto-attached
  `sense [vision, image=N]:` observation at the shim's default
  tier. Observation JSON with mandatory per-field `confidence`, an
  `available: true|false` envelope separating *not measured* from
  *measured and empty*, and region ids in the shared `§ID`
  namespace. Verbs: `upgrade tier=N`, `focus §ID(r) ocr=true`,
  `describe §ID(r)`, `compare §IMG(0) §IMG(1)`. Tier ladder
  (0 Pillow-only <10ms/MP; 1 OCR + cheap detectors <100ms/MP; 2
  model-backed detectors + depth + segmentation <1s/MP; 3 VLM
  escalation) declared in `shim_profile.yaml` — backends
  (YOLO-NAS vs RT-DETR etc.) are shim choices, not protocol
  fixtures. New sigil `§IMG(N)` for turn-scoped image references.
  `sense [audio]:` / `sense [video]:` / `sense [log]:` / `sense
  [sensor]:` reserved for follow-on RFCs.

  §3.8.1 `validate [checks]:` (regex validators, ships today) — 15
  built-in checks (f-string SQL, `pickle.loads`, `eval`, shell
  injection, hardcoded secrets, AWS keys, weak hashes) with
  `warn` / `resubmit` / `fix` / `run` / `block` actions.
  Observation schema attaches to each turn echo after model
  output: `fired` list with severity / action / match / feedback /
  confidence, plus turn-level `outcome` (`accept` / `resubmit` /
  `fixed` / `blocked`). Model may emit `ack`, `suppress id=… reason=…`,
  `remediate`.

  §3.8.2 `validate [rules]:` (extended-action rules, ships today) —
  64 built-in, `suggest` / `fetch_spec` / `score` / `run` action
  space. Same YAML engine as hints and checks. Project-specific
  checks and rules drop into `.checks/` / `.rules/` at repo root.

  Training contract on both sides: a model trained against the
  sense schema (input side) and aware of the validate schema
  (output side) is faster (fewer re-extractions, fewer
  re-validations), more first-try accurate (capacity spent on
  reasoning; output distribution shaped by known validation),
  and safer by default (security checks form hard constraints
  the model learns to avoid rather than rediscover). Training
  data generates mechanically — the shim *is* the annotator. A
  schema-trained model is portable to any schema-conformant shim.
  The protocol becomes the **unit of transferable capability**
  on both sides.

  New **cross-device convergence primitive** `converge` (§3.9) —
  not a device profile; a shape every `motion` / `sense` adapter
  can declare support for. Shim owns the inner closed-loop; model
  sets goal + termination criteria + optional method hint; the
  terminal observation carries outcome (converged / timeout /
  cost_exhausted / aborted / blocked / bailed), final state,
  iteration count, duration, cost breakdown (ms / energy /
  model_calls / usd), trajectory digest (samples / max_error /
  oscillations / overshoot / settling_time), method actually
  used, system-identification summary (adapted / regime_change /
  plant_confidence), and a `telemetry_hash` pointing at the full
  trajectory as a `{@HASH}` chunk for audit. Goal kinds cover
  point / predicate / top-K-threshold / optimization / range.
  Methods are declared in `shim_profile.yaml` with
  `applicable_to` / `goal_kinds` / `certifiable` fields so
  deployments can pick methods matching regulatory context
  (DO-178C / ISO 26262 / IEC 61508 / IEC 62304 precedent for
  classical adaptive control). Ancillary verbs `abort` /
  `status` / `await` plus new `§CONV("handle")` sigil for async
  loops. Unwind policy (commit vs revert on bail) declared
  per-device. This is the load-bearing primitive every future
  world-interface adapter reuses — robot / nav / game / 3d /
  map on the physical side, retrieval / grounding / optimization
  on the software side. The efficiency thesis in one verb: model
  sets *what*, shim owns *how*. Biological parallel: neocortex
  reads terminal state; spinal reflex owns the loop.

  New **information-flow control lattice** (§3.10) — three new
  ref sigils (`§SECRET`, `§PII`, `§CONF`) introduce a four-tier
  classification lattice over every value the protocol carries.
  Tier 0 `§SECRET` handles are opaque to the model; the shim
  dereferences only at declared egress scope (HTTP header, SQL
  parameter, SSH agent, etc.) with the PCI/EMV tokenisation
  precedent — the model cannot leak what it never holds. Tier 1
  `§PII` handles are processable (model reads the value to
  reason about it) but jurisdiction-gated at egress; the label
  propagates through paraphrase / summary / translation with
  lattice-join semantics. Tier 2 `§CONF` handles carry tenant
  scope and destination-class routing, making multi-tenant B2B
  isolation a shim concern rather than prompt engineering. New
  `validate [classify]:` sibling enforces at egress with
  `block` / `redact` / `escalate` / `pseudonymise` actions.
  Labels propagate through `trajectory_digest` (consumer-
  clearance join at read time), training-corpus extraction
  (tier 0 excluded or handle-only; tier 1 pseudonymised), and
  `compress_context` / Ozma IR literal emission (classifiable
  literals promoted to handles before IR output).
  Declassification is shim-owned and always logged. Prior art:
  four decades of OS-level DIFC (Jif / Asbestos / HiStar /
  Flume / DStar / Resin / Laminar), dynamic taint tracking
  (TaintDroid, CodeQL, Pysa), PCI/EMV tokenisation for tier 0,
  and two 2025 LLM-agent IFC papers (CaMeL [arXiv:2503.18813,
  Google DeepMind], FIDES [arXiv:2505.23643, Microsoft
  Research]). Positioning: not first IFC-for-agents, but first
  to apply a full lattice across sense / validate / trajectory /
  training / compression as coequal enforcement boundaries,
  integrate tier-0 tokenisation with tier-1/2 processable labels
  in one schema, and make classification-preserving paraphrase
  a training-contract objective. Makes GDPR / HIPAA / PCI / SOC2
  compliance structural rather than procedural; makes
  prompt-injection credential exfiltration impossible by
  construction at tier 0.
```
