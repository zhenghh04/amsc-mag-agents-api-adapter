"""
Parser + applier for OpenAI's `apply_patch` envelope — the format the Codex
coding agent emits to add / update / delete files.

Envelope shape:

    *** Begin Patch
    *** Add File: path/new.py
    +line one
    +line two
    *** Update File: path/existing.py
    @@ optional section context
     unchanged context line   (leading single space)
    -removed line
    +added line
    *** Delete File: path/old.py
    *** End Patch

This module is pure (no IO): `parse_patch` turns the envelope into structured
operations, `apply_hunks` transforms file text for an Update. The shim performs
the actual reads/writes through the sandbox (`codex_sandbox`), so patch logic
stays unit-testable without a running executor.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    context_before: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass
class Op:
    kind: str                      # "add" | "update" | "delete"
    path: str
    content: str | None = None     # for add
    hunks: list[Hunk] = field(default_factory=list)  # for update


_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_UPDATE = "*** Update File: "
_DELETE = "*** Delete File: "


def parse_patch(text: str) -> list[Op]:
    lines = text.splitlines()
    # Tolerate leading/trailing blank lines around the envelope.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != _BEGIN:
        raise PatchError("patch must start with '*** Begin Patch'")
    if lines[-1].strip() != _END:
        raise PatchError("patch must end with '*** End Patch'")

    ops: list[Op] = []
    i = 1
    n = len(lines) - 1  # exclusive of End
    while i < n:
        line = lines[i]
        if line.startswith(_ADD):
            path = line[len(_ADD):].strip()
            i += 1
            body: list[str] = []
            while i < n and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise PatchError(f"Add File body lines must start with '+': {lines[i]!r}")
                body.append(lines[i][1:])
                i += 1
            ops.append(Op("add", path, content="\n".join(body) + ("\n" if body else "")))
        elif line.startswith(_DELETE):
            ops.append(Op("delete", line[len(_DELETE):].strip()))
            i += 1
        elif line.startswith(_UPDATE):
            path = line[len(_UPDATE):].strip()
            i += 1
            hunks: list[Hunk] = []
            cur: Hunk | None = None
            while i < n and not lines[i].startswith("*** "):
                hl = lines[i]
                if hl.startswith("@@"):
                    cur = Hunk()
                    hunks.append(cur)
                    i += 1
                    continue
                if cur is None:
                    cur = Hunk()
                    hunks.append(cur)
                if hl.startswith("+"):
                    cur.added.append(hl[1:])
                elif hl.startswith("-"):
                    cur.removed.append(hl[1:])
                elif hl.startswith(" "):
                    (cur.context_after if (cur.removed or cur.added) else cur.context_before).append(hl[1:])
                elif hl == "":
                    (cur.context_after if (cur.removed or cur.added) else cur.context_before).append("")
                else:
                    raise PatchError(f"unexpected hunk line: {hl!r}")
                i += 1
            ops.append(Op("update", path, hunks=hunks))
        elif not line.strip():
            i += 1
        else:
            raise PatchError(f"unexpected line in patch: {line!r}")
    return ops


def apply_hunks(original: str, hunks: list[Hunk]) -> str:
    """Apply Update hunks to file text by locating each old block and replacing it."""
    text = original
    for h in hunks:
        old_block = "\n".join(h.context_before + h.removed + h.context_after)
        new_block = "\n".join(h.context_before + h.added + h.context_after)
        if old_block == "":
            # Pure insertion with no context — append.
            text = text + ("" if text.endswith("\n") or not text else "\n") + new_block
            continue
        if old_block not in text:
            raise PatchError(f"context not found for hunk:\n{old_block}")
        text = text.replace(old_block, new_block, 1)
    return text
