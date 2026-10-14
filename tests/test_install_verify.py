"""Installer verification must prove the engine *loads*, not that files exist.

Audit CRITICAL-1 (0.3.1): ``install-hermes`` reported success from what was
essentially a copy check, so a shim Hermes would never load -- shadowed by a
stale bundled dir -- verified as installed. Audit CRITICAL-2 (0.3.2): the fix
asked whether the loader could *resolve a directory* and called that verified,
which still passed for an empty shim or one that raises on import.

These tests drive the real probe against a fake tree that models the host's
resolution rules (bundled-first, then ``$HERMES_HOME/plugins``) and the host's
activation path (import the shim, require a usable engine), then assert the
exit-code contract of ``main()`` -- because the exit code, not the printed
sentence, is what a caller acts on.
"""
from __future__ import annotations

from pathlib import Path

from hermes_context_tuner import cli as cli_mod
from hermes_context_tuner.cli import (
    bundled_engine_dir,
    install_engine,
    main,
    user_engine_dir,
    verify_install,
)

SHIM_OK = "def register(ctx):\n    ctx.register_context_engine(object())\n"
SHIM_EMPTY = ""
SHIM_RAISES = 'raise ImportError("engine exploded on import")\n'

# Mirrors hermes-agent/plugins/context_engine/__init__.py: same bundled-first
# resolution, same user-dir fallback, same requirement that loading the shim
# yields a usable engine (the host extracts a ContextEngine or calls register()).
LOADER = '''
import importlib.util as _iu
import os
from pathlib import Path

_BUNDLED = Path(__file__).resolve().parent


def _user_dir():
    home = os.environ.get("HERMES_HOME")
    return Path(home) / "plugins" if home else None


def find_engine_dir(name):
    bundled = _BUNDLED / name
    if bundled.is_dir():
        return bundled
    user = _user_dir()
    if user and (user / name).is_dir():
        return user / name
    return None


def discover_context_engines():
    found = []
    for base in (_BUNDLED, _user_dir()):
        if base and base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir():
                    found.append((child.name, str(child), True))
    return found


def load_context_engine(name):
    engine_dir = find_engine_dir(name)
    if engine_dir is None:
        return None
    spec = _iu.spec_from_file_location("_probe_engine", engine_dir / "__init__.py")
    module = _iu.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        raise RuntimeError("engine module exposes no usable engine")

    class _Collector:
        def __init__(self):
            self.engine = None

        def register_context_engine(self, engine):
            self.engine = engine

        def __getattr__(self, item):
            return lambda *a, **k: None

    collector = _Collector()
    register(collector)
    if collector.engine is None:
        raise RuntimeError("engine module registered no engine")
    return collector.engine
'''

LOADER_HIDES_ENGINE = LOADER.replace(
    "                    found.append((child.name, str(child), True))",
    "                    found.append((child.name, str(child), True))\n"
    "    return []  # discovery blind to the engine that resolved",
)

LOADER_SILENT = "import sys\nsys.exit(0)\n"


def _write_loader(root: Path, source: str = LOADER) -> None:
    engine_dir = root / "plugins" / "context_engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "__init__.py").write_text(source, encoding="utf-8")


def _write_shim(shim_dir: Path, source: str = SHIM_OK) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "__init__.py").write_text(source, encoding="utf-8")
    (shim_dir / "plugin.yaml").write_text("name: context-tuner\n", encoding="utf-8")
    return shim_dir


def _fake_hermes_root(tmp_path: Path, loader_source: str = LOADER) -> Path:
    root = tmp_path / "hermes-agent"
    _write_loader(root, loader_source)
    return root


def _scene(tmp_path, monkeypatch, shim_source=SHIM_OK, loader_source=LOADER):
    """A Hermes-shaped tree plus a resolved user-plugin install target."""
    home = tmp_path / "home"
    root = _fake_hermes_root(tmp_path, loader_source)
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = _write_shim(home / "plugins" / "context-tuner", shim_source)
    return home, root, target


def _install_user_shim(root: Path, source: str = SHIM_OK) -> Path:
    return _write_shim(root / "plugins" / "context-tuner", source)


def test_clean_tree_verifies_against_the_loader(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = _fake_hermes_root(tmp_path)
    target = _write_shim(home / "plugins" / "context-tuner")

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is True, report["reasons"]
    assert Path(report["resolved"]).resolve() == target.resolve()
    assert report["load"] == "object"


def test_empty_shim_does_not_verify(tmp_path, monkeypatch):
    """CRITICAL-2 regression: a directory that resolves is not an engine."""
    _home, root, target = _scene(tmp_path, monkeypatch, shim_source=SHIM_EMPTY)

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert any("no usable engine" in r for r in report["reasons"]), report["reasons"]


def test_engine_that_raises_on_import_does_not_verify(tmp_path, monkeypatch):
    _home, root, target = _scene(tmp_path, monkeypatch, shim_source=SHIM_RAISES)

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert any("exploded on import" in r for r in report["reasons"]), report["reasons"]


def test_empty_bundled_shadow_fails_verification(tmp_path, monkeypatch):
    """The exact September outage: an empty bundled dir shadows the user install."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = _fake_hermes_root(tmp_path)
    target = _write_shim(home / "plugins" / "context-tuner")
    shadow = bundled_engine_dir(str(root))
    shadow.mkdir(parents=True, exist_ok=True)  # empty on purpose
    assert list(shadow.iterdir()) == []

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert report["shadow"] == str(shadow)
    assert any("bundled shadow" in r for r in report["reasons"])
    # The loader must also be shown to resolve the wrong path -- not merely
    # that a shadow directory happens to exist on disk.
    assert any("loader resolves" in r for r in report["reasons"]), report["reasons"]


def test_engine_missing_from_discovery_fails_verification(tmp_path, monkeypatch):
    _home, root, target = _scene(tmp_path, monkeypatch, loader_source=LOADER_HIDES_ENGINE)

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert any("not listed" in r for r in report["reasons"]), report["reasons"]


def test_probe_without_a_framed_result_fails_verification(tmp_path, monkeypatch):
    _home, root, target = _scene(tmp_path, monkeypatch, loader_source=LOADER_SILENT)

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is False
    assert any("no framed result" in r for r in report["reasons"]), report["reasons"]


def test_tree_without_a_hermes_runtime_is_unverified_not_failed(tmp_path, monkeypatch):
    """A pre-Hermes install: copying the shim is the whole operation.

    The loader probe cannot run in a tree with no Hermes runtime, but that is a
    fact about the tree -- not a defect in the install (audit HIGH: an install
    before Hermes exists must not become a fatal result). It must never be
    reported as *verified*, though.
    """
    home = tmp_path / "home"
    root = tmp_path / "empty-repo"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = _write_shim(home / "plugins" / "context-tuner")

    report = verify_install(target, hermes_repo=str(root), strict=True)

    assert report["ok"] is True, report["reasons"]
    assert report["resolved"] is None
    assert any("not verified" in n for n in report["notes"]), report["notes"]


def test_checkout_install_keeps_findings_advisory(tmp_path, monkeypatch):
    """An explicit --hermes-repo is a build/CI target: the probe's findings are
    notes, not failures. Failing here would break the job whose whole job is to
    check the host contract (regression caught before release)."""
    _home, root, target = _scene(tmp_path, monkeypatch, shim_source=SHIM_EMPTY)

    report = verify_install(target, hermes_repo=str(root))

    assert report["strict"] is False
    assert report["ok"] is True, report["reasons"]
    assert any("no usable engine" in n for n in report["notes"]), report["notes"]


def test_shadow_fails_even_for_an_explicit_checkout(tmp_path, monkeypatch):
    """A bundled shadow is never acceptable, whichever tree you install into."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = _fake_hermes_root(tmp_path)
    target = _write_shim(home / "plugins" / "context-tuner")
    bundled_engine_dir(str(root)).mkdir(parents=True, exist_ok=True)

    report = verify_install(target, hermes_repo=str(root))

    assert report["ok"] is False
    assert report["shadow"]


def test_main_returns_1_when_verification_fails(tmp_path, monkeypatch, capsys):
    """The exit code, not the sentence, is what a caller acts on."""
    home = tmp_path / "home"
    root = _fake_hermes_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli_mod, "_repo_root_from_hermes", lambda: root)
    monkeypatch.setattr(cli_mod, "_hermes_home", lambda: home)
    monkeypatch.setattr(
        cli_mod, "install_engine",
        lambda *a, **k: _write_shim(home / "plugins" / "context-tuner", SHIM_EMPTY),
    )

    rc = main(["install-hermes"])

    assert rc == 1
    assert "INSTALL VERIFICATION FAILED" in capsys.readouterr().out


def test_main_claims_verified_only_when_the_engine_loads(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = _fake_hermes_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli_mod, "_repo_root_from_hermes", lambda: root)
    monkeypatch.setattr(cli_mod, "_hermes_home", lambda: home)

    rc = main(["install-hermes"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "verified: the loader resolves" in out
    # The real shim loads as its own engine class, not as a bare object.
    assert "load_context_engine() returned a ContextTunerEngine" in out


def test_main_fails_when_requested_config_change_does_not_apply(tmp_path, monkeypatch, capsys):
    """A verified copy plus a silent config failure is not an activated engine."""
    home = tmp_path / "home"
    root = _fake_hermes_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli_mod, "_repo_root_from_hermes", lambda: root)
    monkeypatch.setattr(cli_mod, "_hermes_home", lambda: home)
    monkeypatch.setattr(cli_mod, "apply_config", lambda: (1, "config is locked"))

    rc = main(["install-hermes", "--set-config"])

    assert rc == 1
    assert "config is locked" in capsys.readouterr().out


def test_install_engine_writes_engine_files_into_user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    target = install_engine()
    assert target == user_engine_dir()
    assert (target / "__init__.py").is_file()
    assert (target / "plugin.yaml").is_file()
