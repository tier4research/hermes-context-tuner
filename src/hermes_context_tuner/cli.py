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


_LOADER_PROBE = (
    "import importlib.util as _iu, json, sys\n"
    "_ha = sys.argv[1]\n"
    "_spec = _iu.spec_from_file_location('_ct_ctx_engine', _ha + '/plugins/context_engine/__init__.py')\n"
    "_m = _iu.module_from_spec(_spec)\n"
    "_spec.loader.exec_module(_m)\n"
    "_out = {'resolved': None, 'discover': []}\n"
    "try:\n"
    "    _r = _m.find_engine_dir('context-tuner')\n"
    "    _out['resolved'] = str(_r) if _r is not None else None\n"
    "except Exception as _e:\n"
    "    _out['find_error'] = type(_e).__name__\n"
    "try:\n"
    "    _out['discover'] = [repr(_d) for _d in _m.discover_context_engines()]\n"
    "except Exception as _e:\n"
    "    _out['discover_error'] = type(_e).__name__\n"
    "print(json.dumps(_out))\n"
)


def verify_install(
    target: Path,
    hermes_repo: str | Path | None = None,
    strict: bool | None = None,
) -> dict:
    """Verify that the *loader* resolves the shim that was just installed.

    Copying files is not installing. Hermes resolves engines bundled-first, so
    a stale, partial or empty bundled directory silently shadows the user
    install while the CLI still prints success -- the September outage exactly
    (audit CRITICAL-1: a false-success deployment path).

    ``strict`` defaults to True only when installing into the live Hermes home,
    where the plugin must load. For an explicit checkout (``--hermes-repo``,
    i.e. a build/CI target) the probe's findings are reported as notes instead:
    the tree under test is the caller's choice, and failing a build because the
    loader cannot be probed there would break the very job that checks the
    host contract. An unprovable install in the live home is still a failure.
    """
    if strict is None:
        strict = hermes_repo is None
    target = Path(target).resolve()
    report: dict = {
        "ok": False,
        "strict": strict,
        "target": str(target),
        "resolved": None,
        "shadow": None,
        "reasons": [],
        "notes": [],
    }
    findings = report["reasons"] if strict else report["notes"]

    bundled = bundled_engine_dir(hermes_repo)
    shadow_exists = False
    try:
        shadow_exists = bundled.is_dir()
    except Exception:
        shadow_exists = False
    if shadow_exists and bundled.resolve() != target:
        report["shadow"] = str(bundled)
        report["reasons"].append(
            f"bundled shadow at {bundled} -- Hermes resolves it first, so "
            f"{target} will not load"
        )

    root = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    python = Path(root) / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    try:
        proc = subprocess.run(
            [str(python), "-c", _LOADER_PROBE, str(root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        data = json.loads(lines[-1]) if lines else {}
        resolved = data.get("resolved")
        report["resolved"] = resolved
        report["discover"] = data.get("discover")
        if not resolved:
            findings.append(
                "loader reported no engine path "
                f"(find_engine_dir -> None; {data.get('find_error') or 'no error reported'})"
            )
        elif Path(resolved).resolve() != target:
            findings.append(f"loader resolves {resolved}, not the installed target")
    except Exception as exc:
        findings.append(f"could not probe the loader: {type(exc).__name__}: {exc}")

    report["ok"] = not report["reasons"]
    return report


def install_engine(hermes_repo: str | None = None, set_config: bool = False) -> Path:
    """Install or update the context-engine shim.

    Defaults to the user-plugin dir (upgrade-resilient). Pass ``hermes_repo``
    to install into a Hermes checkout's bundled tree instead -- note that a
    bundled copy takes precedence over the user dir at load time.
    """
    if hermes_repo:
        target = Path(hermes_repo) / "plugins" / "context_engine" / "context-tuner"
    else:
        target = user_engine_dir()
    target.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).resolve().parent / "hermes_engine"
    for name in ENGINE_FILES:
        shutil.copy2(source / name, target / name)

    if set_config:
        subprocess.run(["hermes", "config", "set", "context.engine", "context-tuner"], check=False)
    return target


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
        target = install_engine(args.hermes_repo, set_config=args.set_config)
        report = verify_install(target, args.hermes_repo)
        if report["ok"]:
            if report["resolved"]:
                print(f"installed context-tuner engine shim to {target}")
                print(f"verified: the loader resolves {report['resolved']}")
            else:
                # An explicit --hermes-repo install into a tree with no engine
                # bundle: nothing to resolve, so do not claim verification.
                print(f"installed context-tuner engine shim to {target} (not probed)")
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
