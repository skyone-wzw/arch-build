"""GitHub Release reconciliation, asset publishing, and database git commits."""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from arch_build import ROOT, USER_AGENT, run_cmd
from arch_build.gha import log, warning
from arch_build.pacman import BuiltPackage, DbEntry, file_sha256, referenced_filenames

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class ReleaseState:
    exists: bool
    assets: dict[str, str | None]  # asset filename -> lowercase sha256 hex (or None)


@dataclass(frozen=True)
class ReleaseDiff:
    missing_or_corrupt_packages: set[str]
    stale_assets: list[str]


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


def _github_headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_sha256_digest(digest: str | None) -> str | None:
    if not digest or not digest.startswith("sha256:"):
        return None
    return digest.removeprefix("sha256:").lower()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
def fetch_release_state(slug: str, tag: str) -> ReleaseState:
    """Fetch all release asset names and SHA256 digests via the GitHub REST API."""
    with httpx.Client(headers=_github_headers(), timeout=30.0, follow_redirects=True) as client:
        resp = client.get(f"{GITHUB_API}/repos/{slug}/releases/tags/{tag}")
        if resp.status_code == 404:
            return ReleaseState(exists=False, assets={})
        resp.raise_for_status()
        release_id = resp.json()["id"]

        assets: dict[str, str | None] = {}
        next_url: str | None = (
            f"{GITHUB_API}/repos/{slug}/releases/{release_id}/assets?per_page=100"
        )
        while next_url:
            page = client.get(next_url)
            page.raise_for_status()
            for item in page.json():
                assets[item["name"]] = _parse_sha256_digest(item.get("digest"))
            next_link = page.links.get("next")
            next_url = next_link["url"] if next_link else None

    return ReleaseState(exists=True, assets=assets)


def find_stale_assets(
    release_assets: dict[str, str | None], referenced: set[str], repo_name: str
) -> list[str]:
    """Return every asset outside the strict whitelist of DB-referenced files and DB files."""
    keep = set(referenced) | {f"{repo_name}.db", f"{repo_name}.files"}
    return sorted(
        asset
        for asset in release_assets
        if not (asset in keep or (asset.endswith(".sig") and asset[:-4] in keep))
    )


def verify_release(
    repo_name: str,
    db_path: Path,
    db_entries: dict[str, DbEntry],
    release: ReleaseState,
) -> ReleaseDiff:
    """Verify local DB matches Release DB (fatal on mismatch), then diff Release package assets."""
    files_path = db_path.parent / f"{repo_name}.files"
    db_asset = f"{repo_name}.db"
    files_asset = f"{repo_name}.files"
    local_db_exists = db_path.is_file()
    local_files_exists = files_path.is_file()
    remote_db_exists = db_asset in release.assets
    remote_files_exists = files_asset in release.assets

    if not local_db_exists and not local_files_exists:
        if remote_db_exists or remote_files_exists:
            raise RuntimeError(
                f"local database ({db_asset}/{files_asset}) is missing, "
                "but the GitHub Release already contains database files. "
                "Inspect the mismatch and run with --force to rebuild and reset the release."
            )
        return ReleaseDiff(
            missing_or_corrupt_packages=set(),
            stale_assets=sorted(release.assets),
        )

    db_errors: list[str] = []
    if not local_db_exists:
        db_errors.append(f"local {db_asset} is missing while {files_asset} exists")
    if not local_files_exists:
        db_errors.append(f"local {files_asset} is missing while {db_asset} exists")
    if not release.exists:
        db_errors.append("GitHub Release tag does not exist while local database is present")
    else:
        for local_path, asset_name in ((db_path, db_asset), (files_path, files_asset)):
            if not local_path.is_file():
                continue
            local_hash = file_sha256(local_path)
            if asset_name not in release.assets:
                db_errors.append(f"{asset_name}: missing from release (local sha256={local_hash})")
            else:
                remote_hash = release.assets[asset_name]
                if remote_hash != local_hash:
                    db_errors.append(
                        f"{asset_name}: sha256 mismatch "
                        f"(local={local_hash}, release={remote_hash or 'unavailable'})"
                    )

    if db_errors:
        details = "\n  - ".join(db_errors)
        raise RuntimeError(
            "repository database and GitHub Release database are out of sync:\n"
            f"  - {details}\n"
            "Inspect the cause and run with --force to rebuild and reset the release."
        )

    missing_or_corrupt: set[str] = set()
    for name, entry in db_entries.items():
        if not entry.filename or entry.filename not in release.assets:
            missing_or_corrupt.add(name)
            continue
        remote_hash = release.assets[entry.filename]
        if entry.sha256 and remote_hash != entry.sha256:
            missing_or_corrupt.add(name)

    stale = find_stale_assets(release.assets, referenced_filenames(db_entries), repo_name)
    return ReleaseDiff(missing_or_corrupt_packages=missing_or_corrupt, stale_assets=stale)


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


def prune_assets(slug: str, tag: str, assets: list[str]) -> None:
    for asset in assets:
        log(f"pruning stale asset {asset}")
        deletion = _gh(
            "release", "delete-asset", tag, asset, "--yes", "--repo", slug, capture=True
        )
        if deletion.returncode != 0:
            warning(f"cannot delete stale asset {asset}: {deletion.stderr.strip()}")


def _prune_old_assets(slug: str, tag: str, referenced: set[str], repo_name: str) -> None:
    try:
        release = fetch_release_state(slug, tag)
    except httpx.HTTPError as exc:
        warning(f"cannot list release assets for pruning: {exc}")
        return
    if not release.exists:
        return
    stale = find_stale_assets(release.assets, referenced, repo_name)
    prune_assets(slug, tag, stale)


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
