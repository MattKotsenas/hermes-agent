"""`hermes sandboxes` CLI subcommand.

Gives users direct visibility and control over the sandbox-storage
directory at ``~/.hermes/sandboxes/`` (or wherever ``TERMINAL_SANDBOX_DIR``
points). This is the host-side scratch that the docker, gondolin, and
singularity terminal backends write into during sessions::

    hermes sandboxes                     # same as `status`
    hermes sandboxes status              # per-backend usage breakdown
    hermes sandboxes prune [opts]        # delete stale per-task sandboxes
    hermes sandboxes prune --dry-run     # show what would go

The pruner only touches *per-task* directories (``docker/<task_id>/`` and
``gondolin-<task_id>/``); shared infra like the singularity SIF cache is
counted but left alone.

None of these require the agent to be running. Safe to call any time.
"""
from __future__ import annotations

import argparse
from typing import Set

from tools.environments import sandbox_inventory as inv
from tools.environments.base import get_sandbox_dir


def _resolve_backends(arg_value) -> Set[str] | None:
    """Convert the --backend CLI value (comma-separated or None) into a set
    suitable for ``inv.select_stale``. ``None`` means "all prunable"."""
    if not arg_value:
        return None
    return {b.strip() for b in arg_value.split(",") if b.strip()}


def cmd_status(args: argparse.Namespace) -> int:
    root = get_sandbox_dir()
    report = inv.scan(root)

    print(f"Sandbox root:    {root}")
    print(f"Total size:      {inv._fmt_bytes(report.total_bytes)}")
    print(f"Entries:         {len(report.entries)}")

    if not report.entries:
        return 0

    # Group by backend for the summary table.
    by_backend: dict[str, list[inv.SandboxEntry]] = {}
    for e in report.entries:
        by_backend.setdefault(e.backend, []).append(e)

    print()
    print(f"  {'BACKEND':<14}  {'COUNT':>6}  {'SIZE':>10}  PRUNABLE")
    for backend in sorted(by_backend):
        items = by_backend[backend]
        total = sum(e.size_bytes for e in items)
        prunable = "yes" if all(e.prunable for e in items) else "no"
        print(f"  {backend:<14}  {len(items):>6}  {inv._fmt_bytes(total):>10}  {prunable}")

    # Optional detail: list per-task entries, oldest first.
    if getattr(args, "verbose", False):
        per_task = sorted(
            [e for e in report.entries if e.task_id is not None],
            key=lambda e: e.mtime,
        )
        if per_task:
            print()
            print(f"  {'BACKEND':<10}  {'TASK_ID':<30}  {'SIZE':>10}  AGE")
            for e in per_task[: args.limit]:
                age = f"{int(e.age_days)}d" if e.age_days >= 1 else f"{int(e.age_days * 24)}h"
                tid = e.task_id or ""
                if len(tid) > 30:
                    tid = tid[:27] + "..."
                print(f"  {e.backend:<10}  {tid:<30}  {inv._fmt_bytes(e.size_bytes):>10}  {age}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root = get_sandbox_dir()
    report = inv.scan(root)
    backends = _resolve_backends(getattr(args, "backend", None))

    stale = inv.select_stale(
        report,
        older_than_days=args.older_than,
        backends=backends,
    )

    if not stale:
        print(f"No sandboxes older than {args.older_than}d to prune.")
        return 0

    total_bytes = sum(e.size_bytes for e in stale)
    print(f"Sandbox root: {root}")
    if args.dry_run:
        print(f"Would delete {len(stale)} sandbox(es), reclaiming {inv._fmt_bytes(total_bytes)}:")
    else:
        print(f"Deleting {len(stale)} sandbox(es), {inv._fmt_bytes(total_bytes)}:")

    for e in stale:
        age = f"{int(e.age_days)}d"
        print(f"  {e.backend:<10}  {e.task_id or '-':<30}  {inv._fmt_bytes(e.size_bytes):>10}  {age}")

    result = inv.prune(stale, dry_run=args.dry_run)

    print()
    if args.dry_run:
        print(f"Dry run: {result.would_delete} would be deleted.")
    else:
        print(f"Deleted {result.deleted} sandbox(es), reclaimed {inv._fmt_bytes(result.bytes_freed)}.")
        if result.errors:
            print(f"Errors: {len(result.errors)}")
            for err in result.errors:
                print(f"  {err}")
            return 1
    return 0


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire subcommands onto the ``hermes sandboxes`` parser."""
    parser.set_defaults(func=cmd_status)  # bare `hermes sandboxes` → status
    subs = parser.add_subparsers(dest="sandboxes_command", metavar="COMMAND")

    p_status = subs.add_parser(
        "status",
        help="Show per-backend disk usage for ~/.hermes/sandboxes/",
    )
    p_status.add_argument(
        "-v", "--verbose", action="store_true",
        help="Also list per-task sandboxes (oldest first)",
    )
    p_status.add_argument(
        "--limit", type=int, default=20,
        help="Max per-task entries to list in verbose mode (default 20)",
    )
    p_status.set_defaults(func=cmd_status)

    p_prune = subs.add_parser(
        "prune",
        help="Delete stale per-task sandbox directories",
    )
    p_prune.add_argument(
        "--older-than", type=float, default=7.0, metavar="DAYS",
        help="Delete per-task sandboxes whose mtime is older than N days (default 7)",
    )
    p_prune.add_argument(
        "--backend", metavar="LIST",
        help="Comma-separated list of backends to prune (default: all prunable). "
             "Choices: docker, gondolin. Shared infra (singularity) is never pruned.",
    )
    p_prune.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Show what would be deleted without removing anything",
    )
    p_prune.set_defaults(func=cmd_prune)
