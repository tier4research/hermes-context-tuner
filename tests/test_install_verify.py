"""Installer verification tests (audit CRITICAL-1).

Copying the shim is not installing it. Hermes resolves engines bundled-first,
so a stale or empty bundled directory shadows the user install while the CLI
still reports success. These tests reproduce that tree shape with a fake Hermes
root -- including a loader that mirrors Hermes' bundled-first preference -- and
require the installer verification to fail loudly.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from hermes_context_tuner.cli import (
    bundled_engine_dir,
    install_engine,
    user_engine_dir,
    verify_install,
)

# Mirrors Hermes' real resolution rule: a bundled directory wins by existing,
# even when it is empty. If no bundled dir exists, the user dir is used.
FAKE_LOADER = textwrap.dedent(
    '''
    from pathlib import Path


    def _root() -> Path:
        return Path(__file__).resolve().parents[2]


    def find_engine_dir(name: str):
        bundled = _root() / "plugins" / "context_engine" / name
        if bundled.is_dir():
            return bundled
        user = _root() / "plugins" / name
        return user if user.is_dir() else None


    def discover_context_engines():
        found = find_engine_dir("context-tuner")
        return [] if found is None else [("context-tuner", "fake", True)]
    '''
)


def _fake_hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    (root / "plugins" / "context_engine").mkdir(parents=True)
    (root / "plugins" / "context_engine" / "__init__.py").write_text(
        FAKE_LOADER, encoding="utf-8"
    )
    return root


def _install_user_shim(root: Path) -> Path:
    target = root / "plugins" / "context-tuner"
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").write_text("", encoding="utf-8")
    (target / "plugin.yaml").write_text("name: context-tuner\n", encoding="utf-8")
    return target


def test_clean_tree_verifies_against_the_loader(tmp_path):
    root = _fake_hermes_root(tmp_path)
    target = _install_user_shim(root)

    report = verify_install(target, hermes_repo=str(root))

    assert report["ok"] is True, report["reasons"]
    assert Path(report["resolved"]).resolve() == target.resolve()


def test_empty_bundled_shadow_fails_verification(tmp_path):
    """The exact September outage: an empty bundled dir shadows the user install."""
    root = _fake_hermes_root(tmp_path)
    target = _install_user_shim(root)

    shadow = bundled_engine_dir(str(root))
    shadow.mkdir(parents=True, exist_ok=True)  # empty on purpose
    assert list(shadow.iterdir()) == []

    # strict: this is the live-home shape, where a shadow is fatal
    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert report["shadow"] == str(shadow)
    assert any("bundled shadow" in r for r in report["reasons"])
    # The loader must also be shown to resolve the wrong path -- not merely
    # that a shadow directory happens to exist on disk.
    assert any("loader resolves" in r for r in report["reasons"]), report["reasons"]


def test_unprobeable_loader_fails_the_live_install(tmp_path):
    """Fail closed: in the live home, if the loader cannot be probed, that is
    not success -- an unverified install is not an install."""
    root = tmp_path / "hermes-agent"
    (root / "plugins").mkdir(parents=True)  # no plugins/context_engine at all
    target = root / "plugins" / "context-tuner"
    target.mkdir(parents=True)

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert report["reasons"]


def test_checkout_install_reports_unprobeable_loader_as_a_note(tmp_path):
    """An explicit --hermes-repo is a build/CI target: the probe's findings are
    notes, not failures. Failing here would break the CI job whose whole job is
    to check the host contract (regression caught before release)."""
    root = tmp_path / "hermes-agent"
    (root / "plugins").mkdir(parents=True)
    target = root / "plugins" / "context-tuner"
    target.mkdir(parents=True)

    report = verify_install(target, hermes_repo=str(root))  # strict defaults False

    assert report["strict"] is False
    assert report["ok"] is True, report["reasons"]
    assert report["notes"]


def test_shadow_fails_even_for_an_explicit_checkout(tmp_path):
    """A bundled shadow is never acceptable, whichever tree you install into."""
    root = _fake_hermes_root(tmp_path)
    target = _install_user_shim(root)
    bundled_engine_dir(str(root)).mkdir(parents=True, exist_ok=True)

    report = verify_install(target, hermes_repo=str(root))

    assert report["ok"] is False
    assert report["shadow"]


def test_install_engine_writes_engine_files_into_user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    target = install_engine()
    assert target == user_engine_dir()
    assert (target / "__init__.py").is_file()
    assert (target / "plugin.yaml").is_file()
