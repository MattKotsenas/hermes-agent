"""Lazy OCI image materialization for the gondolin terminal backend.

The gondolin backend takes a plain OCI image name in
``terminal.gondolin.image`` (same shape docker uses for
``terminal.docker_image``). The first time a session needs that image
gondolin's build pipeline pulls it via docker/podman and exports its
filesystem into a gondolin-format rootfs cached under
``~/.cache/gondolin/``. Subsequent sessions hit the cache.

This module is the thin glue between Hermes config and that build
pipeline:

- :func:`oci_image_tag` — maps an OCI image name to the gondolin tag we
  cache it under. Deterministic: same OCI name → same gondolin tag.
- :func:`is_image_built` — checks whether the gondolin tag is in the
  local image store.
- :func:`detect_oci_runtime` — returns ``"docker"`` or ``"podman"`` if
  either is on ``$PATH``, else ``None``.
- :func:`missing_build_host_packages` — returns the subset of host
  packages gondolin's build step needs that aren't on ``$PATH``.
- :func:`build_oci_image` — invokes ``gondolin build`` with an OCI
  config to materialize the image. Synchronous; surfaces gondolin's own
  output rather than wrapping it.
- :func:`ensure_built` — the one-call entry point used by the wizard
  and by ``_create_environment``: idempotent, returns the gondolin tag
  on success, raises with an actionable message on failure.

A user-pinned absolute path (already-built gondolin assets) flows
through ensure_built unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO, Any, List, Optional, Union

logger = logging.getLogger(__name__)


#: Host packages gondolin's build pipeline shells out to.
#:
#: ``cpio`` and ``lz4`` are needed for initramfs assembly. ``apk`` and
#: ``mkfs.ext4`` are also referenced but gondolin bundles vendored
#: binaries for those — only ``cpio`` + ``lz4`` actually need ``$PATH``.
#:
#: Discovered empirically: see docs/design/gondolin-terminal-backend.md.
BUILD_HOST_PACKAGES: tuple[str, ...] = ("cpio", "lz4")


#: Default kernel package and image gondolin uses to wrap an OCI rootfs.
#:
#: Gondolin still needs an Alpine-built kernel + initramfs even when the
#: rootfs is sourced from OCI. These values match
#: gondolin/src/build/init-config.ts defaults.
_DEFAULT_ALPINE_VERSION = "3.23.0"
_DEFAULT_KERNEL_PACKAGE = "linux-virt"
_DEFAULT_KERNEL_IMAGE = "vmlinuz-virt"
_DEFAULT_KRUNFW_VERSION = "v5.2.1"


def missing_build_host_packages() -> List[str]:
    """Return the subset of :data:`BUILD_HOST_PACKAGES` not on ``$PATH``."""
    return [p for p in BUILD_HOST_PACKAGES if shutil.which(p) is None]


def detect_oci_runtime() -> Optional[str]:
    """Return ``"docker"`` or ``"podman"`` if either is available, else None.

    Order: podman first, docker second. Podman is rootless-friendly and
    doesn't need a daemon running on Linux; docker requires the user to
    be in the ``docker`` group or have a daemon listening. If both are
    installed we prefer podman to match the no-Docker-Desktop story most
    gondolin users have on WSL2/Linux.
    """
    for runtime in ("podman", "docker"):
        if shutil.which(runtime):
            return runtime
    return None


_TAG_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def oci_image_tag(oci_image: str) -> str:
    """Map an OCI image name to the gondolin tag we cache its build under.

    The mapping is deterministic and reversible enough to debug. The
    OCI registry/repo path is collapsed to a safe identifier and joined
    with the OCI tag (or ``latest`` if absent):

    - ``nikolaik/python-nodejs:python3.11-nodejs20`` →
      ``nikolaik_python-nodejs:python3.11-nodejs20``
    - ``mcr.microsoft.com/devcontainers/universal:latest`` →
      ``mcr.microsoft.com_devcontainers_universal:latest``
    - ``python:3.11-slim`` → ``python:3.11-slim``

    The gondolin tag namespace is flat; this mapping just keeps it
    legible in ``gondolin image ls`` output.
    """
    if ":" in oci_image and oci_image.rfind(":") > oci_image.rfind("/"):
        name, _, tag = oci_image.rpartition(":")
    else:
        name = oci_image
        tag = "latest"
    safe_name = _TAG_SAFE_RE.sub("_", name).strip("_")
    safe_tag = _TAG_SAFE_RE.sub("_", tag).strip("_") or "latest"
    return f"{safe_name}:{safe_tag}"


def _repo_root() -> Path:
    """Locate the Hermes repo root so we can find the daemon CLI."""
    here = Path(__file__).resolve()
    # hermes_cli/ -> repo root
    return here.parent.parent


def _resolve_gondolin_cli() -> Optional[Path]:
    """Locate the bundled ``gondolin`` CLI shipped under the daemon's node_modules.

    Returns the path to the executable JS file, or ``None`` if the daemon
    deps haven't been installed yet (``npm install`` in
    ``tools/environments/gondolin_host/`` wasn't run).
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


def is_image_built(gondolin_tag: str) -> bool:
    """Return True iff ``gondolin image ls`` lists ``gondolin_tag``.

    Returns False on any failure (missing node, missing gondolin CLI,
    non-zero exit) — caller's job is "build it if not present" either
    way; we don't want a flaky transient to look like "image present".
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
    for line in result.stdout.splitlines():
        if line.strip().startswith(gondolin_tag):
            return True
    return False


def _build_config_for_oci(oci_image: str, runtime: str) -> dict:
    """Construct a gondolin build config that wraps an OCI image as rootfs.

    See gondolin/docs/custom-images.md § OCI Support for the schema.
    Kernel + initramfs come from Alpine; rootfs comes from OCI.
    """
    return {
        "arch": "x86_64",
        "distro": "alpine",
        "oci": {
            "image": oci_image,
            "runtime": runtime,
        },
        "alpine": {
            "version": _DEFAULT_ALPINE_VERSION,
            "kernelPackage": _DEFAULT_KERNEL_PACKAGE,
            "kernelImage": _DEFAULT_KERNEL_IMAGE,
            "krunfwVersion": _DEFAULT_KRUNFW_VERSION,
        },
        "rootfs": {
            "label": "gondolin-root",
        },
    }


def build_oci_image(
    oci_image: str,
    *,
    tag: Optional[str] = None,
    runtime: Optional[str] = None,
    stdout: Optional[Union[int, IO[Any]]] = None,
    stderr: Optional[Union[int, IO[Any]]] = None,
) -> int:
    """Invoke ``gondolin build`` against an OCI image.

    Returns the gondolin exit code. Errors are surfaced as gondolin
    prints them — we don't paraphrase the supply chain.

    ``tag`` defaults to :func:`oci_image_tag` of ``oci_image``.
    ``runtime`` defaults to :func:`detect_oci_runtime`.
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

    chosen_runtime = runtime or detect_oci_runtime()
    if chosen_runtime is None:
        print(
            "Neither podman nor docker found on $PATH. Gondolin needs one "
            "of them to pull and export the OCI image.\n"
            "Install with: sudo apt-get install -y podman",
            file=sys.stderr,
        )
        return 1

    missing = missing_build_host_packages()
    if missing:
        pkgs = " ".join(missing)
        print(
            f"missing host packages: {pkgs}\n"
            f"install with:\n"
            f"  sudo apt-get install -y {pkgs}",
            file=sys.stderr,
        )
        return 1

    target_tag = tag or oci_image_tag(oci_image)
    config = _build_config_for_oci(oci_image, chosen_runtime)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="gondolin-oci-",
        delete=False,
    ) as fp:
        json.dump(config, fp, indent=2)
        config_path = fp.name

    try:
        cmd = [
            node, str(cli), "build",
            "--config", config_path,
            "--tag", target_tag,
        ]
        logger.info("running: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, stdout=stdout, stderr=stderr)
        except OSError as e:
            print(f"failed to invoke gondolin build: {e}", file=sys.stderr)
            return 1
        return result.returncode
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


def ensure_built(image: str) -> str:
    """Resolve a Hermes config ``image`` value to a gondolin daemon image arg.

    Three input shapes:

    1. **OCI image name** (e.g. ``nikolaik/python-nodejs:python3.11-nodejs20``,
       ``mcr.microsoft.com/devcontainers/universal:latest``). We map it
       to a gondolin tag, build if absent, return the tag.
    2. **Pre-built gondolin tag** in the local store (e.g. one a power
       user built via ``gondolin build --tag my-image:1``). We detect
       this by checking ``gondolin image ls`` first; if the input is
       already a tag in the store, return it unchanged.
    3. **Absolute path to a directory of built assets**. We pass through.

    Raises :class:`RuntimeError` with an actionable message on build
    failure or invalid input.
    """
    if not isinstance(image, str) or not image:
        raise RuntimeError(
            f"Invalid gondolin image configuration: {image!r}. "
            "Set `terminal.gondolin.image` to an OCI image name "
            "(e.g. 'python:3.11-slim'), a pre-built gondolin tag, or an "
            "absolute path to a directory of built assets."
        )

    # Absolute path — gondolin's contract, pass through.
    if os.path.isabs(image):
        return image

    # Already a pre-built gondolin tag in the store? Use it as-is.
    if is_image_built(image):
        return image

    # OCI image name. Map to a stable gondolin tag and build if missing.
    gondolin_tag = oci_image_tag(image)
    if is_image_built(gondolin_tag):
        return gondolin_tag

    runtime = detect_oci_runtime()
    if runtime is None:
        raise RuntimeError(
            f"Gondolin terminal backend needs to build the rootfs for "
            f"OCI image {image!r}, but neither podman nor docker is on "
            f"$PATH. Install one of them:\n"
            f"  sudo apt-get install -y podman\n"
            f"Or set `terminal.gondolin.image` to an already-built "
            f"gondolin tag or an absolute path to built assets."
        )

    missing = missing_build_host_packages()
    if missing:
        pkgs = " ".join(missing)
        raise RuntimeError(
            f"Gondolin terminal backend needs to build the rootfs for "
            f"OCI image {image!r}, but the build pipeline needs these "
            f"host packages on $PATH: {pkgs}\n"
            f"Install with:\n"
            f"  sudo apt-get install -y {pkgs}"
        )

    logger.info(
        "gondolin image %s not built; running gondolin build (may take "
        "several minutes for large images)",
        gondolin_tag,
    )
    rc = build_oci_image(image, tag=gondolin_tag, runtime=runtime)
    if rc != 0 or not is_image_built(gondolin_tag):
        raise RuntimeError(
            f"gondolin build failed for OCI image {image!r} (exit {rc}). "
            f"See output above for gondolin's own diagnostics. To retry "
            f"manually:\n"
            f"  cd tools/environments/gondolin_host && \\\n"
            f"  node node_modules/@earendil-works/gondolin/dist/bin/"
            f"gondolin.js build --tag {gondolin_tag} \\\n"
            f"    --config <(echo '<paste the auto-generated config>')"
        )
    return gondolin_tag
