#!/usr/bin/env python3
"""Phase D one-way memory migration (devagentic issue #54).

Reads `MEMORY.md`, `USER.md`, `SOUL.md` from `HERMES_HOME` (or a
custom dir via `--from`) and writes each one as a
`kind:user-fact` graph node in devagentic via `userFactCreate`.
After the migration, devagentic's `userFactQuery` returns facts
sourced from these files, and `agent.devagentic_memory` short-
circuits memory reads when `DEVAGENTIC_MEMORY_GRAPH=1`.

Usage:

    python scripts/migrate_memory_to_graph.py            # migrate
    python scripts/migrate_memory_to_graph.py --dry-run  # preview
    python scripts/migrate_memory_to_graph.py --from /custom/path

Granularity: one node per file (file-level migration v0). Each
file's full content becomes the `body` of one `kind:user-fact`;
tags identify the origin file (`origin:MEMORY.md`,
`origin:USER.md`, etc.) plus the migration date. Per-paragraph
or per-bullet granularity is documented as deferred — file-level
keeps the migration round-trippable and the supersede story
simple (refining one file == one supersede).

Idempotency: append-only. Re-running creates fresh facts; the
old ones remain visible in the graph. To dedupe a re-migration,
manually `userFactSupersede(old_id, new_id)` for each pair, or
use `--supersede-prior` (flagged but not implemented in v0).

Requires:
  * A running devagentic at $DEVAGENTIC_BASE_URL (default
    http://127.0.0.1:6071/v1).
  * A bearer in $DEVAGENTIC_API_KEY (any value works when
    devagentic runs in trust-header mode).
  * X-User-Id resolution via the active hermes profile (or
    $DEVAGENTIC_USER_ID override).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# Files we attempt to migrate. Order matters only for the printed
# plan; the actual writes can happen in any order.
_MEMORY_FILES = ("MEMORY.md", "USER.md", "SOUL.md")


def _default_memory_dir() -> Path:
    """Resolve the active hermes home dir. Prefers
    `hermes_constants.get_hermes_home()` when importable; falls
    back to `$HERMES_HOME` or `~/.hermes`."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home)


def _today_tag() -> str:
    """`migration:YYYY-MM-DD` tag for traceability."""
    return f"migration:{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}"


def _iter_memory_files(root: Path) -> list[Path]:
    """Yield each MEMORY.md / USER.md / SOUL.md file in `root` that
    exists and is non-empty."""
    out: list[Path] = []
    for fname in _MEMORY_FILES:
        p = root / fname
        if not p.is_file():
            continue
        try:
            if not p.read_text(encoding="utf-8").strip():
                continue
        except OSError:
            continue
        out.append(p)
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_dir", type=Path, default=None,
                   help="hermes home dir to migrate from "
                        "(default: HERMES_HOME)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the migration plan without writing")
    p.add_argument("--tag", action="append", default=[],
                   metavar="TAG",
                   help="extra tag to attach to every migrated fact; "
                        "repeatable")
    p.add_argument("--confidence", type=float, default=0.9,
                   help="confidence value for migrated facts "
                        "(default: 0.9; user-facts from files are "
                        "high-trust but not perfect since they may "
                        "have aged)")
    args = p.parse_args()

    root = args.from_dir or _default_memory_dir()
    print(f"migration source: {root}")
    files = _iter_memory_files(root)
    if not files:
        print(f"no MEMORY.md / USER.md / SOUL.md files found "
              f"under {root}; nothing to do")
        return 0
    print(f"discovered {len(files)} memory file(s): "
          f"{[f.name for f in files]}")

    # Lazy import so --help works without the adapter on the path.
    try:
        from agent.devagentic_memory import create_user_fact
    except Exception as exc:
        print(f"ABORT: could not import agent.devagentic_memory: {exc}")
        return 2

    today = _today_tag()
    extra_tags = list(args.tag)

    written: list[str] = []
    failures: list[tuple[str, str]] = []
    for path in files:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append((str(path), f"read failed: {exc}"))
            continue
        if not body.strip():
            print(f"  SKIP {path.name}: file empty")
            continue
        tags = [
            f"origin:{path.name}",
            today,
            "kind:hermes-memory-migration",
            *extra_tags,
        ]
        source = f"hermes-migration:{path.name}"
        if args.dry_run:
            print(f"  [DRY] would migrate {path.name} "
                  f"({len(body)} chars, tags={tags!r}, "
                  f"source={source!r})")
            continue
        fact_id = create_user_fact(
            body=body, source=source, tags=tags,
            confidence=args.confidence)
        if fact_id is None:
            failures.append((str(path),
                              "create_user_fact returned None "
                              "(check devagentic is reachable + "
                              "X-User-Id resolvable)"))
            continue
        print(f"  wrote {path.name} → {fact_id}")
        written.append(fact_id)

    print()
    if args.dry_run:
        print(f"dry-run complete; would have migrated "
              f"{len(files)} fact(s)")
        return 0
    print(f"migrated {len(written)} fact(s); "
          f"{len(failures)} failure(s)")
    for path, reason in failures:
        print(f"  FAIL {path}: {reason}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
