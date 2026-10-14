"""Regression: the shim must install somewhere the Hermes upgrade cannot erase.

Hermes resolves context engines bundled-first, then from $HERMES_HOME/plugins/.
The bundled tree is runtime-owned; an upgrade that empties it leaves an empty
directory that still shadows the user install, so the engine silently stops
loading. The installer therefore defaults to the user-plugin dir.
"""
from __future__ import annotations

from pathlib import Path

from hermes_context_tuner import cli


def test_install_defaults_to_user_plugin_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    target = cli.install_engine()

    assert target == tmp_path / "plugins" / "context-tuner"
    assert (target / "__init__.py").is_file()
    assert (target / "plugin.yaml").is_file()
    # The bundled tree must not have been touched.
    assert not (tmp_path / "hermes-agent" / "plugins" / "context_engine").exists()


def test_install_into_explicit_hermes_repo_uses_bundled_path(tmp_path):
    repo = tmp_path / "checkout"

    target = cli.install_engine(hermes_repo=str(repo))

    assert target == repo / "plugins" / "context_engine" / "context-tuner"
    assert (target / "__init__.py").is_file()
    assert (target / "plugin.yaml").is_file()


def test_manifest_version_is_not_stale(tmp_path, monkeypatch):
    """plugin.yaml ships the shim; it must not drift behind the package version."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = cli.install_engine()

    manifest = (target / "plugin.yaml").read_text(encoding="utf-8")
    pkg_version = _package_version()
    assert f"version: {pkg_version}" in manifest, (
        f"plugin.yaml says {manifest!r} but the package is {pkg_version}"
    )


def _package_version() -> str:
    """The repo's declared version (source of truth, not installed metadata)."""
    import re

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
    assert m, "could not read version from pyproject.toml"
    return m.group(1)


def test_user_engine_dir_honours_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cli.user_engine_dir() == Path(tmp_path) / "plugins" / "context-tuner"
    assert cli.user_engine_dir("other") == Path(tmp_path) / "plugins" / "other"
