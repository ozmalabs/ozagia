"""Self-contained HTML for a graph view model.

The page is one file: inline CSS, inline SVG and a small vanilla script. It has
no external stylesheet, font, image, script or network request. Labels and
attributes are escaped; the embedded view JSON is escaped for a script element
and the inspector writes values with ``textContent``, never ``innerHTML``.

Layouts are computed deterministically in Python, so the page is useful with
JavaScript disabled. JavaScript only adds the node inspector and selection.
"""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any

from ozagia.core.view import node_dates

_LAYOUT_WIDTH = 960
_LAYOUT_HEIGHT = 640
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_DEFAULT_COLOR = "#5b6b73"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _color(value: Any) -> str:
    text = str(value or "")
    return text if _HEX_RE.fullmatch(text) else _DEFAULT_COLOR


def _node_color(node: dict[str, Any], colors: dict[str, str]) -> str:
    return _color(colors.get(str(node.get("group")), _DEFAULT_COLOR))


def _weight(node: dict[str, Any]) -> float:
    try:
        value = float(node.get("weight", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _json_for_script(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False)
    return (text.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _rng(seed: int):
    state = (int(seed) or 1) & 0x7FFFFFFF

    def next_float() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    return next_float


def _edge_pairs(edges: list[dict[str, Any]],
                positions: dict[str, tuple[float, float]]
                ) -> list[tuple[dict[str, Any], tuple[float, float],
                                tuple[float, float]]]:
    out = []
    for edge in edges:
        src = positions.get(str(edge["src"]))
        tgt = positions.get(str(edge["tgt"]))
        if src is not None and tgt is not None:
            out.append((edge, src, tgt))
    return out


def _force_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                  seed: int) -> dict[str, Any]:
    count = len(nodes)
    width = max(_LAYOUT_WIDTH, min(1800, 700 + count * 4))
    height = max(_LAYOUT_HEIGHT, min(1200, 480 + count * 3))
    if count == 0:
        return {"positions": {}, "width": width, "height": height, "axis": []}
    if count == 1:
        node_id = str(nodes[0]["id"])
        return {"positions": {node_id: (width / 2, height / 2)},
                "width": width, "height": height, "axis": []}

    rand = _rng(seed)
    ids = [str(node["id"]) for node in nodes]
    positions = {}
    for index, node_id in enumerate(ids):
        angle = 2 * math.pi * index / count + rand() * 0.12
        radius = min(width, height) * (0.18 + 0.22 * rand())
        positions[node_id] = (
            width / 2 + math.cos(angle) * radius,
            height / 2 + math.sin(angle) * radius,
        )
    pairs = [(str(edge["src"]), str(edge["tgt"])) for edge in edges
             if str(edge["src"]) in positions and str(edge["tgt"]) in positions]
    k = math.sqrt((width * height) / max(1, count))
    temperature = width / 8.0
    iterations = 60 if count <= 120 else 36
    margin = 48.0
    for _ in range(iterations):
        movement = {node_id: [0.0, 0.0] for node_id in ids}
        for i, left_id in enumerate(ids):
            lx, ly = positions[left_id]
            for right_id in ids[i + 1:]:
                rx, ry = positions[right_id]
                dx, dy = lx - rx, ly - ry
                distance = math.hypot(dx, dy)
                if distance < 0.01:
                    dx, dy, distance = 0.01 * (i + 1), 0.01, 0.02
                force = (k * k) / distance
                ux, uy = dx / distance, dy / distance
                movement[left_id][0] += ux * force
                movement[left_id][1] += uy * force
                movement[right_id][0] -= ux * force
                movement[right_id][1] -= uy * force
        for src, tgt in pairs:
            sx, sy = positions[src]
            tx, ty = positions[tgt]
            dx, dy = sx - tx, sy - ty
            distance = math.hypot(dx, dy) or 0.01
            force = (distance * distance) / k
            ux, uy = dx / distance, dy / distance
            movement[src][0] -= ux * force
            movement[src][1] -= uy * force
            movement[tgt][0] += ux * force
            movement[tgt][1] += uy * force
        for node_id in ids:
            mx, my = movement[node_id]
            distance = math.hypot(mx, my) or 1.0
            step = min(distance, temperature)
            x, y = positions[node_id]
            positions[node_id] = (
                max(margin, min(width - margin, x + mx / distance * step)),
                max(margin, min(height - margin, y + my / distance * step)),
            )
        temperature *= 0.94
    return {
        "positions": {key: (round(x, 1), round(y, 1))
                      for key, (x, y) in positions.items()},
        "width": width,
        "height": height,
        "axis": [],
    }


def _grid_layout(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(nodes)
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    width = max(_LAYOUT_WIDTH, columns * 180)
    height = max(420, rows * 90)
    positions = {}
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        positions[str(node["id"])] = (
            width * (column + 0.5) / columns,
            height * (row + 0.5) / rows,
        )
    return {"positions": positions, "width": width, "height": height,
            "axis": []}


def _layered_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
                    ) -> dict[str, Any]:
    if not nodes:
        return {"positions": {}, "width": _LAYOUT_WIDTH,
                "height": _LAYOUT_HEIGHT, "axis": []}
    ids = [str(node["id"]) for node in nodes]
    known = set(ids)
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in ids}
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        src, tgt = str(edge["src"]), str(edge["tgt"])
        if src not in known or tgt not in known or src == tgt:
            continue
        if (src, tgt) in seen_edges:
            continue
        seen_edges.add((src, tgt))
        adjacency[src].append(tgt)
        indegree[tgt] += 1
    if not seen_edges:
        return _grid_layout(nodes)

    layers: dict[str, int] = {node_id: 0 for node_id in ids}
    ready = sorted((node_id for node_id in ids if indegree[node_id] == 0),
                   key=str)
    processed: set[str] = set()
    while ready:
        node_id = ready.pop(0)
        processed.add(node_id)
        for other in sorted(adjacency[node_id], key=str):
            layers[other] = max(layers[other], layers[node_id] + 1)
            indegree[other] -= 1
            if indegree[other] == 0:
                ready.append(other)
                ready.sort(key=str)
    leftover = sorted((node_id for node_id in ids if node_id not in processed),
                      key=str)
    if leftover:
        next_layer = max(layers.values(), default=-1) + 1
        for node_id in leftover:
            layers[node_id] = next_layer
    if len(set(layers.values())) == 1:
        return _grid_layout(nodes)

    by_layer: dict[int, list[dict[str, Any]]] = {}
    node_by_id = {str(node["id"]): node for node in nodes}
    for node_id in ids:
        by_layer.setdefault(layers[node_id], []).append(node_by_id[node_id])
    for items in by_layer.values():
        items.sort(key=lambda node: (str(node.get("group", "")),
                                     str(node["id"])))
    layer_count = max(by_layer) + 1
    max_rows = max(len(items) for items in by_layer.values())
    width = max(_LAYOUT_WIDTH, layer_count * 260)
    height = max(420, max_rows * 76)
    positions = {}
    for layer, items in by_layer.items():
        x = (width * (layer + 0.5) / layer_count)
        for index, node in enumerate(items):
            y = height * (index + 0.5) / len(items)
            positions[str(node["id"])] = (x, y)
    return {"positions": positions, "width": width, "height": height,
            "axis": []}


def _timeline_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
                     ) -> dict[str, Any]:
    if not nodes:
        return {"positions": {}, "width": _LAYOUT_WIDTH,
                "height": _LAYOUT_HEIGHT, "axis": []}
    dates = node_dates(nodes)
    groups = sorted({str(node.get("group", "")) for node in nodes})
    node_by_id = {str(node["id"]): node for node in nodes}
    dated = sorted({dates[node_id] for node_id in dates})
    undated = [node_id for node_id in node_by_id if node_id not in dates]
    columns = dated + (["unknown"] if undated else [])
    if not columns:
        columns = ["unknown"]
    width = max(_LAYOUT_WIDTH, len(columns) * 130)
    stack_limit: dict[tuple[str, str], int] = {}
    positions: dict[str, tuple[float, float]] = {}
    axis: list[tuple[float, str]] = []
    column_x = {name: width * (index + 0.5) / len(columns)
                for index, name in enumerate(columns)}
    for column in columns:
        if column != "unknown":
            axis.append((column_x[column], column))
    max_stack = 0
    ordered = sorted(
        nodes,
        key=lambda node: (
            columns.index(dates.get(str(node["id"]), "unknown")),
            str(node.get("group", "")),
            str(node["id"]),
        ),
    )
    for node in ordered:
        node_id = str(node["id"])
        column = dates.get(node_id, "unknown")
        group = str(node.get("group", ""))
        key = (column, group)
        stack = stack_limit.get(key, 0)
        stack_limit[key] = stack + 1
        max_stack = max(max_stack, stack + 1)
        x = column_x[column]
        y = 90 + groups.index(group) * 78 + stack * 26
        positions[node_id] = (x, y)
    height = max(420, 90 + len(groups) * 78 + max_stack * 26)
    return {"positions": positions, "width": width, "height": height,
            "axis": axis}


def _layout(view: dict[str, Any]) -> dict[str, Any]:
    nodes = view.get("nodes", [])
    edges = view.get("edges", [])
    kind = str((view.get("layout") or {}).get("kind", "force"))
    seed = int((view.get("layout") or {}).get("seed", 7))
    if kind == "layered":
        return _layered_layout(nodes, edges)
    if kind == "timeline":
        return _timeline_layout(nodes, edges)
    return _force_layout(nodes, edges, seed)


def _svg_graph(view: dict[str, Any]) -> str:
    nodes = view.get("nodes", [])
    edges = view.get("edges", [])
    layout = _layout(view)
    positions = layout["positions"]
    width, height = layout["width"], layout["height"]
    colors = (view.get("legend") or {}).get("color", {})
    parts = [
        f'<svg id="graph-svg" viewBox="0 0 {width} {height}" '
        f'data-layout="{_esc((view.get("layout") or {}).get("kind", "force"))}" '
        'role="img" aria-label="Graph view">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8a99a8"></path></marker></defs>',
        '<g class="edges">',
    ]
    for edge, (sx, sy), (tx, ty) in _edge_pairs(edges, positions):
        label = (f"{edge.get('raw_type', edge.get('type'))}: "
                 f"{edge.get('src')} → {edge.get('tgt')}")
        parts.append(
            f'<line x1="{sx}" y1="{sy}" x2="{tx}" y2="{ty}" '
            'stroke="#b7c2cc" stroke-width="1.2" marker-end="url(#arrow)" '
            f'data-edge-id="{_esc(edge.get("id", ""))}">'
            f'<title>{_esc(label)}</title></line>')
    parts.append('</g><g class="nodes">')
    for node in nodes:
        node_id = str(node["id"])
        x, y = positions[node_id]
        radius = 8 + 14 * min(1.0, math.sqrt(_weight(node)))
        color = _node_color(node, colors)
        label = _short(node.get("label", node_id), 44)
        parts.append(
            f'<g class="node" data-node="{_esc(node_id)}" tabindex="0" '
            f'role="button" aria-label="{_esc(node.get("label", node_id))}">'
            f'<circle cx="{x}" cy="{y}" r="{radius:.1f}" fill="{color}" '
            'fill-opacity="0.88" stroke="#ffffff" stroke-width="1.5"></circle>'
            f'<text x="{x}" y="{y + radius + 13}" text-anchor="middle" '
            f'class="node-label">{_esc(label)}</text>'
            f'<title>{_esc(node.get("label", node_id))} '
            f'[{_esc(node.get("type", "other"))}]</title></g>')
    parts.append('</g>')
    if layout["axis"]:
        parts.append('<g class="axis">')
        for x, label in layout["axis"]:
            parts.append(
                f'<text x="{x}" y="{height - 18}" text-anchor="middle" '
                f'class="axis-label">{_esc(label)}</text>')
        parts.append('</g>')
    parts.append('</svg>')
    return "\n".join(parts)


def _matrix_graph(view: dict[str, Any]) -> str:
    nodes = view.get("nodes", [])
    edges = view.get("edges", [])
    if not nodes:
        return '<p class="empty">No nodes in this view.</p>'
    colors = (view.get("legend") or {}).get("color", {})
    cell = max(7.0, min(24.0, 700.0 / max(1, len(nodes))))
    left, top = 170.0, 150.0
    width = left + len(nodes) * cell + 24
    height = top + len(nodes) * cell + 24
    index = {str(node["id"]): position for position, node in enumerate(nodes)}
    edge_cells: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for edge in edges:
        src, tgt = index.get(str(edge["src"])), index.get(str(edge["tgt"]))
        if src is not None and tgt is not None:
            edge_cells.setdefault((src, tgt), []).append(edge)
    parts = [
        f'<svg id="graph-svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        'data-layout="matrix" role="img" aria-label="Adjacency matrix">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"></rect>',
    ]
    lines = []
    for offset in range(len(nodes) + 1):
        x = left + offset * cell
        y = top + offset * cell
        lines.append(f"M{x:.1f} {top:.1f}V{top + len(nodes) * cell:.1f}")
        lines.append(f"M{left:.1f} {y:.1f}H{left + len(nodes) * cell:.1f}")
    parts.append(f'<path d="{" ".join(lines)}" stroke="#e3e8ee" '
                 'stroke-width="0.7" fill="none"></path>')
    for (row, column), items in sorted(edge_cells.items()):
        source = nodes[row]
        color = _node_color(source, colors)
        label = " | ".join(
            f"{item.get('raw_type', item.get('type'))}: "
            f"{item.get('src')} → {item.get('tgt')}" for item in items)
        parts.append(
            f'<rect x="{left + column * cell:.1f}" '
            f'y="{top + row * cell:.1f}" width="{cell:.1f}" '
            f'height="{cell:.1f}" fill="{color}" fill-opacity="0.9">'
            f'<title>{_esc(label)}</title></rect>')
    label_step = 1 if len(nodes) <= 70 else max(1, len(nodes) // 40)
    for position, node in enumerate(nodes):
        if position % label_step:
            continue
        node_id = str(node["id"])
        color = _node_color(node, colors)
        y = top + position * cell + cell / 2
        parts.append(
            f'<rect x="{left - 150:.1f}" y="{y - 5:.1f}" width="9" '
            f'height="9" fill="{color}"></rect>')
        parts.append(
            f'<text x="{left - 8:.1f}" y="{y:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" class="axis-label" '
            f'data-node="{_esc(node_id)}">{_esc(_short(node.get("label", node_id), 20))}'
            '</text>')
        x = left + position * cell + cell / 2
        parts.append(
            f'<text x="{x:.1f}" y="{top - 8:.1f}" text-anchor="start" '
            f'transform="rotate(-55 {x:.1f} {top - 8:.1f})" '
            f'class="axis-label" data-node="{_esc(node_id)}">'
            f'{_esc(_short(node.get("label", node_id), 20))}</text>')
    parts.append('</svg>')
    return "\n".join(parts)


def _table_graph(view: dict[str, Any]) -> str:
    nodes = view.get("nodes", [])
    edges = view.get("edges", [])
    colors = (view.get("legend") or {}).get("color", {})
    parts = ['<div class="tables">']
    parts.append('<h3>Nodes</h3>')
    if nodes:
        parts.append('<table class="node-table"><thead><tr><th>Type</th>'
                     '<th>Label</th><th>Group</th><th>Weight</th>'
                     '<th>Key</th></tr></thead><tbody>')
        for node in nodes:
            color = _node_color(node, colors)
            parts.append(
                f'<tr data-node="{_esc(node.get("id", ""))}" tabindex="0">'
                f'<td>{_esc(node.get("type", "other"))}</td>'
                f'<td><span class="swatch" style="background:{color}"></span>'
                f'{_esc(node.get("label", ""))}</td>'
                f'<td>{_esc(node.get("group", ""))}</td>'
                f'<td>{_weight(node):.3f}</td>'
                f'<td><code>{_esc(node.get("key", ""))}</code></td></tr>')
        parts.append('</tbody></table>')
    else:
        parts.append('<p class="empty">No nodes in this view.</p>')
    parts.append('<h3>Edges</h3>')
    if edges:
        parts.append('<table class="edge-table"><thead><tr><th>Type</th>'
                     '<th>Source</th><th>Target</th><th>ID</th></tr></thead>'
                     '<tbody>')
        for edge in edges:
            parts.append(
                f'<tr><td>{_esc(edge.get("type", "other"))}</td>'
                f'<td>{_esc(edge.get("src", ""))}</td>'
                f'<td>{_esc(edge.get("tgt", ""))}</td>'
                f'<td><code>{_esc(edge.get("id", ""))}</code></td></tr>')
        parts.append('</tbody></table>')
    else:
        parts.append('<p class="empty">No edges in this view.</p>')
    parts.append('</div>')
    return "\n".join(parts)


def _graph_section(view: dict[str, Any]) -> str:
    kind = str((view.get("layout") or {}).get("kind", "force"))
    if not view.get("nodes"):
        return '<section class="graph-view" data-layout="' + _esc(kind) + \
               '"><p class="empty">No nodes in this view.</p></section>'
    if kind == "table":
        body = _table_graph(view)
    elif kind == "matrix":
        body = _matrix_graph(view)
    else:
        body = _svg_graph(view)
    return (f'<section class="graph-view" data-layout="{_esc(kind)}">'
            f'{body}</section>')


def _legend_section(view: dict[str, Any]) -> str:
    groups = view.get("groups", [])
    colors = (view.get("legend") or {}).get("color", {})
    parts = ['<section class="legend"><h2>Legend</h2>']
    if groups:
        parts.append('<ul class="legend-list">')
        for group in groups:
            group_id = str(group.get("id", ""))
            color = _color(colors.get(group_id, _DEFAULT_COLOR))
            collapsed = int(group.get("collapsed", 0) or 0)
            extra = f" · {collapsed} collapsed" if collapsed else ""
            count = int(group.get("count", 0) or 0)
            parts.append(
                f'<li><span class="swatch" style="background:{color}"></span>'
                f'<strong>{_esc(group.get("label", group_id))}</strong>'
                f'<span class="muted"> ({count}{_esc(extra)})</span></li>')
        parts.append('</ul>')
    else:
        parts.append('<p class="muted">No groups.</p>')
    parts.append('</section>')
    return "\n".join(parts)


def _snapshot_section(view: dict[str, Any]) -> str:
    snapshot = view.get("snapshot") or {}
    provenance = view.get("provenance") or {}
    parts = ['<section class="provenance"><h2>Provenance and snapshot</h2>',
             '<dl class="facts">']
    for key in ("root", "head", "freshness", "generated_at"):
        value = snapshot.get(key)
        parts.append(f'<dt>{_esc(key)}</dt><dd>{_esc(value) if value is not None else "not recorded"}</dd>')
    for key in ("graph", "graph_schema", "investigation"):
        value = provenance.get(key)
        if value is not None:
            parts.append(f'<dt>{_esc(key)}</dt><dd>{_esc(value)}</dd>')
    parts.append('</dl>')
    parts.append('<p class="muted">This view does not compute freshness or '
                 'provenance; it only reports what the graph records.</p>')
    parts.append('<h3>Sources</h3>')
    sources = provenance.get("sources") or []
    if sources:
        parts.append('<ul>' + ''.join(
            f'<li><code>{_esc(item)}</code></li>' for item in sources) + '</ul>')
    else:
        parts.append('<p class="muted">No source paths were recorded with this view.</p>')
    parts.append('<pre>' + _esc(json.dumps(provenance, indent=2, sort_keys=True))
                 + '</pre></section>')
    return "\n".join(parts)


def _omitted_section(view: dict[str, Any]) -> str:
    omitted = view.get("omitted") or {}
    nodes = int(omitted.get("nodes", 0) or 0)
    edges = int(omitted.get("edges", 0) or 0)
    reason = str(omitted.get("reason", "none") or "none")
    parts = ['<section class="omitted"><h2>Omitted</h2>']
    if nodes or edges:
        parts.append(
            f'<p class="warning">{nodes} node(s) and {edges} edge(s) were '
            f'omitted: {_esc(reason)}.</p>')
        breakdown = omitted.get("by") or {}
        if breakdown:
            parts.append('<ul class="facts-list">')
            for key, value in sorted(breakdown.items()):
                parts.append(f'<li><code>{_esc(key)}</code>: {_esc(value)}</li>')
            parts.append('</ul>')
    else:
        parts.append('<p class="muted">0 node(s) and 0 edge(s) omitted; '
                     'nothing was omitted.</p>')
    parts.append('</section>')
    return "\n".join(parts)


def _header(view: dict[str, Any], title: str) -> str:
    layout = view.get("layout") or {}
    return (
        '<header><h1>' + _esc(title) + '</h1>'
        '<p class="meta">aperture <strong>' + _esc(view.get("aperture", "")) +
        '</strong> · layout <strong>' + _esc(layout.get("kind", "force")) +
        '</strong> · seed ' + _esc(layout.get("seed", 7)) + ' · ' +
        str(len(view.get("nodes", []))) + ' nodes · ' +
        str(len(view.get("edges", []))) + ' edges · schema ' +
        _esc(view.get("schema", 1)) + '</p>'
        '<p class="note">A view is a projection of the graph, not a truth '
        'claim. It does not add support, consensus, freshness or provenance.'
        '</p></header>')


_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: system-ui, -apple-system,
       "Segoe UI", sans-serif; color: #1d2733; background: #fbfcfd; }
header { max-width: 1280px; margin: 0 auto 18px; }
h1 { margin: 0 0 6px; font-size: 1.7rem; }
h2 { margin: 0 0 8px; font-size: 1.05rem; }
h3 { margin: 12px 0 6px; font-size: .95rem; }
.meta { margin: 0 0 8px; color: #52616f; font-size: .9rem; }
.note { margin: 0; padding: 8px 10px; border-left: 3px solid #b07d2b;
        background: #fff8e8; font-size: .88rem; }
main { display: grid; grid-template-columns: minmax(0, 1fr) 300px;
       gap: 18px; max-width: 1280px; margin: 0 auto 18px; align-items: start; }
.graph-view { background: #fff; border: 1px solid #dde4ea; border-radius: 8px;
              overflow: auto; padding: 8px; min-height: 240px; }
.graph-view svg { display: block; width: 100%; height: auto; }
.node { cursor: pointer; }
.node circle { transition: stroke-width .1s ease; }
.node:hover circle, .node.selected circle { stroke: #111; stroke-width: 3; }
.node-label { font-size: 11px; fill: #263645; pointer-events: none; }
.axis-label { font-size: 10px; fill: #52616f; cursor: pointer; }
.axis-label:hover, .axis-label.selected { fill: #111; font-weight: 700; }
#inspector { background: #fff; border: 1px solid #dde4ea; border-radius: 8px;
             padding: 12px; min-height: 180px; position: sticky; top: 12px; }
#inspector h2 { margin-top: 0; }
#inspector dl { display: grid; grid-template-columns: 74px 1fr; gap: 3px 8px;
                font-size: .88rem; }
#inspector dt { color: #52616f; }
#inspector dd { margin: 0; overflow-wrap: anywhere; }
#inspector pre { max-height: 320px; overflow: auto; font-size: .78rem;
                 background: #f5f7f9; padding: 8px; border-radius: 5px; }
section.legend, section.provenance, section.omitted {
  max-width: 1280px; margin: 0 auto 18px; background: #fff;
  border: 1px solid #dde4ea; border-radius: 8px; padding: 12px 14px; }
.legend-list { list-style: none; padding: 0; margin: 0; display: flex;
               flex-wrap: wrap; gap: 8px 20px; font-size: .9rem; }
.legend-list li { display: flex; align-items: center; gap: 6px; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px;
          margin-right: 6px; vertical-align: -1px; }
.muted { color: #52616f; }
.warning { color: #8a1f11; background: #fff1ef; border-left: 3px solid #c0504d;
           padding: 8px 10px; margin: 0 0 8px; }
.facts { display: grid; grid-template-columns: 110px 1fr; gap: 3px 10px;
         font-size: .88rem; }
.facts dt { color: #52616f; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.facts-list { margin: 6px 0 0; font-size: .86rem; }
pre { background: #f5f7f9; padding: 10px; border-radius: 6px; overflow: auto;
      font-size: .8rem; }
.tables { overflow: auto; }
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #e7ecf1; }
th { color: #52616f; font-weight: 600; }
tbody tr[data-node] { cursor: pointer; }
tbody tr[data-node]:hover, tbody tr.selected { background: #eef4f8; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.empty { color: #52616f; padding: 18px; }
@media (max-width: 900px) {
  main { grid-template-columns: 1fr; }
  #inspector { position: static; }
}
@media print {
  body { background: #fff; padding: 0; }
  #inspector { display: none; }
}
"""

_SCRIPT = """
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("view-data").textContent);
  var byId = new Map();
  data.nodes.forEach(function (node) { byId.set(String(node.id), node); });
  var body = document.getElementById("inspector-body");

  function row(label, value) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    var fragment = document.createDocumentFragment();
    fragment.appendChild(dt);
    fragment.appendChild(dd);
    return fragment;
  }

  function show(id) {
    var node = byId.get(String(id));
    if (!node) { return; }
    document.querySelectorAll("[data-node].selected").forEach(
      function (element) { element.classList.remove("selected"); });
    document.querySelectorAll("[data-node]").forEach(function (element) {
      if (element.getAttribute("data-node") === String(id)) {
        element.classList.add("selected");
      }
    });
    body.replaceChildren();
    var dl = document.createElement("dl");
    dl.appendChild(row("ID", String(node.id)));
    dl.appendChild(row("Type", String(node.type)));
    if (node.raw_type && node.raw_type !== node.type) {
      dl.appendChild(row("Raw type", String(node.raw_type)));
    }
    dl.appendChild(row("Label", String(node.label)));
    dl.appendChild(row("Group", String(node.group)));
    dl.appendChild(row("Key", String(node.key)));
    dl.appendChild(row("Weight", String(node.weight)));
    body.appendChild(dl);
    var heading = document.createElement("h3");
    heading.textContent = "Attributes";
    body.appendChild(heading);
    var pre = document.createElement("pre");
    pre.textContent = JSON.stringify(node.attrs || {}, null, 2);
    body.appendChild(pre);
  }

  function activate(element) {
    var id = element.getAttribute("data-node");
    if (id !== null) { show(id); }
  }

  document.querySelectorAll("[data-node]").forEach(function (element) {
    element.addEventListener("click", function () { activate(element); });
    element.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate(element);
      }
    });
  });
})();
"""


def render_html(view: dict[str, Any], *, title: str | None = None) -> str:
    """Render one view model as a self-contained HTML document."""
    page_title = title or f"{view.get('aperture', 'graph')} view"
    payload = _json_for_script(view)
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(page_title)} — ozagia view</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        _header(view, page_title),
        "<main>",
        _graph_section(view),
        '<aside id="inspector"><h2>Node inspector</h2>'
        '<div id="inspector-body"><p class="muted">Select a node to inspect '
        "its id, type, group and recorded attributes.</p></div></aside>",
        "</main>",
        _legend_section(view),
        _snapshot_section(view),
        _omitted_section(view),
        '<script type="application/json" id="view-data">' + payload
        + "</script>",
        f"<script>{_SCRIPT}</script>",
        "</body>",
        "</html>",
        "",
    ])


__all__ = ["render_html"]
