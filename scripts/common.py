"""Shared helpers for the arch-build repository automation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "packages.lock"
CONFIG_FILE = ROOT / "packages.toml"

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT = "https://aur.archlinux.org/{name}.git"
USER_AGENT = "arch-build-automation/1.0"


# ---------------------------------------------------------------------------
# Logging / GitHub Actions integration
# ---------------------------------------------------------------------------

def _escape_annotation(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def log(message: str) -> None:
    print(message, flush=True)


def group(title: str) -> None:
    print(f"::group::{_escape_annotation(title)}", flush=True)


def endgroup() -> None:
    print("::endgroup::", flush=True)


def warning(message: str) -> None:
    print(f"::warning::{_escape_annotation(message)}", flush=True)


def error(message: str) -> None:
    print(f"::error::{_escape_annotation(message)}", flush=True)


def set_output(name: str, value: str) -> None:
    """Write a step output, using the delimiter form when needed."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        if "\n" in value:
            delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
            handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            handle.write(f"{name}={value}\n")


def add_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown.rstrip() + "\n")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.is_file():
        raise ValueError(f"missing configuration file: {CONFIG_FILE}")
    config = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    aur = config.get("aur", {})
    names = aur.get("packages", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("packages.toml: [aur].packages must be a list of strings")
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"packages.toml: duplicate AUR packages: {sorted(duplicates)}")

    return config


def aur_package_names(config: dict) -> list[str]:
    return list(config.get("aur", {}).get("packages", []))


def repo_settings(config: dict) -> tuple[str, str]:
    """Return (repository/database name, release tag).

    The name is used for the pacman database file (``<name>.db``) and must
    match the section name users put in their pacman.conf.
    """
    section = config.get("repo", {})
    name = section.get("name")
    if not name:
        slug = os.environ.get("GITHUB_REPOSITORY", "")
        name = slug.rsplit("/", 1)[-1] if slug else "arch-build"
    tag = section.get("tag") or "pacman"

    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+", name):
        raise ValueError(f"invalid repository name: {name!r}")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+", tag):
        raise ValueError(f"invalid release tag: {tag!r}")
    return name, tag


# ---------------------------------------------------------------------------
# PKGBUILD handling
# ---------------------------------------------------------------------------

_PKGBUILD_PROBE = r"""
set -e
source ./PKGBUILD
printf '%s\0' "${pkgver}" "${pkgrel}" "${epoch:-}" "${pkgbase:-}" "${pkgname[@]}"
"""


def read_pkgbuild(pkg_dir: Path) -> dict:
    """Source a PKGBUILD in bash and return its basic variables.

    Local PKGBUILDs live in this repository and are trusted; external AUR
    PKGBUILDs are never sourced by the update check (the AUR RPC supplies the
    version instead).
    """
    pkgbuild = pkg_dir / "PKGBUILD"
    if not pkgbuild.is_file():
        raise ValueError(f"missing PKGBUILD in {pkg_dir}")
    try:
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", _PKGBUILD_PROBE],
            cwd=pkg_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"timed out while evaluating {pkgbuild}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ValueError(f"failed to evaluate {pkgbuild}: {detail}")

    fields = proc.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) < 5:
        raise ValueError(f"unexpected PKGBUILD output for {pkgbuild}")

    pkgver, pkgrel, epoch, pkgbase = fields[:4]
    names = fields[4:]
    if not pkgver or not pkgrel:
        raise ValueError(f"{pkgbuild}: pkgver/pkgrel must not be empty")

    return {
        "pkgver": pkgver,
        "pkgrel": pkgrel,
        "epoch": epoch,
        "pkgbase": pkgbase or names[0],
        "pkgname": names,
        "version": full_version(pkgver, pkgrel, epoch),
    }


def full_version(pkgver: str, pkgrel: str, epoch: str = "") -> str:
    version = f"{pkgver}-{pkgrel}"
    if epoch and epoch != "0":
        version = f"{epoch}:{version}"
    return version


def local_package_dirs() -> list[Path]:
    base = ROOT / "local"
    if not base.is_dir():
        return []
    return sorted(
        path for path in base.iterdir() if (path / "PKGBUILD").is_file()
    )


# ---------------------------------------------------------------------------
# AUR lookups
# ---------------------------------------------------------------------------

def _http_json(url: str, attempts: int = 3) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"request failed: {url}: {last_error}")


def aur_info(names: list[str], chunk_size: int = 50) -> dict[str, dict]:
    """Fetch AUR RPC metadata for multiple packages at once."""
    results: dict[str, dict] = {}
    for offset in range(0, len(names), chunk_size):
        chunk = names[offset : offset + chunk_size]
        query = urllib.parse.urlencode([("arg[]", name) for name in chunk])
        payload = _http_json(f"{AUR_RPC}?{query}")
        for item in payload.get("results", []):
            results[item["Name"]] = item
    return results


def aur_head_commit(name: str, attempts: int = 3) -> str:
    """Resolve the current HEAD commit of an AUR git repository."""
    last_error = "no output"
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                ["git", "ls-remote", AUR_GIT.format(name=name), "HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            last_error = "timed out"
        else:
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    parts = line.split()
                    if parts:
                        return parts[0]
                last_error = "empty ls-remote output"
            else:
                last_error = proc.stderr.strip() or f"exit {proc.returncode}"
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(f"cannot resolve AUR commit for {name}: {last_error}")


# ---------------------------------------------------------------------------
# packages.lock
# ---------------------------------------------------------------------------

def read_lock() -> dict[str, dict]:
    """Return the locked external packages, keyed by package name."""
    if not LOCK_FILE.is_file():
        return {}
    data = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    entries: dict[str, dict] = {}
    for item in data.get("package", []):
        if not isinstance(item, dict) or "name" not in item:
            raise ValueError("packages.lock: every [[package]] needs a name")
        name = item["name"]
        entries[name] = {
            "name": name,
            "source": item.get("source", "aur"),
            "version": item.get("version", ""),
            "commit": item.get("commit", ""),
        }
    return entries


def render_lock(entries: dict[str, dict]) -> str:
    lines = [
        "# Generated by scripts/check_update.py -- do not edit manually.",
        "# Pins external (AUR) packages so that builds are reproducible.",
        "",
        "version = 1",
    ]
    for name in sorted(entries):
        entry = entries[name]
        lines.extend(
            [
                "",
                "[[package]]",
                f"name = {json.dumps(name, ensure_ascii=False)}",
                f"source = {json.dumps(entry.get('source', 'aur'), ensure_ascii=False)}",
                f"version = {json.dumps(entry.get('version', ''), ensure_ascii=False)}",
                f"commit = {json.dumps(entry.get('commit', ''), ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines) + "\n"
