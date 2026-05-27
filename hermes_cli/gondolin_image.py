"""Hermes-runtime gondolin image: build, presence check, and host-deps probe.

The ``gondolin`` terminal backend boots VMs from gondolin-format images
sourced from gondolin's local image store (``~/.cache/gondolin/``).
Gondolin's built-in default, ``alpine-base:latest``, ships BusyBox plus
networking but no python3/node/uv/bash — which makes the built-in
``execute_code`` tool fail and degrades skills that shell out to those
interpreters.

Rather than publishing our own image (registry, signing keys, release
pipeline) or shipping a vendor-managed default we don't control, we
ship a **pinned build spec** at
``tools/environments/gondolin_host/hermes-runtime.json`` and let
gondolin's own build pipeline produce a local image tagged
``hermes-runtime:<hermes-version>`` on first run.

This module is the shared surface:

- :data:`HERMES_RUNTIME_TAG` — the tag the rest of the codebase looks
  for. Versioned with ``hermes_cli.__version__`` so a Hermes upgrade
  that changes the spec doesn't silently swap images mid-flight.
- :data:`HERMES_RUNTIME_BUILD_CONFIG` — absolute path to the bundled
  build spec.
- :data:`HERMES_RUNTIME_HOST_PACKAGES` — host packages gondolin's
  build step needs (cpio, lz4 — both stock apt, both absent by default
  on Ubuntu 24.04).
- :func:`missing_host_packages` — returns the subset of host packages
  not on ``$PATH``. Doctor uses this to surface the exact
  ``apt install`` command up front.
- :func:`is_hermes_runtime_present` — quick probe against
  ``gondolin image ls`` to tell callers whether the tag is built.
- :func:`run_build` — invokes ``gondolin build`` with our config, used
  by the ``hermes gondolin build`` CLI subcommand.

Everything here is side-effect-free except :func:`run_build`, which is
explicitly the one place we touch the host's gondolin image store.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, List, Optional, Union

logger = logging.getLogger(__name__)


def _hermes_version() -> str:
    """Return the running Hermes version string.

    Falls back to ``unknown`` if the package metadata can't be loaded —
    callers should treat that case the same as "no built image", i.e.
    surface a build prompt rather than crashing.
    """
    try:
        from hermes_cli import __version__
        return __version__
    except Exception:  # noqa: BLE001 — defensive: never crash callers
        return "unknown"


#: The image tag the gondolin backend resolves to by default.
#:
#: Versioned with the Hermes release so two installs on the same
#: version produce identical images, and a Hermes upgrade that changes
#: the build spec doesn't silently swap images mid-flight.
HERMES_RUNTIME_TAG = f"hermes-runtime:{_hermes_version()}"


def _repo_root() -> Path:
    """Locate the bundled ``hermes-runtime.json`` next to the daemon source."""
    # tools/environments/gondolin_host/hermes-runtime.json lives next to
    # daemon.mjs. This module lives at hermes_cli/gondolin_image.py.
    # Walk up to the repo root, then descend.
    here = Path(__file__).resolve()
    # hermes_cli/ -> repo root
    return here.parent.parent


HERMES_RUNTIME_BUILD_CONFIG: Path = (
    _repo_root()
    / "tools"
    / "environments"
    / "gondolin_host"
    / "hermes-runtime.json"
)


#: Host packages gondolin's build pipeline shells out to.
#:
#: gondolin's build mostly runs in-process (Alpine minirootfs extraction,
#: package install via apk inside a chroot, kernel fetch from libkrunfw
#: releases). The final initramfs packaging step shells out to ``cpio``
#: and ``lz4``; on Ubuntu 24.04 neither is in the base install. ``apk``
#: and ``mkfs.ext4`` are also referenced but gondolin bundles its own
#: vendored binaries — only cpio + lz4 actually need to be on $PATH.
#:
#: Discovered empirically: see docs/design/gondolin-terminal-backend.md
#: § "Default image: first-run local build".
HERMES_RUNTIME_HOST_PACKAGES: tuple[str, ...] = ("cpio", "lz4")


def missing_host_packages() -> List[str]:
    """Return the subset of :data:`HERMES_RUNTIME_HOST_PACKAGES` not on ``$PATH``."""
    return [p for p in HERMES_RUNTIME_HOST_PACKAGES if shutil.which(p) is None]


def _resolve_gondolin_cli() -> Optional[Path]:
    """Locate the bundled ``gondolin`` CLI shipped under the daemon's node_modules.

    Returns the path to the executable JS file, or ``None`` if the daemon
    deps haven't been installed yet (``npm install`` in
    ``tools/environments/gondolin_host/`` wasn't run). Callers should
    treat ``None`` as a setup-incomplete signal — same shape as missing
    node.
    """
    candidate = (
        _repo_root()
        / "tools"
        / "environments"
        / "gondolin_host"
        / "node_modules"
        / "@earendil-works"
        / "gondolin"
        / "dist"
        / "bin"
        / "gondolin.js"
    )
    return candidate if candidate.is_file() else None


def is_hermes_runtime_present(tag: str = HERMES_RUNTIME_TAG) -> bool:
    """Return True iff ``gondolin image ls`` lists ``tag``.

    Fast probe (~100ms): runs ``node gondolin.js image ls`` and matches
    on the tag prefix. Returns False on any failure — missing node,
    missing gondolin CLI, non-zero exit, parse error — because the
    caller's job is to "prompt the user to build it" either way; we
    don't want a flaky transient to look like "image present".
    """
    cli = _resolve_gondolin_cli()
    if cli is None:
        return False
    node = shutil.which("node")
    if node is None:
        return False
    try:
        result = subprocess.run(
            [node, str(cli), "image", "ls"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    # gondolin image ls output is line-per-ref: "name:tag  x86_64=<build-id>"
    # We just want a hit on the tag at start-of-line.
    for line in result.stdout.splitlines():
        if line.strip().startswith(tag):
            return True
    return False


def run_build(
    *,
    tag: str = HERMES_RUNTIME_TAG,
    config_path: Path = HERMES_RUNTIME_BUILD_CONFIG,
    stdout: Optional[Union[int, IO[Any]]] = None,
    stderr: Optional[Union[int, IO[Any]]] = None,
) -> int:
    """Invoke ``gondolin build`` with the bundled hermes-runtime spec.

    Returns the upstream gondolin exit code. We don't translate errors —
    if gondolin's build pipeline fails (missing host package, mirror
    flake, integrity check), the user wants to see gondolin's own error
    text, not a Hermes-wrapped paraphrase. This is the supply chain;
    obscuring it would be worse than honest.

    ``stdout`` / ``stderr``: passed through to subprocess. ``None`` lets
    the build's output flow to the caller's terminal. The CLI command
    wires this up.
    """
    cli = _resolve_gondolin_cli()
    if cli is None:
        print(
            "gondolin CLI not found — run `npm install` in "
            "tools/environments/gondolin_host/ first.",
            file=sys.stderr,
        )
        return 1
    node = shutil.which("node")
    if node is None:
        print(
            "node not found on PATH — install Node.js >= 20 first.",
            file=sys.stderr,
        )
        return 1
    if not config_path.is_file():
        print(
            f"build config not found at {config_path}",
            file=sys.stderr,
        )
        return 1

    # Pre-flight host-deps check so we fail with an actionable message
    # before gondolin itself dies inside the pipeline.
    missing = missing_host_packages()
    if missing:
        pkgs = " ".join(missing)
        print(
            f"missing host packages: {pkgs}\n"
            f"install with:\n"
            f"  sudo apt-get install -y {pkgs}",
            file=sys.stderr,
        )
        return 1

    cmd = [node, str(cli), "build", "--config", str(config_path), "--tag", tag]
    logger.info("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, stdout=stdout, stderr=stderr)
    except OSError as e:
        print(f"failed to invoke gondolin build: {e}", file=sys.stderr)
        return 1
    return result.returncode
