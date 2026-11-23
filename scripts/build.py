#!/usr/bin/env python3
"""Build packages whose version differs from the published database.

Designed to run as root inside an Arch Linux container (``archlinux:base-devel``)
in GitHub Actions.  It

1. compares the committed pacman database (``archlinux/<repo>.db``) with the
   versions of local PKGBUILDs and the pinned AUR packages in ``packages.lock``;
2. builds every outdated package with ``makepkg`` as an unprivileged user;
3. merges the resulting packages into the database with ``repo-add``;
4. uploads packages and the database to a fixed GitHub release tag;
5. commits the updated database to the main branch.

The fixed release tag means the ``Server`` URL in pacman.conf never changes.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from common import (
    ROOT,
    USER_AGENT,
    aur_package_names,
    endgroup,
    error,
    group,
    load_config,
    local_package_dirs,
    log,
    read_lock,
    read_pkgbuild,
    repo_settings,
    warning,
)

BUILDER = "builder"
BUILDER_HOME = Path(f"/home/{BUILDER}")
WORK = Path("/tmp/arch-build")

PACKAGE_ASSET_RE = re.compile(r"\.pkg\.tar\.(?:zst|xz|gz)(?:\.sig)?$")


@dataclass
class Target:
    kind: str  # "local" or "aur"
    name: str  # primary name, used for logging and directories
    version: str
    source: Path | None = None
    commit: str = ""
    package_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class DbEntry:
    name: str
    base: str
    version: str
    filename: str = ""


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def parse_desc(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal key, buffer
        if key is not None:
            fields[key] = "\n".join(buffer).strip()
        key = None
        buffer = []

    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%") and len(line) > 2:
            flush()
            key = line.strip("%")
        elif line.strip() == "":
            flush()
        elif key is not None:
            buffer.append(line)
    flush()
    return fields


def read_database(path: Path) -> dict[str, DbEntry]:
    """Return the database entries keyed by package name."""
    if not path.is_file():
        return {}

    entries: dict[str, DbEntry] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            desc = parse_desc(handle.read().decode("utf-8", "replace"))
            name = desc.get("NAME")
            if not name:
                continue
            entries[name] = DbEntry(
                name=name,
                base=desc.get("BASE") or name,
                version=desc.get("VERSION", ""),
                filename=desc.get("FILENAME", ""),
            )
    return entries


def referenced_filenames(entries: dict[str, DbEntry]) -> set[str]:
    return {entry.filename for entry in entries.values() if entry.filename}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def load_local_packages() -> list[tuple[Path, dict]]:
    return [(pkg_dir, read_pkgbuild(pkg_dir)) for pkg_dir in local_package_dirs()]


def plan_targets(
    config: dict,
    db_entries: dict[str, DbEntry],
    force: bool,
    local_packages: list[tuple[Path, dict]],
) -> list[Target]:
    targets: list[Target] = []

    for pkg_dir, info in local_packages:
        version_changed = any(
            name not in db_entries or db_entries[name].version != info["version"]
            for name in info["pkgname"]
        )
        # A renamed pkgbase must be reflected in the database even when the
        # package names and versions are unchanged.
        base_changed = any(
            name in db_entries and db_entries[name].base != info["pkgbase"]
            for name in info["pkgname"]
        )
        if force or version_changed or base_changed:
            targets.append(
                Target(
                    kind="local",
                    name=info["pkgbase"],
                    version=info["version"],
                    source=pkg_dir,
                    package_names=tuple(info["pkgname"]),
                )
            )

    lock = read_lock()
    for name in aur_package_names(config):
        entry = lock.get(name)
        if not entry or not entry.get("version"):
            warning(
                f"packages.lock has no version for AUR package {name!r}; "
                "run the update check workflow first"
            )
            continue
        outdated = force or (
            name not in db_entries or db_entries[name].version != entry["version"]
        )
        if outdated:
            targets.append(
                Target(
                    kind="aur",
                    name=name,
                    version=entry["version"],
                    commit=entry.get("commit", ""),
                    package_names=(name,),
                )
            )

    return targets


def plan_removals(
    config: dict,
    db_entries: dict[str, DbEntry],
    local_packages: list[tuple[Path, dict]],
) -> list[str]:
    """Return database package names that are no longer produced by any source.

    Every database entry records its pkgbase (%BASE%), so removing an AUR
    entry, a local package directory, or one package of a split local build is
    detected without keeping extra state.
    """
    local_bases = {info["pkgbase"] for _, info in local_packages}
    local_names = {name for _, info in local_packages for name in info["pkgname"]}
    aur_names = set(aur_package_names(config))

    # Bases owned by a configured AUR source. A configured name is normally
    # the pkgbase; when it is a split-package member, resolve its base.
    aur_bases = set(aur_names)
    for name in aur_names:
        entry = db_entries.get(name)
        if entry:
            aur_bases.add(entry.base)

    removals: list[str] = []
    for name, entry in db_entries.items():
        base = entry.base or name
        if base in local_bases:
            # Local split packages are known exactly; drop removed members.
            if name not in local_names:
                removals.append(name)
        elif base in aur_bases:
            continue
        elif name in local_names:
            # The package is still produced by a local source whose pkgbase
            # changed; plan_targets rebuilds it instead of removing it.
            continue
        else:
            removals.append(name)
    return sorted(removals)


# ---------------------------------------------------------------------------
# Build environment
# ---------------------------------------------------------------------------

def run(
    cmd: list[str | Path],
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    printable = " ".join(str(part) for part in cmd)
    log(f"+ {printable}")
    proc = subprocess.run([str(part) for part in cmd], cwd=cwd, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {printable}")
    return proc


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("build.py must run as root (use the GitHub Actions container)")


def prepare_environment() -> None:
    # The workflow installs git/python before checkout; finish the toolchain here.
    run(["pacman", "-Syu", "--noconfirm", "--needed"])
    run(
        [
            "pacman",
            "-S",
            "--noconfirm",
            "--needed",
            "--asdeps",
            "base-devel",
            "zstd",
            "gnupg",
            "github-cli",
            "curl",
            "ca-certificates",
        ]
    )


def ensure_builder() -> None:
    if subprocess.run(["id", "-u", BUILDER], capture_output=True).returncode != 0:
        run(["useradd", "--create-home", "--user-group", "--shell", "/bin/bash", BUILDER])
    # The build user never gets sudo: dependencies are installed by this
    # (root) process before makepkg runs, so PKGBUILD code cannot escalate.


def chown_tree(path: Path) -> None:
    run(["chown", "-R", f"{BUILDER}:{BUILDER}", path])


def builder_env() -> dict[str, str]:
    # Deliberately minimal: AUR PKGBUILDs run as this user, so never expose
    # tokens or other workflow secrets to them.
    return {
        "HOME": str(BUILDER_HOME),
        "USER": BUILDER,
        "LOGNAME": BUILDER,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }


def as_builder(*cmd: str | Path) -> list[str | Path]:
    return ["runuser", "-u", BUILDER, "--", *cmd]


def have_pgp_key(key: str) -> bool:
    return (
        subprocess.run(
            as_builder("gpg", "--batch", "--list-keys", key),
            env=builder_env(),
            capture_output=True,
        ).returncode
        == 0
    )


def import_pgp_key(key: str) -> bool:
    for server in ("hkps://keys.openpgp.org", "hkps://keyserver.ubuntu.com"):
        subprocess.run(
            as_builder(
                "gpg", "--batch", "--quiet", "--keyserver", server, "--recv-keys", key
            ),
            env=builder_env(),
            capture_output=True,
            text=True,
        )
        if have_pgp_key(key):
            log(f"imported PGP key {key} from {server}")
            return True

    # Fallback for keyservers that refuse hkps or filter the key.
    url = f"https://keys.openpgp.org/vks/v1/by-fingerprint/{key}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as exc:  # noqa: BLE001
        warning(f"cannot download PGP key {key}: {exc}")
        return False

    keys_dir = WORK / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_file = keys_dir / f"{key}.asc"
    key_file.write_bytes(data)
    key_file.chmod(0o644)
    subprocess.run(
        as_builder("gpg", "--batch", "--quiet", "--import", key_file),
        env=builder_env(),
        capture_output=True,
        text=True,
    )
    if have_pgp_key(key):
        log(f"imported PGP key {key} from keys.openpgp.org")
        return True
    return False


def read_srcinfo(build_dir: Path) -> dict[str, list[str]]:
    proc = subprocess.run(
        as_builder("makepkg", "--printsrcinfo"),
        cwd=build_dir,
        env=builder_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"makepkg --printsrcinfo failed in {build_dir.name}: {proc.stderr.strip()}"
        )

    fields: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            fields.setdefault(key, []).append(value)
    return fields


DEPENDENCY_FIELDS = (
    "depends",
    "depends_x86_64",
    "makedepends",
    "makedepends_x86_64",
    "checkdepends",
    "checkdepends_x86_64",
)


def install_dependencies(srcinfo: dict[str, list[str]]) -> None:
    dependencies: list[str] = []
    seen: set[str] = set()
    for field_name in DEPENDENCY_FIELDS:
        for dependency in srcinfo.get(field_name, []):
            if dependency not in seen:
                seen.add(dependency)
                dependencies.append(dependency)
    if not dependencies:
        return

    # pacman -T prints only the dependencies that are not satisfied.
    proc = subprocess.run(
        ["pacman", "-T", "--", *dependencies], capture_output=True, text=True
    )
    missing = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not missing:
        log("all build dependencies are already installed")
        return

    log(f"installing build dependencies: {' '.join(missing)}")
    run(["pacman", "-S", "--noconfirm", "--needed", "--asdeps", *missing])


def prepare_signing(srcinfo: dict[str, list[str]], skip_pgpcheck: bool) -> list[str]:
    if skip_pgpcheck:
        log("PGP signature verification disabled by [build].skip_pgpcheck")
        return ["--skippgpcheck"]

    for key in sorted(set(srcinfo.get("validpgpkeys", []))):
        if have_pgp_key(key):
            continue
        if not import_pgp_key(key):
            raise RuntimeError(
                f"cannot import PGP key {key}; set [build].skip_pgpcheck = true to build anyway"
            )
    return []


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def prepare_source(target: Target, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if target.kind == "local":
        assert target.source is not None
        shutil.copytree(
            target.source,
            destination,
            ignore=shutil.ignore_patterns(".git", "src", "pkg"),
        )
    else:
        run(
            [
                "git",
                "clone",
                "--quiet",
                f"https://aur.archlinux.org/{target.name}.git",
                destination,
            ]
        )
        if target.commit:
            run(["git", "-C", destination, "checkout", "--quiet", target.commit])


def build_target(target: Target, pkgs_dir: Path, skip_pgpcheck: bool) -> list[Path]:
    build_dir = WORK / "build" / f"{target.kind}-{target.name}"
    prepare_source(target, build_dir)
    chown_tree(build_dir)

    srcinfo = read_srcinfo(build_dir)
    extra_args = prepare_signing(srcinfo, skip_pgpcheck)
    install_dependencies(srcinfo)
    run(
        as_builder("makepkg", "--noconfirm", "--force", "--nodeps", *extra_args),
        cwd=build_dir,
        env=builder_env(),
    )

    packages = sorted(build_dir.glob("*.pkg.tar.*"))
    if not packages:
        raise RuntimeError("makepkg produced no package files")

    collected: list[Path] = []
    for package in packages:
        destination = pkgs_dir / package.name
        shutil.copy2(package, destination)
        collected.append(destination)
        log(f"built {package.name}")

    shutil.rmtree(build_dir, ignore_errors=True)
    return collected


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def update_database(
    repo_name: str,
    packages: list[Path],
    removals: list[str],
    db_path: Path,
) -> dict[str, DbEntry]:
    staging = WORK / "repo"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    work_db = staging / f"{repo_name}.db.tar.gz"
    if db_path.is_file():
        shutil.copy2(db_path, work_db)
    existing_files = ROOT / "archlinux" / f"{repo_name}.files"
    if existing_files.is_file():
        shutil.copy2(existing_files, staging / f"{repo_name}.files.tar.gz")

    if removals:
        run(["repo-remove", "--quiet", work_db, *removals])
    if packages:
        run(["repo-add", "--quiet", work_db, *packages])

    arch_dir = ROOT / "archlinux"
    arch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work_db, db_path)

    work_files = staging / f"{repo_name}.files.tar.gz"
    if work_files.is_file():
        shutil.copy2(work_files, arch_dir / f"{repo_name}.files")

    return read_database(db_path)


def github_slug() -> str | None:
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", proc.stdout.strip())
    return match.group(1) if match else None


def gh(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=capture,
        text=True,
        check=False,
    )


def ensure_release(slug: str, tag: str) -> None:
    if gh("release", "view", tag, "--repo", slug, capture=True).returncode == 0:
        return
    proc = gh(
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


def publish(
    repo_name: str,
    tag: str,
    packages: list[Path],
    db_path: Path,
    referenced: set[str],
) -> None:
    slug = github_slug()
    if not slug:
        raise RuntimeError("cannot determine GitHub repository (set GITHUB_REPOSITORY)")

    ensure_release(slug, tag)

    db_files = [db_path, ROOT / "archlinux" / f"{repo_name}.files"]
    missing = [str(path) for path in db_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"database files missing before upload: {', '.join(missing)}")
    assets = [str(path) for path in packages + db_files]
    proc = gh("release", "upload", tag, *assets, "--clobber", "--repo", slug)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to upload release assets (exit {proc.returncode})")

    prune_old_assets(slug, tag, referenced, repo_name)


def prune_old_assets(slug: str, tag: str, referenced: set[str], repo_name: str) -> None:
    proc = gh(
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
    for asset in proc.stdout.splitlines():
        asset = asset.strip()
        if not PACKAGE_ASSET_RE.search(asset):
            continue
        if asset in keep or (asset.endswith(".sig") and asset[:-4] in keep):
            continue
        log(f"pruning stale asset {asset}")
        deletion = gh(
            "release", "delete-asset", tag, asset, "--yes", "--repo", slug, capture=True
        )
        if deletion.returncode != 0:
            warning(f"cannot delete stale asset {asset}: {deletion.stderr.strip()}")


def git_auth_env() -> dict[str, str]:
    """Environment that supplies the GitHub token without writing it to disk."""
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
    repo_name: str, packages: list[Path], removals: list[str] | None = None
) -> None:
    paths = [
        ROOT / "archlinux" / f"{repo_name}.db",
        ROOT / "archlinux" / f"{repo_name}.files",
    ]
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return

    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(ROOT)])
    run(["git", "add", "--", *existing], cwd=ROOT)

    if run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode == 0:
        log("database unchanged; nothing to commit")
        return

    body_lines = [
        f"- build {package.name}" for package in sorted(packages, key=lambda p: p.name)
    ]
    body_lines.extend(f"- remove {name}" for name in sorted(removals or []))
    commit_args = [
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
    run(commit_args, cwd=ROOT)

    if os.environ.get("GITHUB_ACTIONS") != "true":
        log("not running in GitHub Actions; database committed locally but not pushed")
        return

    auth_env = git_auth_env()
    for attempt in range(1, 4):
        if (
            run(
                ["git", "push", "origin", "HEAD:main"],
                cwd=ROOT,
                check=False,
                env=auth_env,
            ).returncode
            == 0
        ):
            log("database commit pushed to main")
            return
        warning(f"push failed (attempt {attempt}); retrying after rebase")
        run(["git", "fetch", "origin", "main"], cwd=ROOT, check=False, env=auth_env)
        if run(["git", "rebase", "origin/main"], cwd=ROOT, check=False).returncode != 0:
            run(["git", "rebase", "--abort"], cwd=ROOT, check=False)
            break

    raise RuntimeError("failed to push the database commit")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="rebuild all packages regardless of the database"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only print the build plan"
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="update the database locally but skip release upload and the database commit",
    )
    args = parser.parse_args(argv)

    config = load_config()
    repo_name, tag = repo_settings(config)

    build_section = config.get("build", {})
    if not isinstance(build_section, dict):
        raise ValueError("packages.toml: [build] must be a table")
    skip_pgpcheck = build_section.get("skip_pgpcheck", False)
    if not isinstance(skip_pgpcheck, bool):
        raise ValueError("packages.toml: [build].skip_pgpcheck must be a boolean")

    db_path = ROOT / "archlinux" / f"{repo_name}.db"

    db_entries = read_database(db_path)
    local_packages = load_local_packages()
    targets = plan_targets(config, db_entries, args.force, local_packages)
    removals = plan_removals(config, db_entries, local_packages)

    if not targets and not removals:
        log("All packages are up to date; nothing to build.")
        return 0

    log("Build plan:")
    for target in targets:
        log(f"  - build {target.kind}:{target.name} {target.version}")
    for name in removals:
        log(f"  - remove {name}")

    if args.dry_run:
        return 0

    ensure_root()
    prepare_environment()
    WORK.mkdir(parents=True, exist_ok=True)

    built: list[Path] = []
    failures: list[str] = []

    if targets:
        ensure_builder()
        pkgs_dir = WORK / "pkgs"
        if pkgs_dir.exists():
            shutil.rmtree(pkgs_dir)
        pkgs_dir.mkdir(parents=True)

        for target in targets:
            group(f"Building {target.name} {target.version}")
            try:
                built.extend(build_target(target, pkgs_dir, skip_pgpcheck))
            except Exception as exc:  # noqa: BLE001 - keep building the rest
                failures.append(f"{target.name}: {exc}")
                error(f"failed to build {target.name}: {exc}")
            finally:
                endgroup()

    # All or nothing: a single failed build must not change the published
    # database or release assets. The next run retries the whole set.
    if failures:
        for failure in failures:
            error(f"build failure: {failure}")
        error("build failed; database and release assets were left untouched")
        return 1

    if built or removals:
        new_entries = update_database(repo_name, built, removals, db_path)
        referenced = referenced_filenames(new_entries)
        if args.no_publish:
            log("--no-publish given; skipping release upload and git push")
        else:
            publish(repo_name, tag, built, db_path, referenced)
            commit_database(repo_name, built, removals)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as exc:
        error(str(exc))
        sys.exit(1)
