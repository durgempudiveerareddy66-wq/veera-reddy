"""Why is the vault index empty?

    python diagnose.py

Walks the real index roots and reports, for the first files it finds, exactly
which check rejects them — instead of me guessing from a distance.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BUILD = "fix2"          # so we can tell whether the running code is the new code

from agent import vault
from agent.paths import PathGuard, is_black_windows_path
from agent.runtime import ROOT, build

print(f"\n  DIAGNOSE  build={BUILD}  python={sys.version.split()[0]}  platform={sys.platform}")
print(f"  jarvis root : {ROOT}")
print(f"  this file   : {Path(__file__).resolve()}\n")

rt = build()
g = rt.guard

print("  index roots:")
for r in g.index_roots:
    n = len(list(r.rglob('*'))) if r.exists() else 0
    print(f"    {r}")
    print(f"      exists={r.exists()}  entries_under_it={n}")

print("\n  never-touch roots:")
for r in g._never_touch:
    print(f"    {r}")

if getattr(g, "warnings", ()):
    print("\n  guard warnings:")
    for w in g.warnings:
        print(f"    ! {w}")

print("\n  per-file verdicts (first 12 files found):")
shown = 0
for root in g.index_roots:
    if not root.exists():
        continue
    for p in sorted(root.rglob("*")):
        if shown >= 12:
            break
        try:
            if not p.is_file():
                continue
            reasons = []
            hit = [part for part in p.parts if part in vault.SKIP_DIRS]
            if hit:
                reasons.append(f"SKIP_DIRS matched {hit}")
            black, why = g.is_black(p)
            if black:
                reasons.append(f"is_black: {why}")
            if is_black_windows_path(str(p)):
                reasons.append("is_black_windows_path matched")
            size = p.stat().st_size
            if size > vault.MAX_BYTES:
                reasons.append(f"too big ({size})")
            if p.suffix.lower() not in vault.TEXT_SUFFIXES:
                reasons.append(f"suffix {p.suffix!r} not indexed as text")
            else:
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) < 20:
                    reasons.append(f"only {len(text.strip())} chars of text")
            verdict = "REJECTED: " + "; ".join(reasons) if reasons else "would become a NODE"
            print(f"    {p.name:44s} {verdict}")
            shown += 1
        except Exception as e:                                  # noqa: BLE001
            print(f"    {p.name:44s} EXCEPTION {type(e).__name__}: {e}")
            shown += 1

print()
stats = vault.build(g)["stats"]
print(f"  vault.build() says: {stats}\n")
