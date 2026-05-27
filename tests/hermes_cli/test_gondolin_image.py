"""Unit tests for hermes_cli/gondolin_image.py.

Behavioural-only — no monkey-patching of internal helpers. The defensive
exception-swallowing branches in ``is_hermes_runtime_present`` and
``run_build`` aren't tested because exercising them requires patching
``subprocess.run`` or ``shutil.which``, which would test the mock and
not the code. Those branches exist to make sure a probe failure
degrades to today's fallback (alpine-base) rather than crashing backend
construction; that contract is checked end-to-end in the
``test_execute_code_round_trip`` integration test.
"""

from __future__ import annotations

import json

from hermes_cli import gondolin_image


def test_hermes_runtime_tag_includes_version():
    """Tag is versioned with the running Hermes version so two installs
    on the same version produce identical images, and an upgrade that
    changes the build spec doesn't silently swap images mid-flight."""
    tag = gondolin_image.HERMES_RUNTIME_TAG
    assert tag.startswith("hermes-runtime:")
    _, _, version = tag.partition(":")
    assert version
    assert version != "hermes-runtime"  # sanity: partition worked


def test_bundled_build_config_exists():
    """The shipped build spec must exist in the repo — without it
    ``hermes gondolin build`` has nothing to feed gondolin."""
    assert gondolin_image.HERMES_RUNTIME_BUILD_CONFIG.is_file(), (
        f"build config missing at {gondolin_image.HERMES_RUNTIME_BUILD_CONFIG}"
    )


def test_bundled_build_config_includes_required_packages():
    """The pinned package list must include python3 (so execute_code
    works), bash (so non-Bourne shell idioms work), and ca-certs (so
    https works). Anyone editing the spec should keep these — without
    them the whole point of hermes-runtime evaporates.

    This is a regression guard, not a complete schema check.
    """
    spec = json.loads(gondolin_image.HERMES_RUNTIME_BUILD_CONFIG.read_text())
    pkgs = set(spec["alpine"]["rootfsPackages"])
    for required in ("python3", "bash", "ca-certificates"):
        assert required in pkgs, (
            f"build spec is missing {required!r}; the hermes-runtime "
            f"image was designed around python3 + bash + ca-certificates"
        )


def test_missing_host_packages_returns_subset_of_declared_list():
    """The function must only return packages from its own declared
    host-package list — it can never invent entries. This is a
    contract check; whether the result is empty or full depends on the
    host running the test."""
    missing = gondolin_image.missing_host_packages()
    assert set(missing).issubset(set(gondolin_image.HERMES_RUNTIME_HOST_PACKAGES))
