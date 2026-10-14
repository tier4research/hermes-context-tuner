from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE_FILES = ("__init__.py", "plugin.yaml")


def _repo_root_from_hermes() -> Path:
    try:
        out = subprocess.check_output(["hermes", "--version"], text=True, stderr=subprocess.STDOUT)
        for line in out.splitlines():
            if line.startswith("Project:"):
                return Path(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"


def _hermes_home() -> Path:
    """Resolve the Hermes home dir the way Hermes itself does."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    win = Path.home() / "AppData" / "Local" / "hermes"
    return win if win.is_dir() else Path.home() / ".hermes"


def user_engine_dir(name: str = "context-tuner") -> Path:
    """The user-plugin install target: ``$HERMES_HOME/plugins/<name>``.

    Hermes resolves engines bundled-first and then from this user dir -- but a
    *bundled* directory shadows the user dir even when it is empty, and the
    bundled tree is runtime-owned: a Hermes upgrade can empty or replace it,
    which is how an installed shim silently disappears. This directory is ours
    and survives upgrades, so it is the default install target.
    """
    return _hermes_home() / "plugins" / name


def bundled_engine_dir(hermes_repo: str | Path | None = None) -> Path:
    """Where a Hermes checkout keeps the *bundled* engine shim.

    A bundled directory wins over the user dir merely by existing, so this is
    the path that must be checked before any install is reported as successful.
    """
    root = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    return Path(root) / "plugins" / "context_engine" / "context-tuner"


_LOADER_PROBE = r"""
import importlib.util as _iu
import json, os, sys

_ha = os.path.abspath(sys.argv[1])
if _ha not in sys.path:
    sys.path.insert(0, _ha)
out = {"marker": "__CT_PROBE__", "resolved": None, "discover": None, "load": None}
_mod = None


def _err(key, exc):
    out[key] = type(exc).__name__ + ": " + str(exc)[:200]


try:
    _spec = _iu.spec_from_file_location(
        "_ct_ctx_engine", os.path.join(_ha, "plugins", "context_engine", "__init__.py")
    )
    _mod = _iu.module_from_spec(_spec)
    sys.modules["_ct_ctx_engine"] = _mod
    _spec.loader.exec_module(_mod)
    out["loader_imported"] = True
except Exception as e:
    out["loader_imported"] = False
    _err("import_error", e)

if _mod is not None:
    try:
        _r = _mod.find_engine_dir("context-tuner")
        out["resolved"] = os.path.abspath(str(_r)) if _r is not None else None
    except Exception as e:
        _err("find_error", e)
    try:
        out["discover"] = [str(_d) for _d in _mod.discover_context_engines()]
    except Exception as e:
        _err("discover_error", e)
    # Resolving a directory is not loading an engine. Exercise the host's own
    # activation path, so an unloadable shim cannot verify as installed.
    try:
        _load = getattr(_mod, "load_context_engine", None)
        if callable(_load):
            _engine = _load("context-tuner")
            out["load"] = type(_engine).__name__ if _engine is not None else "None"
        else:
            out["load_unavailable"] = "host exposes no load_context_engine()"
    except Exception as e:
        _err("load_error", e)

print("__CT_PROBE__" + json.dumps(out))
"""


def verify_install(
    target: Path,
    hermes_repo: str | Path | None = None,
    strict: bool | None = None,
) -> dict:
    """Verify that Hermes can *load* the shim, not merely that it exists.

    Copying files is not installing. Hermes resolves engines bundled-first, so
    a stale, partial or empty bundled directory silently shadows the user
    install while the CLI still prints success -- the September outage exactly
    (audit CRITICAL-1). Locating the directory is not loading the engine
    either: the second CRITICAL was that this function called a resolved path
    "verified" without ever exercising the host's activation path
    (``load_context_engine``), so a shim that raises on import or registers no
    engine verified as installed.

    ``strict`` defaults to True only when installing into the live Hermes home,
    where the plugin must load. For an explicit checkout (``--hermes-repo``,
    i.e. a build/CI target) the probe's findings are reported as notes instead:
    the tree under test is the caller's choice, and failing a build because the
    loader cannot be probed there would break the very job that checks the host
    contract. An unprovable install in the live home is still a failure.

    Distinct outcomes, deliberately:
      * bundled shadow, divergent resolution, unloadable engine -> failure;
      * no Hermes runtime at all in the tree -> unverified, not failed: the
        copy is the whole of the requested operation (audit HIGH: a pre-Hermes
        or pre-venv install must not become a fatal installer result).

    The probe runs the root's own venv interpreter with this process's
    environment, so it is the closest available equivalent of a real Hermes
    start-up; it is still a point-in-time result and a later upgrade can
    invalidate it.
    """
    if strict is None:
        strict = hermes_repo is None
    target = Path(target).resolve()
    root = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    report: dict = {
        "ok": False,
        "strict": strict,
        "target": str(target),
        "resolved": None,
        "shadow": None,
        "load": None,
        "reasons": [],
        "notes": [],
    }
    findings = report["reasons"] if strict else report["notes"]

    # A bundled directory wins over the user dir merely by existing, so it is
    # checked first -- and an *uninspectable* one is not reported as clean.
    bundled: Path | None = None
    try:
        candidate = bundled_engine_dir(hermes_repo).resolve()
        bundled = candidate if candidate.is_dir() else None
    except Exception as exc:
        report["reasons"].append(
            f"could not inspect the bundled engine dir: {type(exc).__name__}: {exc}"
        )
    if bundled is not None and bundled != target:
        report["shadow"] = str(bundled)
        report["reasons"].append(
            f"bundled shadow at {bundled} -- Hermes resolves it first, so "
            f"{target} will not load"
        )

    loader_py = root / "plugins" / "context_engine" / "__init__.py"
    if not loader_py.is_file():
        report["notes"].append(
            f"not verified: no Hermes context_engine loader at {loader_py}"
        )
        report["ok"] = not report["reasons"]
        return report

    python = root / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = root / "venv" / "bin" / "python"
    if not python.is_file():
        python = Path(sys.executable)
    try:
        proc = subprocess.run(
            [str(python), "-c", _LOADER_PROBE, str(root)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(root),
            env={**os.environ, "HERMES_HOME": str(_hermes_home())},
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or "").strip().splitlines() or [""])[-1][:200]
            findings.append(
                f"the loader probe itself failed (rc={proc.returncode}): {tail}"
            )
        else:
            framed = [
                ln for ln in (proc.stdout or "").splitlines()
                if ln.startswith("__CT_PROBE__")
            ]
            if not framed:
                findings.append("the loader probe produced no framed result line")
            else:
                data = json.loads(framed[-1][len("__CT_PROBE__"):])
                resolved = data.get("resolved")
                report["resolved"] = resolved
                report["discover"] = data.get("discover")
                report["load"] = data.get("load")
                if data.get("import_error"):
                    findings.append(
                        f"the context_engine loader cannot be imported: {data['import_error']}"
                    )
                if data.get("find_error"):
                    findings.append(f"find_engine_dir() raised: {data['find_error']}")
                if not resolved:
                    findings.append(
                        "loader reported no engine path (find_engine_dir -> None)"
                    )
                else:
                    try:
                        if Path(resolved).resolve() != target:
                            findings.append(
                                f"loader resolves {resolved}, not the installed target {target}"
                            )
                    except Exception as exc:
                        findings.append(
                            f"could not compare resolved path {resolved!r}: {type(exc).__name__}"
                        )
                if data.get("discover_error"):
                    findings.append(
                        f"discover_context_engines() raised: {data['discover_error']}"
                    )
                elif not any(
                    "context-tuner" in str(entry)
                    for entry in (data.get("discover") or [])
                ):
                    findings.append(
                        "the engine is not listed by discover_context_engines()"
                    )
                # Loadability: the check whose absence made CRITICAL-1 reachable.
                if data.get("load_error"):
                    findings.append(f"the engine failed to load: {data['load_error']}")
                elif data.get("load_unavailable"):
                    findings.append(
                        f"engine load was not exercised: {data['load_unavailable']}"
                    )
                elif not data.get("load") or data.get("load") == "None":
                    findings.append("load_context_engine() returned no engine")
    except Exception as exc:
        findings.append(f"could not probe the loader: {type(exc).__name__}: {exc}")

    report["ok"] = not report["reasons"]
    return report


def install_engine(hermes_repo: str | None = None) -> Path:
    """Install or update the context-engine shim.

    Defaults to the user-plugin dir (upgrade-resilient). Pass ``hermes_repo``
    to install into a Hermes checkout's bundled tree instead -- note that a
    bundled copy takes precedence over the user dir at load time.

    Copy only: activation (``apply_config``) and verification
    (``verify_install``) are separate steps with their own outcomes, so a
    config failure can no longer be swallowed by a successful copy.
    """
    if hermes_repo:
        target = Path(hermes_repo) / "plugins" / "context_engine" / "context-tuner"
    else:
        target = user_engine_dir()
    target.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).resolve().parent / "hermes_engine"
    for name in ENGINE_FILES:
        shutil.copy2(source / name, target / name)
    return target


def apply_config() -> tuple[int, str]:
    """Point Hermes at this engine, reporting the outcome rather than assuming it.

    The previous code ran ``hermes config set`` from inside ``install_engine()``
    with ``check=False`` and then let the caller print a verified install: if
    hermes was missing from PATH, the config was locked, or the key was
    rejected, the engine was activated in name only (audit HIGH: swallowed
    set-config failure).
    """
    try:
        proc = subprocess.run(
            ["hermes", "config", "set", "context.engine", "context-tuner"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
    return proc.returncode, (detail[-1][:200] if detail else "")


def _ledger_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(Path.home(), ".hermes")
    return Path(home) / "context_tuner" / "checkpoints.sqlite"


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    from hermes_context_tuner.checkpoints import CheckpointLedger

    try:
        if args.file:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        else:
            data = json.loads(sys.stdin.read())
        ledger = CheckpointLedger(_ledger_path())
        checkpoint_id = ledger.write_checkpoint(data)
        print(json.dumps({"ok": True, "checkpoint_id": checkpoint_id}))
        return 0
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"checkpoint failed: {exc}"}))
        return 1


def _cmd_resume(args: argparse.Namespace) -> int:
    from hermes_context_tuner.checkpoints import CheckpointLedger

    try:
        ledger = CheckpointLedger(_ledger_path())
        state = ledger.latest_state(
            args.project, conversation_id=args.conversation if args.conversation else None
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"resume failed: {exc}"}))
        return 1
    if state is None:
        print(json.dumps({"ok": True, "checkpoint": None, "decisions": [], "tasks": {}}))
        return 0
    packet = {
        "ok": True,
        "checkpoint": state["checkpoint"],
        "decisions": [
            {"content": d["content"], "supersedes": d["supersedes"]}
            for d in state["decisions"]
        ],
        "tasks": {
            t: {"status": v["status"], "history_tail": v["history"][-5:]}
            for t, v in state["tasks"].items()
        },
    }
    print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Context Tuner utilities")
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install-hermes", help="Install/update the Hermes context-engine shim")
    p_install.add_argument(
        "--hermes-repo",
        default=None,
        help="Install into a Hermes checkout's bundled tree instead of the "
             "upgrade-resilient user-plugin dir ($HERMES_HOME/plugins/).",
    )
    p_install.add_argument("--set-config", action="store_true", help="Set hermes config context.engine=context-tuner")

    p_cp = sub.add_parser("checkpoint", help="Write an explicit checkpoint from a JSON file (or stdin)")
    p_cp.add_argument("--file", default=None, help="Path to checkpoint JSON payload")
    p_cp.set_defaults(func=_cmd_checkpoint)

    p_rs = sub.add_parser("resume", help="Print the bounded resume packet for a project")
    p_rs.add_argument("--project", required=True)
    p_rs.add_argument("--conversation", default=None)
    p_rs.set_defaults(func=_cmd_resume)

    args = parser.parse_args(argv)
    if args.cmd == "install-hermes":
        target = install_engine(args.hermes_repo)
        report = verify_install(target, args.hermes_repo)
        if args.set_config:
            rc, detail = apply_config()
            if rc != 0:
                suffix = f": {detail}" if detail else ""
                message = (
                    "`hermes config set context.engine context-tuner` failed "
                    f"(rc={rc}){suffix}"
                )
                bucket = report["reasons"] if report["strict"] else report["notes"]
                bucket.append(message)
                report["ok"] = not report["reasons"]
        if report["ok"]:
            if report["load"]:
                print(f"installed context-tuner engine shim to {target}")
                print(
                    f"verified: the loader resolves {report['resolved']} and "
                    f"load_context_engine() returned a {report['load']}"
                )
            else:
                # Nothing to load in this tree (no Hermes runtime): the copy is
                # the whole of the operation, so do not claim verification.
                print(f"installed context-tuner engine shim to {target} (not verified)")
            for note in report["notes"]:
                print(f"  note: {note}")
            return 0
        # Fail closed: an unverified install is not an install. Reporting
        # success here is what let the shim disappear silently for 19 days.
        print(f"copied context-tuner engine shim to {target}")
        print("INSTALL VERIFICATION FAILED -- the engine will not load:")
        for reason in report["reasons"]:
            print(f"  - {reason}")
        for note in report["notes"]:
            print(f"  note: {note}")
        return 1
    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
