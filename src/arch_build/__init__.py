"""Automated custom pacman repository tooling."""

from __future__ import annotations

import subprocess
from pathlib import Path

USER_AGENT = "arch-build-automation/1.0"


def find_root() -> Path:
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (start, *start.parents):
            if (candidate / "packages.toml").is_file():
                return candidate
    return Path.cwd()


ROOT = find_root()
CONFIG_FILE = ROOT / "packages.toml"
LOCK_FILE = ROOT / "packages.lock"


def run_cmd(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(str(part) for part in cmd)
    print(f"+ {printable}", flush=True)
    proc = subprocess.run([str(part) for part in cmd], cwd=cwd, env=env, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {printable}")
    return proc
