"""GitHub Release asset publishing and database git commits."""

from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

from arch_build import ROOT, run_cmd
from arch_build.gha import log, warning
from arch_build.pacman import PACKAGE_ASSET_RE, BuiltPackage


def ensure_safe_directory(root: Path = ROOT) -> None:
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", str(root)],
        capture_output=True,
        check=False,
    )


def github_slug(root: Path = ROOT) -> str | None:
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    ensure_safe_directory(root)
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", proc.stdout.strip())
    return match.group(1) if match else None


def _gh(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=capture, text=True, check=False)


def _ensure_release(slug: str, tag: str) -> None:
    if _gh("release", "view", tag, "--repo", slug, capture=True).returncode == 0:
        return
    proc = _gh(
        "release",
        "create",
        tag,
        "--repo",
        slug,
        "--target",
        "main",
        "--title",
        "Package repository",
        "--notes",
        "Automated pacman repository assets. Do not delete; pacman clients use this URL.",
        "--latest=false",
        capture=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cannot create release {tag}: {proc.stderr.strip()}")


def _prune_old_assets(slug: str, tag: str, referenced: set[str], repo_name: str) -> None:
    proc = _gh(
        "release",
        "view",
        tag,
        "--repo",
        slug,
        "--json",
        "assets",
        "--jq",
        ".assets[].name",
        capture=True,
    )
    if proc.returncode != 0:
        warning(f"cannot list release assets for pruning: {proc.stderr.strip()}")
        return

    keep = set(referenced) | {f"{repo_name}.db", f"{repo_name}.files"}
    for raw_asset in proc.stdout.splitlines():
        asset = raw_asset.strip()
        if not PACKAGE_ASSET_RE.search(asset):
            continue
        if asset in keep or (asset.endswith(".sig") and asset[:-4] in keep):
            continue
        log(f"pruning stale asset {asset}")
        deletion = _gh(
            "release", "delete-asset", tag, asset, "--yes", "--repo", slug, capture=True
        )
        if deletion.returncode != 0:
            warning(f"cannot delete stale asset {asset}: {deletion.stderr.strip()}")


def publish(
    repo_name: str,
    tag: str,
    packages: list[BuiltPackage],
    db_path: Path,
    referenced: set[str],
    root: Path = ROOT,
) -> None:
    slug = github_slug(root)
    if not slug:
        raise RuntimeError("cannot determine GitHub repository (set GITHUB_REPOSITORY)")

    _ensure_release(slug, tag)

    db_files = [db_path, db_path.parent / f"{repo_name}.files"]
    missing = [str(path) for path in db_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"database files missing before upload: {', '.join(missing)}")

    # Upload packages before database files so clients never observe dangling DB references.
    pkg_assets = [path for pkg in packages for path in pkg.upload_paths]
    assets = [str(path) for path in pkg_assets + db_files]
    proc = _gh("release", "upload", tag, *assets, "--clobber", "--repo", slug)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to upload release assets (exit {proc.returncode})")

    _prune_old_assets(slug, tag, referenced, repo_name)


def _git_auth_env() -> dict[str, str]:
    """Supply the GitHub token via env instead of writing credentials into .git/config."""
    env = os.environ.copy()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return env
    header = "AUTHORIZATION: basic " + base64.b64encode(
        f"x-access-token:{token}".encode()
    ).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = header
    return env


def commit_database(
    repo_name: str,
    packages: list[BuiltPackage],
    removals: list[str] | None = None,
    root: Path = ROOT,
) -> None:
    paths = [
        root / "archlinux" / f"{repo_name}.db",
        root / "archlinux" / f"{repo_name}.files",
    ]
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return

    ensure_safe_directory(root)
    run_cmd(["git", "add", "--", *existing], cwd=root)

    if run_cmd(["git", "diff", "--cached", "--quiet"], cwd=root, check=False).returncode == 0:
        log("database unchanged; nothing to commit")
        return

    body_lines = [
        f"- build {pkg.archive.name}" for pkg in sorted(packages, key=lambda p: p.archive.name)
    ]
    body_lines.extend(f"- remove {name}" for name in sorted(removals or []))
    commit_args: list[str | Path] = [
        "git",
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "--message",
        "chore(db): update package database [skip ci]",
    ]
    if body_lines:
        commit_args.extend(["--message", "\n".join(body_lines)])
    run_cmd(commit_args, cwd=root)

    if os.environ.get("GITHUB_ACTIONS") != "true":
        log("not running in GitHub Actions; database committed locally but not pushed")
        return

    auth_env = _git_auth_env()
    for attempt in range(1, 4):
        if (
            run_cmd(
                ["git", "push", "origin", "HEAD:main"], cwd=root, check=False, env=auth_env
            ).returncode
            == 0
        ):
            log("database commit pushed to main")
            return
        warning(f"push failed (attempt {attempt}); retrying after rebase")
        run_cmd(["git", "fetch", "origin", "main"], cwd=root, check=False, env=auth_env)
        if run_cmd(["git", "rebase", "origin/main"], cwd=root, check=False).returncode != 0:
            run_cmd(["git", "rebase", "--abort"], cwd=root, check=False)
            break

    raise RuntimeError("failed to push the database commit")
