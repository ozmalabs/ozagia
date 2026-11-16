/** Styles contributed through the GUI `Style` registry. */
export const panelCss = `
.ozagia-panel,
.ozagia-settings {
  --ozagia-line: color-mix(in srgb, currentColor 16%, transparent);
  --ozagia-line-strong: color-mix(in srgb, currentColor 30%, transparent);
  --ozagia-muted: color-mix(in srgb, currentColor 62%, transparent);
  --ozagia-soft: color-mix(in srgb, currentColor 6%, transparent);
  --ozagia-ok: #2f8a6b;
  --ozagia-warn: #b07818;
  --ozagia-error: #c24b4b;
}
.ozagia-panel {
  padding: 10px 12px 28px;
  font-size: 12px;
  line-height: 1.45;
  color: inherit;
  overflow-wrap: anywhere;
}
.ozagia-settings {
  font-size: 12px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.ozagia-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.ozagia-title { font-size: 13px; font-weight: 650; }
.ozagia-sub { color: var(--ozagia-muted); font-size: 11px; }
.ozagia-stack { display: flex; flex-direction: column; gap: 10px; }
.ozagia-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; }
.ozagia-row > * { min-width: 0; }
.ozagia-field { display: flex; flex-direction: column; gap: 2px; flex: 1 1 110px; }
.ozagia-field > span {
  color: var(--ozagia-muted);
  font-size: 10px;
  letter-spacing: .05em;
  text-transform: uppercase;
}
.ozagia-input, .ozagia-select, .ozagia-textarea {
  background: transparent;
  color: inherit;
  border: 1px solid var(--ozagia-line-strong);
  border-radius: 5px;
  padding: 3px 6px;
  font: inherit;
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
}
.ozagia-textarea { min-height: 52px; resize: vertical; font-family: inherit; }
.ozagia-input:focus, .ozagia-select:focus, .ozagia-textarea:focus {
  outline: 1px solid var(--ozagia-line-strong);
  outline-offset: 0;
}
.ozagia-button {
  background: var(--ozagia-soft);
  color: inherit;
  border: 1px solid var(--ozagia-line-strong);
  border-radius: 5px;
  padding: 3px 9px;
  font: inherit;
  cursor: pointer;
  white-space: nowrap;
}
.ozagia-button:hover { background: color-mix(in srgb, currentColor 12%, transparent); }
.ozagia-button:disabled { opacity: .5; cursor: default; }
.ozagia-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 1px solid var(--ozagia-line-strong);
  border-radius: 999px;
  padding: 0 6px;
  font-size: 10px;
  line-height: 1.6;
  white-space: nowrap;
}
.ozagia-badge[data-tone="ok"] { color: var(--ozagia-ok); border-color: color-mix(in srgb, var(--ozagia-ok) 55%, transparent); }
.ozagia-badge[data-tone="warn"] { color: var(--ozagia-warn); border-color: color-mix(in srgb, var(--ozagia-warn) 55%, transparent); }
.ozagia-badge[data-tone="error"] { color: var(--ozagia-error); border-color: color-mix(in srgb, var(--ozagia-error) 55%, transparent); }
.ozagia-badge[data-tone="muted"] { color: var(--ozagia-muted); }
.ozagia-card {
  border: 1px solid var(--ozagia-line);
  border-radius: 7px;
  padding: 8px 9px;
  background: color-mix(in srgb, currentColor 2.5%, transparent);
}
.ozagia-card > h3 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--ozagia-muted);
}
.ozagia-metrics { display: flex; flex-wrap: wrap; gap: 10px 16px; margin-bottom: 4px; }
.ozagia-metric { display: flex; flex-direction: column; gap: 0; }
.ozagia-metric > b { font-size: 13px; font-weight: 650; }
.ozagia-metric > span { color: var(--ozagia-muted); font-size: 10px; }
.ozagia-scroll { overflow: auto; max-height: 300px; border: 1px solid var(--ozagia-line); border-radius: 5px; }
.ozagia-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.ozagia-table th, .ozagia-table td {
  padding: 3px 5px;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--ozagia-line);
}
.ozagia-table th {
  position: sticky;
  top: 0;
  background: Canvas;
  color: var(--ozagia-muted);
  font-weight: 600;
  white-space: nowrap;
  z-index: 1;
}
.ozagia-table tr:last-child td { border-bottom: 0; }
.ozagia-truncate { display: block; max-width: 170px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ozagia-pre {
  margin: 0;
  padding: 6px;
  border: 1px solid var(--ozagia-line);
  border-radius: 5px;
  background: var(--ozagia-soft);
  font-size: 10px;
  line-height: 1.35;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}
.ozagia-list { margin: 0; padding-left: 16px; }
.ozagia-list li { margin: 2px 0; }
.ozagia-empty, .ozagia-loading {
  border: 1px dashed var(--ozagia-line-strong);
  border-radius: 7px;
  padding: 12px;
  color: var(--ozagia-muted);
  text-align: center;
}
.ozagia-failure {
  border: 1px solid color-mix(in srgb, var(--ozagia-error) 45%, transparent);
  border-radius: 7px;
  padding: 9px;
  color: var(--ozagia-error);
  background: color-mix(in srgb, var(--ozagia-error) 7%, transparent);
}
.ozagia-failure b { display: block; margin-bottom: 2px; }
.ozagia-failure .ozagia-sub { color: inherit; opacity: .8; }
.ozagia-failure code { font-size: 10px; white-space: pre-wrap; }
.ozagia-meter {
  height: 5px;
  border-radius: 999px;
  background: var(--ozagia-soft);
  overflow: hidden;
  border: 1px solid var(--ozagia-line);
}
.ozagia-meter > i { display: block; height: 100%; background: currentColor; opacity: .55; }
.ozagia-meter[data-tone="warn"] > i { background: var(--ozagia-warn); opacity: .9; }
.ozagia-meter[data-tone="error"] > i { background: var(--ozagia-error); opacity: .9; }
.ozagia-chips { display: flex; flex-wrap: wrap; gap: 4px 8px; }
.ozagia-chip { display: inline-flex; align-items: center; gap: 4px; color: var(--ozagia-muted); font-size: 10.5px; }
.ozagia-dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; }
.ozagia-svg {
  width: 100%;
  height: auto;
  display: block;
  border: 1px solid var(--ozagia-line);
  border-radius: 7px;
  background: color-mix(in srgb, currentColor 2.5%, transparent);
  touch-action: manipulation;
}
.ozagia-svg-edge { stroke: currentColor; stroke-opacity: .22; stroke-width: .7; }
.ozagia-svg-node { stroke: Canvas; stroke-width: .7; cursor: pointer; }
.ozagia-svg-node:focus { outline: none; stroke: currentColor; stroke-width: 1.6; }
.ozagia-svg-node[data-selected="true"] { stroke: currentColor; stroke-width: 2; }
.ozagia-details { border-top: 1px solid var(--ozagia-line); padding-top: 6px; margin-top: 6px; }
.ozagia-details > summary { cursor: pointer; color: var(--ozagia-muted); font-size: 11px; }
.ozagia-details[open] > summary { margin-bottom: 5px; }
.ozagia-grid { display: grid; grid-template-columns: minmax(80px, auto) 1fr; gap: 2px 10px; font-size: 11px; }
.ozagia-grid > dt { color: var(--ozagia-muted); }
.ozagia-grid > dd { margin: 0; }
.ozagia-note { color: var(--ozagia-muted); font-size: 10.5px; }
.ozagia-sep { height: 1px; background: var(--ozagia-line); margin: 8px 0; }
.ozagia-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--ozagia-muted);
  font-size: 11px;
}
.ozagia-check input { margin: 0; }
.ozagia-setting-value {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  max-width: 360px;
}
.ozagia-setting-value code {
  font-size: 11px;
  text-align: right;
  overflow-wrap: anywhere;
}
.ozagia-token {
  display: block;
  padding: 6px;
  border: 1px solid var(--ozagia-line-strong);
  border-radius: 5px;
  background: var(--ozagia-soft);
  font-size: 11px;
  word-break: break-all;
  user-select: all;
}
`
