from __future__ import annotations

import argparse
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Context Tuner utilities")
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install-hermes", help="Install/update the Hermes context-engine shim")
    p_install.add_argument("--hermes-repo", default=None)
    p_install.add_argument("--set-config", action="store_true", help="Set hermes config context.engine=context-tuner")

    args = parser.parse_args(argv)
    if args.cmd == "install-hermes":
        target = install_engine(args.hermes_repo, set_config=args.set_config)
        print(f"installed context-tuner engine shim to {target}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
