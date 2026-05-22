#!/usr/bin/env python3
"""Phase C one-way skill migration (devagentic issue #52).

Reads each `*.md` file in `~/.hermes/skills/` (or `HERMES_HOME/skills/`)
and writes a corresponding `kind:skill` graph node to a running
devagentic via the `skillCreate` mutation. After the migration,
the devagentic-side `skillResolve(name)` returns the same body the
file held, and `agent.devagentic_skills.resolve_skill_body(name)`
short-circuits the file read when
`DEVAGENTIC_SKILLS_GRAPH=1` is set.

Idempotency: the script appends — re-running creates a fresh
revision per skill (mirrors the rest of the graph's append-only
contract). To avoid double-writing during dry-runs, pass
`--dry-run` which prints the plan without mutating.

Usage:

    # Migrate all skills from the active profile's skills dir:
    python scripts/migrate_skills_to_graph.py

    # Dry-run (no mutations; prints what would land):
    python scripts/migrate_skills_to_graph.py --dry-run

    # Migrate from an explicit directory (skip HERMES_HOME):
    python scripts/migrate_skills_to_graph.py --from /path/to/skills

Requires:
  * A running devagentic at $DEVAGENTIC_BASE_URL (default
    http://127.0.0.1:6071/v1).
  * A bearer in $DEVAGENTIC_API_KEY (any value works when
    devagentic runs in trust-header mode).
  * X-User-Id resolution via the active hermes profile (or
    $DEVAGENTIC_USER_ID override).

Each file is treated as one skill. The skill `name` is the file
stem (`refactor-tips.md` → `refactor-tips`); the body is the full
file content. Front-matter parsing is intentionally NOT performed
here — graph callers can introspect / re-parse later. v0 keeps
the migration verbatim.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _default_skills_dir() -> Path:
    """Resolve the active skills directory.

    Prefers `hermes_constants.get_skills_dir()` when importable;
    falls back to `$HERMES_HOME/skills` or `~/.hermes/skills`."""
    try:
        from hermes_constants import get_skills_dir
        return get_skills_dir()
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "skills"


def _iter_skill_files(root: Path) -> list[Path]:
    """Yield each top-level `*.md` file in `root`. Recurses one
    level for skills laid out as `<name>/SKILL.md` (hermes' newer
    convention)."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix.lower() == ".md":
            out.append(entry)
        elif entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                out.append(skill_md)
    return out


def _skill_name(path: Path) -> str:
    """Derive a skill name from its file path.

    `~/.hermes/skills/refactor-tips.md`              → `refactor-tips`
    `~/.hermes/skills/auth-rules/SKILL.md`           → `auth-rules`
    """
    if path.name.upper() == "SKILL.MD":
        return path.parent.name
    return path.stem


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_dir", type=Path,
                   default=None,
                   help="skills directory to migrate from "
                        "(default: HERMES_HOME/skills)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the migration plan without writing")
    p.add_argument("--tag", action="append", default=[],
                   metavar="TAG",
                   help="extra tag to attach to every migrated skill; "
                        "repeatable")
    p.add_argument("--authored-by", default="migration:2026-05-22",
                   help="authored_by field for every migrated skill")
    args = p.parse_args()

    skills_root = args.from_dir or _default_skills_dir()
    print(f"migration source: {skills_root}")
    files = _iter_skill_files(skills_root)
    if not files:
        print(f"no *.md skills found under {skills_root}; nothing to do")
        return 0
    print(f"discovered {len(files)} skill file(s)")

    # Import the adapter lazily so --help works without the adapter
    # on the path (e.g. when running this from a fresh checkout
    # before `pip install`).
    try:
        from agent.devagentic_skills import create_skill
    except Exception as exc:
        print(f"ABORT: could not import agent.devagentic_skills: {exc}")
        return 2

    extra_tags = ["migrated:hermes-skills", *args.tag]

    written: list[str] = []
    failures: list[tuple[str, str]] = []
    for path in files:
        name = _skill_name(path)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append((str(path), f"read failed: {exc}"))
            continue
        if not body.strip():
            print(f"  SKIP {name}: file is empty")
            continue
        if args.dry_run:
            print(f"  [DRY] would migrate {name!r} "
                  f"({len(body)} chars, tags={extra_tags!r})")
            continue
        skill_id = create_skill(
            name=name, body=body,
            tags=extra_tags,
            authored_by=args.authored_by,
        )
        if skill_id is None:
            failures.append((str(path),
                              "create_skill returned None "
                              "(check devagentic is reachable + "
                              "X-User-Id resolvable)"))
            continue
        print(f"  wrote {name!r} → {skill_id}")
        written.append(skill_id)

    print()
    if args.dry_run:
        print(f"dry-run complete; would have migrated {len(files)} skill(s)")
        return 0
    print(f"migrated {len(written)} skill(s); {len(failures)} failure(s)")
    for path, reason in failures:
        print(f"  FAIL {path}: {reason}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
