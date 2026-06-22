from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import json
from .engine import default_store_path
from .recovery import RecoveryPointerStore, SCHEMA_VERSION
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


def install_engine(hermes_repo: str | None = None, set_config: bool = False, *, dry_run: bool = False) -> Path:
    repo = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    target = repo / "plugins" / "context_engine" / "context-tuner"
    if dry_run:
        return target
    target.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).resolve().parent / "hermes_engine"
    for name in ENGINE_FILES:
        destination = target / name
        if destination.exists() and destination.read_bytes() == (source / name).read_bytes():
            continue
        if destination.exists():
            shutil.copy2(destination, destination.with_suffix(destination.suffix + ".bak"))
        with tempfile.NamedTemporaryFile(dir=target, delete=False) as tmp:
            tmp.write((source / name).read_bytes()); temp_path = Path(tmp.name)
        temp_path.replace(destination)

    if set_config:
        subprocess.run(["hermes", "config", "set", "context.engine", "context-tuner"], check=True)
    return target

def check_engine(hermes_repo: str | None = None) -> tuple[bool, Path]:
    repo = Path(hermes_repo) if hermes_repo else _repo_root_from_hermes()
    target = repo / "plugins" / "context_engine" / "context-tuner"
    source = Path(__file__).resolve().parent / "hermes_engine"
    return all((target / n).exists() and (target / n).read_bytes() == (source / n).read_bytes() for n in ENGINE_FILES), target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Context Tuner utilities")
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install-hermes", help="Install/update the Hermes context-engine shim")
    p_install.add_argument("--hermes-repo", default=None)
    p_install.add_argument("--set-config", action="store_true", help="Set hermes config context.engine=context-tuner")
    p_install.add_argument("--dry-run", action="store_true")
    p_check = sub.add_parser("check", help="Check whether the current shim matches this package")
    p_check.add_argument("--hermes-repo", default=None)
    p_doctor = sub.add_parser("doctor", help="Report recovery/audit health")
    p_doctor.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)
    if args.cmd == "install-hermes":
        target = install_engine(args.hermes_repo, set_config=args.set_config, dry_run=args.dry_run)
        print(f"{'would install' if args.dry_run else 'installed'} context-tuner engine shim to {target}")
        return 0
    if args.cmd == "check":
        ok, target = check_engine(args.hermes_repo); print(f"{'ok' if ok else 'outdated or missing'}: {target}"); return 0 if ok else 1
    if args.cmd == "doctor":
        report = {"ok": True, "schema_version": SCHEMA_VERSION, "recovery_store": str(default_store_path()), "pending_records": 0, "audit_failures": 0}
        try:
            report["pending_records"] = sum(e["status"] == "pending" for e in RecoveryPointerStore(default_store_path()).latest_events(1000))
        except Exception as exc:
            report.update(ok=False, audit_failures=1, error=f"{type(exc).__name__}: {exc}")
        print(json.dumps(report, sort_keys=True) if args.as_json else "\n".join(f"{k}: {v}" for k,v in report.items()))
        return 0 if report["ok"] else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
