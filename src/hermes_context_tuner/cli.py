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


def install_engine(hermes_repo: str | None = None, set_config: bool = False) -> Path:
    repo = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    target = repo / "plugins" / "context_engine" / "context-tuner"
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
    p_install.add_argument("--hermes-repo", default=None)
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
        print(f"installed context-tuner engine shim to {target}")
        return 0
    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
