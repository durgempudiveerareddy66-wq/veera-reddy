"""Building the vault graph from the index roots.

Nodes are files, edges are references between them — `[[wikilinks]]`, markdown
links, and bare filename mentions. This is read-only and never leaves the index
roots.

Two things §9 warns about are handled here:

*   **Scanned PDFs produce garbage.** Extraction that comes back near-empty marks
    the node ``unindexed`` rather than feeding noise into the graph. A node with
    no readable text is honest; a node full of ligature soup is worse than absent.
*   **Credential files are never nodes.** The PathGuard's BLACK check runs on every
    candidate before it becomes a node, so a private key cannot appear in the
    graph even as a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import PathGuard

TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
MAX_BYTES = 2_000_000
SKIP_DIRS = {"node_modules", ".git", ".obsidian", ".venv", "__pycache__", ".jarvis_trash"}

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_MDLINK = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")
_FRONT_TYPE = re.compile(r"^type:\s*(\S+)", re.M)
_NONWORD = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    """Normalise a link target or filename to one comparable key.

    The vault writes `[[Halberd Legal]]` and stores it as `halberd-legal.md`.
    Comparing raw lowercase makes those two different strings, and every edge in
    the graph silently disappears — 66 nodes, 0 links, which looks like a vault
    with no structure rather than a resolver with a bug.
    """
    return _NONWORD.sub("-", s.strip().lower()).strip("-")


@dataclass
class Node:
    id: str
    name: str
    path: str
    type: str = "note"
    links: set[str] = field(default_factory=set)
    unindexed: bool = False

    def to_dict(self, degree: int) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "path": self.path,
                "type": self.type, "degree": degree, "unindexed": self.unindexed}


def _type_of(p: Path, text: str) -> str:
    m = _FRONT_TYPE.search(text[:400])
    if m:
        return m.group(1).strip()
    parent = p.parent.name.lower()
    for t in ("invoice", "client", "project", "meeting", "people", "person",
              "idea", "admin"):
        if parent.startswith(t):
            return t.rstrip("s")
    return "note"


def build(guard: PathGuard) -> dict[str, Any]:
    """Walk the index roots and return {nodes, edges, stats}."""
    nodes: dict[str, Node] = {}
    by_stem: dict[str, str] = {}

    for root in guard.index_roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if guard.is_black(p)[0]:
                continue                    # never even name a credential file
            if p.stat().st_size > MAX_BYTES:
                continue

            nid = str(p)
            if p.suffix.lower() not in TEXT_SUFFIXES:
                # A non-text file is a real node with no readable content. It is
                # marked unindexed rather than silently dropped or fake-parsed.
                nodes[nid] = Node(nid, p.stem, str(p), type=p.suffix.lstrip(".") or "file",
                                  unindexed=True)
                by_stem.setdefault(_slug(p.stem), nid)
                continue

            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text.strip()) < 20:
                # §9.9: near-empty extraction is noise, not content.
                nodes[nid] = Node(nid, p.stem, str(p), unindexed=True)
                by_stem.setdefault(_slug(p.stem), nid)
                continue

            n = Node(nid, p.stem, str(p), type=_type_of(p, text))
            for m in _WIKILINK.finditer(text):
                n.links.add(_slug(m.group(1)))
            for m in _MDLINK.finditer(text):
                n.links.add(_slug(Path(m.group(1)).stem))
            nodes[nid] = n
            by_stem.setdefault(_slug(p.stem), nid)

    # Resolve link targets to node ids. A link to something that isn't in the
    # index is dropped rather than becoming a phantom node.
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes.values():
        for target in n.links:
            tid = by_stem.get(target)
            if tid and tid != n.id:
                key = tuple(sorted((n.id, tid)))
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": n.id, "target": tid})

    degree: dict[str, int] = {nid: 0 for nid in nodes}
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1

    return {
        "nodes": [n.to_dict(degree[n.id]) for n in nodes.values()],
        "edges": edges,
        "stats": {
            "files": len(nodes),
            "links": len(edges),
            "unindexed": sum(1 for n in nodes.values() if n.unindexed),
        },
    }
