"""YAML front matter on a beads issue description.

The board stores an issue's description as free text, which is right for prose
and wrong for the one or two facts a tool has to read back exactly. The first
of those is ``workspace``: a session created from an issue has to be created
*somewhere*, and nothing in the issue said where, so the operator picked the
directory again by hand every time and a wrong pick is not reported by
anything downstream.

So the description may open with a front matter block, spelled the way Obsidian
spells it — a ``---`` fence, ``key: value`` lines, a closing ``---`` — and the
prose follows underneath, untouched:

.. code-block:: text

    ---
    workspace: claude-launcher
    ---
    ## 목표
    ...

What this module is: the one place that block is read and written. What it is
not: a schema. Keys this module does not know are parsed, preserved across a
write and handed back to the caller, because an issue edited by a human or a
later version of this tool must not lose fields by passing through here.

Three properties the callers depend on:

* A description with no front matter parses to an empty mapping and its own
  text as the body — every issue written before this existed reads correctly,
  and nothing has to be migrated.
* A front matter block that is not a mapping (a list, a scalar, malformed YAML)
  is *not* front matter. It parses as body text and is left exactly as typed.
  The alternative is a description silently truncated to the part after a
  ``---`` line that was never meant as a fence.
* :func:`render` of what :func:`parse` returned is byte-identical to the input
  for every description this module wrote, so a read-modify-write cycle that
  changes nothing writes nothing new.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

import yaml

#: The fence, both ends of it. Obsidian's spelling, and YAML's own document
#: marker, which is why a block written here is readable by anything that
#: already understands front matter.
FENCE = "---"

#: The key the dashboard reads: the registered workspace name a session made
#: from this issue should be created in. A *name*, not a path — the same
#: vocabulary ``claunch workspace`` and the spawn policy use, so an issue
#: cannot name a directory nobody registered.
WORKSPACE = "workspace"


def parse(description: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """Split ``description`` into (front matter mapping, body text).

    Returns an empty mapping and the description unchanged when there is no
    front matter, when the fence is unterminated, when the block is not valid
    YAML, or when it parses to anything other than a mapping. In every one of
    those cases the text is somebody's prose and is returned as prose.
    """
    text = description or ""
    if not text.startswith(FENCE):
        return {}, text
    # The opening fence is a line of its own: "---" followed by a newline, and
    # nothing else on it. "--- x" and "----" are not fences.
    first_break = text.find("\n")
    if first_break == -1 or text[:first_break].strip() != FENCE:
        return {}, text
    rest = text[first_break + 1:]
    block, sep, body = _split_at_fence(rest)
    if not sep:
        return {}, text          # unterminated: not a block, just prose
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, text
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return {}, text
    return dict(loaded), body


def _split_at_fence(text: str) -> Tuple[str, bool, str]:
    """Cut ``text`` at the first line that is exactly the closing fence.

    Returns (block before it, whether the fence was found, body after it).
    The body keeps everything after that line's newline, so a block followed
    by no body yields an empty body rather than a newline.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == FENCE:
            block = "\n".join(lines[:index])
            body = "\n".join(lines[index + 1:])
            return block, True, body
    return text, False, ""


def render(meta: Optional[Mapping[str, Any]], body: str) -> str:
    """The inverse of :func:`parse`: a description carrying ``meta`` above
    ``body``.

    An empty (or all-empty-valued) mapping writes no fence at all — an issue
    with nothing to record keeps a description that looks like every issue
    written before front matter existed.
    """
    kept = {k: v for k, v in (meta or {}).items() if v not in (None, "")}
    if not kept:
        return body
    block = yaml.safe_dump(
        kept,
        allow_unicode=True,       # a workspace name may be non-ASCII
        default_flow_style=False,
        sort_keys=True,           # stable output: no spurious diff on rewrite
    )
    return f"{FENCE}\n{block}{FENCE}\n{body}"


def get(description: Optional[str], key: str, default: Any = None) -> Any:
    """One front matter value, or ``default`` when the issue records none."""
    meta, _ = parse(description)
    value = meta.get(key, default)
    return default if value in (None, "") else value


def set_key(description: Optional[str], key: str, value: Any) -> str:
    """``description`` with ``key`` recorded, keeping every other key and the
    body exactly as they were.

    A ``value`` of ``None`` or ``""`` removes the key, which is how the
    dashboard clears a workspace the operator unset — writing the empty string
    into the block instead would leave an issue claiming a workspace named
    nothing.
    """
    meta, body = parse(description)
    if value in (None, ""):
        meta.pop(key, None)
    else:
        meta[key] = value
    return render(meta, body)


def workspace_of(issue: Optional[Mapping[str, Any]]) -> str:
    """The workspace an issue names, or ``""``.

    Takes the issue row rather than its description so callers hold one shape;
    a row without a description is not an error, it is an issue that records
    no workspace.
    """
    if not issue:
        return ""
    return str(get(issue.get("description"), WORKSPACE, "") or "")
