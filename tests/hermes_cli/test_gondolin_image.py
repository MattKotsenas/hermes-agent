"""Unit tests for hermes_cli/gondolin_image.py.

Behavioral-only: we exercise the OCI tag mapping, runtime detection,
and host-package probe directly. The actual ``gondolin build`` and
``gondolin image ls`` shell-outs aren't tested here — they require a
real gondolin installation with podman/docker on the host and are
covered end-to-end by the integration tests under
``tests/integration/test_gondolin_terminal.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


def test_oci_image_tag_simple():
    """Simple repo:tag → safe gondolin tag."""
    from hermes_cli.gondolin_image import oci_image_tag

    assert oci_image_tag("python:3.11-slim") == "python:3.11-slim"


def test_oci_image_tag_with_namespace():
    """Slash-separated namespace gets flattened with underscores."""
    from hermes_cli.gondolin_image import oci_image_tag

    assert (
        oci_image_tag("nikolaik/python-nodejs:python3.11-nodejs20")
        == "nikolaik_python-nodejs:python3.11-nodejs20"
    )


def test_oci_image_tag_with_registry_host():
    """Full registry URL gets host.subdomains.tld_path:tag shape."""
    from hermes_cli.gondolin_image import oci_image_tag

    assert (
        oci_image_tag("mcr.microsoft.com/devcontainers/universal:latest")
        == "mcr.microsoft.com_devcontainers_universal:latest"
    )


def test_oci_image_tag_no_tag_defaults_to_latest():
    """Image name without a tag gets :latest appended."""
    from hermes_cli.gondolin_image import oci_image_tag

    assert oci_image_tag("python") == "python:latest"
    assert oci_image_tag("nikolaik/python-nodejs") == "nikolaik_python-nodejs:latest"


def test_oci_image_tag_with_digest_treated_as_no_tag():
    """An @sha256:... digest is not a tag; we still produce something.

    Behavior here isn't load-bearing — users who pin by digest are
    advanced enough to set an explicit tag in config. The contract is
    just "deterministic, doesn't raise, produces something gondolin
    will accept as a tag name."
    """
    from hermes_cli.gondolin_image import oci_image_tag

    result = oci_image_tag("python@sha256:abcdef")
    # We treat everything after the last `:` as a tag, so this is messy
    # but deterministic and won't collide with normal use.
    assert ":" in result
    assert not result.startswith(":")


def test_detect_oci_runtime_prefers_podman(monkeypatch):
    """When both podman and docker exist, podman wins.

    Behavioral rationale: gondolin users on WSL2/Linux mostly don't
    want a persistent Docker Desktop daemon. Podman is rootless and
    daemonless. Pinning the preference makes the wizard's "OCI runtime:
    podman" line predictable.
    """
    from hermes_cli import gondolin_image

    def fake_which(name):
        # Both available, but podman comes first in detection order.
        return f"/usr/bin/{name}" if name in ("podman", "docker") else None

    monkeypatch.setattr(gondolin_image.shutil, "which", fake_which)
    assert gondolin_image.detect_oci_runtime() == "podman"


def test_detect_oci_runtime_falls_back_to_docker(monkeypatch):
    """When only docker is installed, docker is returned."""
    from hermes_cli import gondolin_image

    def fake_which(name):
        return "/usr/bin/docker" if name == "docker" else None

    monkeypatch.setattr(gondolin_image.shutil, "which", fake_which)
    assert gondolin_image.detect_oci_runtime() == "docker"


def test_detect_oci_runtime_returns_none_when_absent(monkeypatch):
    """No OCI runtime on $PATH → None (caller will surface the error)."""
    from hermes_cli import gondolin_image

    monkeypatch.setattr(gondolin_image.shutil, "which", lambda _: None)
    assert gondolin_image.detect_oci_runtime() is None


def test_missing_build_host_packages_returns_subset(monkeypatch):
    """Returns only the packages that are actually missing."""
    from hermes_cli import gondolin_image

    # Pretend cpio is present, lz4 is not.
    def fake_which(name):
        return "/usr/bin/cpio" if name == "cpio" else None

    monkeypatch.setattr(gondolin_image.shutil, "which", fake_which)
    assert gondolin_image.missing_build_host_packages() == ["lz4"]


def test_ensure_built_rejects_empty():
    """Empty image string is a misconfiguration, raises with a hint."""
    from hermes_cli.gondolin_image import ensure_built

    with pytest.raises(RuntimeError, match="Invalid gondolin image"):
        ensure_built("")


def test_ensure_built_rejects_none():
    """None is a misconfiguration, raises with a hint."""
    from hermes_cli.gondolin_image import ensure_built

    with pytest.raises(RuntimeError, match="Invalid gondolin image"):
        ensure_built(None)  # type: ignore[arg-type]


def test_ensure_built_passes_through_absolute_path(tmp_path):
    """Absolute path to built assets flows through unchanged.

    Power-user contract: if you already have built gondolin assets in a
    directory, point image: at that absolute path and Hermes won't
    second-guess you.
    """
    from hermes_cli.gondolin_image import ensure_built

    assets = str(tmp_path / "gondolin-assets")
    assert ensure_built(assets) == assets


def test_ensure_built_passes_through_already_built_tag(monkeypatch):
    """If the input is already a gondolin tag in the local store, use it."""
    from hermes_cli import gondolin_image

    # Simulate "tag exists in gondolin's local image store".
    monkeypatch.setattr(
        gondolin_image, "is_image_built", lambda tag: tag == "my-custom:1"
    )
    assert gondolin_image.ensure_built("my-custom:1") == "my-custom:1"


def test_ensure_built_returns_derived_tag_when_already_built(monkeypatch):
    """If the OCI image has already been materialized, return the derived tag.

    Common case: second session after a successful first build.
    """
    from hermes_cli import gondolin_image

    derived = gondolin_image.oci_image_tag("python:3.11-slim")
    # First call: "is the raw OCI name a built tag?" → no.
    # Second call: "is the derived tag built?" → yes.
    calls = []

    def fake_is_built(tag):
        calls.append(tag)
        return tag == derived

    monkeypatch.setattr(gondolin_image, "is_image_built", fake_is_built)
    assert gondolin_image.ensure_built("python:3.11-slim") == derived


def test_ensure_built_raises_when_no_oci_runtime(monkeypatch):
    """If we need to build but podman/docker are missing, raise with a fix."""
    from hermes_cli import gondolin_image

    monkeypatch.setattr(gondolin_image, "is_image_built", lambda _: False)
    monkeypatch.setattr(gondolin_image, "detect_oci_runtime", lambda: None)

    with pytest.raises(RuntimeError) as exc:
        gondolin_image.ensure_built("python:3.11-slim")
    msg = str(exc.value)
    assert "python:3.11-slim" in msg
    assert "podman" in msg or "docker" in msg
    assert "terminal.gondolin.image" in msg


def test_ensure_built_raises_when_missing_host_packages(monkeypatch):
    """If cpio/lz4 are missing, raise with the apt install command."""
    from hermes_cli import gondolin_image

    monkeypatch.setattr(gondolin_image, "is_image_built", lambda _: False)
    monkeypatch.setattr(gondolin_image, "detect_oci_runtime", lambda: "podman")
    monkeypatch.setattr(
        gondolin_image, "missing_build_host_packages", lambda: ["cpio", "lz4"]
    )

    with pytest.raises(RuntimeError) as exc:
        gondolin_image.ensure_built("python:3.11-slim")
    msg = str(exc.value)
    assert "cpio" in msg and "lz4" in msg
    assert "apt-get install" in msg
