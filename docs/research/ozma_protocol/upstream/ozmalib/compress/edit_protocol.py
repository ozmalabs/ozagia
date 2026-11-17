"""
edit_protocol.py — Position-addressed edit protocol for LLM code generation.

Defines the format the model produces when making code changes. Edits
reference positions from the code outline, not line numbers or raw text.

The model sees:
    [3] method Config.load (L10-13)
        def load(self) -> dict:
            with open(self.path) as f:
                return json.load(f)

The model outputs:
    edit [3]
    ```
    def load(self) -> dict:
        with open(self.path) as f:
            text = f.read()
            return json.loads(text) if text.strip() else {}
    ```

Or for multi-file:
    edit config.py:[3]
    ```
    def load(self) -> dict:
        ...
    ```

    add config.py:[3]:after
    ```
    def load_or_default(self, default=None) -> dict:
        try:
            return self.load()
        except FileNotFoundError:
            return default or {}
    ```

Operations:
    edit [N]       — replace the body of position N
    delete [N]     — remove position N entirely
    add [N]:before — insert new code before position N
    add [N]:after  — insert new code after position N
    add [file]     — append to end of file (new function/class)

The protocol is deliberately simple — one operation per block, full
replacement (not line-level diffs). This makes training data clean
and validation straightforward: parse the edit, apply to outline,
run checks.

Usage::

    from ozmalib.compress.edit_protocol import parse_edits, apply_edits, render_edit

    edits = parse_edits(model_output)
    result = apply_edits(source, code_index, edits)
    if result.errors:
        print(result.errors)
    else:
        print(result.patched_source)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Edit — a single position-addressed edit operation
# ---------------------------------------------------------------------------

@dataclass
class Edit:
    """A single edit operation referencing a code position."""
    operation: str          # "edit", "delete", "add"
    position: int           # target position number
    file: str = ""          # file path (for multi-file)
    placement: str = ""     # "before", "after" (for add operations)
    content: str = ""       # new code content (for edit/add)
    raw: str = ""           # original text from model output

    def render(self) -> str:
        """Render this edit in the protocol format."""
        pos = f"{self.file}:[{self.position}]" if self.file else f"[{self.position}]"

        if self.operation == "delete":
            return f"delete {pos}"

        if self.operation == "add":
            placement = f":{self.placement}" if self.placement else ""
            return f"add {pos}{placement}\n```\n{self.content}\n```"

        # edit
        return f"edit {pos}\n```\n{self.content}\n```"


@dataclass
class EditSet:
    """A collection of edits from a single model response."""
    edits: list[Edit] = field(default_factory=list)
    preamble: str = ""      # any text before the first edit (explanation)
    postamble: str = ""     # any text after the last edit

    @property
    def files(self) -> list[str]:
        """Unique files referenced."""
        return list(dict.fromkeys(e.file for e in self.edits if e.file))

    @property
    def positions(self) -> list[int]:
        """All positions referenced."""
        return [e.position for e in self.edits]


# ---------------------------------------------------------------------------
# Parser — extract edits from model output
# ---------------------------------------------------------------------------

# Matches: edit [3], edit config.py:[3], delete [3], add [3]:after
_EDIT_HEADER = re.compile(
    r'^(edit|delete|add)\s+'
    r'(?:(\S+?):)?'             # optional file: prefix
    r'\[(\d+)\]'                # [position]
    r'(?::(\w+))?'              # optional :before/:after
    r'\s*$',
    re.MULTILINE,
)

# Matches: add config.py (no position — append to file)
_ADD_FILE = re.compile(
    r'^add\s+(\S+)\s*$',
    re.MULTILINE,
)

# Code block: ```...``` or ``` lang\n...```
_CODE_BLOCK = re.compile(
    r'```(?:\w+)?\n(.*?)```',
    re.DOTALL,
)


def parse_edits(text: str) -> EditSet:
    """Parse model output into an EditSet.

    Extracts position-addressed edit operations from the model's response.
    Tolerates explanation text around the edits.

    Args:
        text: Raw model output text.

    Returns:
        EditSet with parsed edits.
    """
    edits = []
    preamble_end = len(text)
    postamble_start = 0

    # Find all edit headers and their associated code blocks
    for m in _EDIT_HEADER.finditer(text):
        operation = m.group(1)
        file = m.group(2) or ""
        position = int(m.group(3))
        placement = m.group(4) or ""

        # Track preamble/postamble boundaries
        if not edits:
            preamble_end = m.start()

        content = ""
        raw = m.group(0)

        if operation != "delete":
            # Find the next code block after this header
            code_match = _CODE_BLOCK.search(text, m.end())
            if code_match:
                content = code_match.group(1).rstrip("\n")
                raw = text[m.start():code_match.end()]
                postamble_start = code_match.end()
            else:
                # Try indented block (no fences)
                block = _extract_indented_block(text, m.end())
                if block:
                    content = block
                    postamble_start = m.end() + len(block)
        else:
            postamble_start = m.end()

        edits.append(Edit(
            operation=operation,
            position=position,
            file=file,
            placement=placement,
            content=content,
            raw=raw,
        ))

    # Collect spans already matched by _EDIT_HEADER to avoid double-matching
    matched_spans = {(e_m.start(), e_m.end()) for e_m in _EDIT_HEADER.finditer(text)}

    # Handle "add file.py" (no position — append)
    for m in _ADD_FILE.finditer(text):
        file = m.group(1)
        # Skip if this overlaps with a positioned edit header
        if any(m.start() >= s and m.start() < e for s, e in matched_spans):
            continue
        # Skip if already matched as a positioned add
        if any(e.file == file and e.operation == "add" for e in edits):
            continue

        code_match = _CODE_BLOCK.search(text, m.end())
        if code_match:
            edits.append(Edit(
                operation="add",
                position=0,  # 0 = append to file
                file=file,
                placement="end",
                content=code_match.group(1).rstrip("\n"),
                raw=text[m.start():code_match.end()],
            ))

    preamble = text[:preamble_end].strip() if preamble_end > 0 else ""
    postamble = text[postamble_start:].strip() if postamble_start < len(text) else ""

    return EditSet(edits=edits, preamble=preamble, postamble=postamble)


def _extract_indented_block(text: str, start: int) -> str:
    """Extract an indented code block starting after `start`."""
    lines = text[start:].split("\n")
    block_lines = []
    started = False

    for line in lines:
        if not started:
            if line.strip():
                # First non-empty line — check if indented
                if line.startswith("    ") or line.startswith("\t"):
                    started = True
                    block_lines.append(line)
                else:
                    break
        else:
            if line.strip() == "" or line.startswith("    ") or line.startswith("\t"):
                block_lines.append(line)
            else:
                break

    return "\n".join(block_lines).rstrip() if block_lines else ""


# ---------------------------------------------------------------------------
# Apply — execute edits against source code
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    """Result of applying edits to source code."""
    patched_source: str
    applied: list[Edit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def apply_edits(
    source: str,
    code_index,
    edits: EditSet | list[Edit],
) -> ApplyResult:
    """Apply position-addressed edits to source code.

    Resolves positions via the code index, replaces/inserts/deletes
    the corresponding source regions, and returns the patched source.

    Args:
        source: Original source code.
        code_index: CodeIndex from code_to_outline().
        edits: EditSet or list of Edit objects.

    Returns:
        ApplyResult with patched source and any errors.
    """
    if isinstance(edits, EditSet):
        edit_list = edits.edits
    else:
        edit_list = edits

    result = ApplyResult(patched_source=source)
    lines = source.splitlines(keepends=True)

    # Collect all edits with their line ranges, validate, and sort
    operations: list[tuple[int, int, str, Edit]] = []  # (start_line, end_line, new_content, edit)

    for edit in edit_list:
        node = code_index.get(edit.position)
        if not node:
            if edit.position == 0 and edit.operation == "add":
                # Position 0 = append to end of file
                operations.append((len(lines), len(lines), edit.content + "\n", edit))
                continue
            result.errors.append(
                f"position [{edit.position}] not found in code index"
            )
            continue

        if not node.line_start or not node.line_end:
            result.errors.append(
                f"position [{edit.position}] ({node.name}) has no line range"
            )
            continue

        start = node.line_start - 1  # 0-based
        end = node.line_end          # exclusive

        if edit.operation == "edit":
            if not edit.content:
                result.errors.append(
                    f"edit [{edit.position}] ({node.name}): empty content"
                )
                continue
            # Detect indentation of original code
            indent = _detect_indent(lines, start)
            new_content = _reindent(edit.content, indent)
            operations.append((start, end, new_content + "\n", edit))

        elif edit.operation == "delete":
            operations.append((start, end, "", edit))

        elif edit.operation == "add":
            indent = _detect_indent(lines, start)
            new_content = _reindent(edit.content, indent)
            if edit.placement == "before":
                operations.append((start, start, new_content + "\n", edit))
            else:  # after
                operations.append((end, end, new_content + "\n", edit))

    if result.errors:
        return result

    # Sort operations by line number (reverse order so indices stay valid)
    operations.sort(key=lambda op: op[0], reverse=True)

    # Check for overlapping operations
    for i in range(len(operations) - 1):
        _, end_a, _, edit_a = operations[i]
        start_b, _, _, edit_b = operations[i + 1]
        if end_a > start_b:
            result.errors.append(
                f"overlapping edits: [{edit_a.position}] and [{edit_b.position}]"
            )
            return result

    # Apply in reverse order
    for start, end, new_content, edit in operations:
        if new_content:
            new_lines = new_content.splitlines(keepends=True)
            # Ensure last line has newline
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            lines[start:end] = new_lines
        else:
            # Delete
            lines[start:end] = []
        result.applied.append(edit)

    result.patched_source = "".join(lines)
    return result


def _detect_indent(lines: list[str], line_idx: int) -> str:
    """Detect the indentation of a line."""
    if line_idx < len(lines):
        line = lines[line_idx]
        return line[:len(line) - len(line.lstrip())]
    return ""


def _reindent(content: str, target_indent: str) -> str:
    """Re-indent content to match target indentation.

    Detects the content's existing indentation and adjusts.
    """
    content_lines = content.splitlines()
    if not content_lines:
        return content

    # Find minimum indentation in content (ignoring blank lines)
    min_indent = None
    for line in content_lines:
        if line.strip():
            line_indent = line[:len(line) - len(line.lstrip())]
            if min_indent is None or len(line_indent) < len(min_indent):
                min_indent = line_indent

    if min_indent is None:
        min_indent = ""

    # Re-indent
    result = []
    for line in content_lines:
        if line.strip():
            # Strip original indent, add target indent
            stripped = line[len(min_indent):] if line.startswith(min_indent) else line.lstrip()
            result.append(target_indent + stripped)
        else:
            result.append("")

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Render — format edits for display/training data
# ---------------------------------------------------------------------------

def render_edit_protocol(edits: EditSet) -> str:
    """Render an EditSet in the canonical protocol format.

    This is the format used in training data.
    """
    parts = []
    if edits.preamble:
        parts.append(edits.preamble)
        parts.append("")

    for edit in edits.edits:
        parts.append(edit.render())
        parts.append("")

    return "\n".join(parts).strip()


def render_edit_diff(source: str, result: ApplyResult) -> str:
    """Render a human-readable diff of the applied edits.

    Shows what changed at each position — useful for review.
    """
    if not result.success:
        return "ERRORS:\n" + "\n".join(result.errors)

    from .snapshot_diff import diff, CODE_FINGERPRINTER
    from .code_outline import code_to_outline

    # Build outlines for before/after
    _, old_index = code_to_outline(source)
    _, new_index = code_to_outline(result.patched_source)

    delta = diff(old_index.all_positions(), new_index.all_positions(), CODE_FINGERPRINTER)

    lines = []
    for change in delta.changes:
        lines.append(f"[{change.position}] {change.kind}: {change.description}")
    for pos in delta.added:
        node = new_index.get(pos)
        if node:
            lines.append(f"[{pos}] added: {node.kind} {node.name}")
    for pos in delta.removed:
        node = old_index.get(pos)
        if node:
            lines.append(f"[{pos}] removed: {node.kind} {node.name}")

    return "\n".join(lines) if lines else "no structural changes"
