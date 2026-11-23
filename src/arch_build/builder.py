"""Build planning, staging repo management, and makepkg execution."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from arch_build import ROOT, USER_AGENT, run_cmd
from arch_build.aur import AUR_GIT
from arch_build.config import Config, LockEntry
from arch_build.gha import log, warning
from arch_build.pacman import (
    BuiltPackage,
    DbEntry,
    collect_built_packages,
    parse_srcinfo,
)
from arch_build.pkgbuild import PkgbuildInfo, local_package_dirs, read_pkgbuild

BUILDER = os.environ.get("ARCH_BUILD_USER", "builder")
BUILDER_HOME = Path(f"/home/{BUILDER}")
WORK = Path("/tmp/arch-build")
PACMAN_CONF = Path("/etc/pacman.conf")
STAGE_REPO_NAME = "arch-build-stage"

_MANAGED_BEGIN = "# >>> arch-build managed repos"
_MANAGED_END = "# <<< arch-build managed repos"

DEPENDENCY_FIELDS = (
    "depends",
    "depends_x86_64",
    "makedepends",
    "makedepends_x86_64",
    "checkdepends",
    "checkdepends_x86_64",
)


@dataclass(frozen=True)
class Target:
    kind: str  # "local" or "aur"
    name: str  # pkgbase (used for logging, build dir, and AUR git repo)
    version: str
    source: Path | None = None
    commit: str = ""
    package_names: tuple[str, ...] = field(default_factory=tuple)


def load_local_packages(root: Path = ROOT) -> list[tuple[Path, PkgbuildInfo]]:
    return [(pkg_dir, read_pkgbuild(pkg_dir)) for pkg_dir in local_package_dirs(root)]


def plan_targets(
    config: Config,
    db_entries: dict[str, DbEntry],
    force: bool,
    local_packages: list[tuple[Path, PkgbuildInfo]],
    lock: dict[str, LockEntry],
) -> list[Target]:
    targets: list[Target] = []

    for pkg_dir, info in local_packages:
        version_changed = any(
            name not in db_entries or db_entries[name].version != info.version
            for name in info.pkgname
        )
        # Rebuild when pkgbase changes so %BASE% in the DB stays in sync.
        base_changed = any(
            name in db_entries and db_entries[name].base != info.pkgbase
            for name in info.pkgname
        )
        if force or version_changed or base_changed:
            targets.append(
                Target(
                    kind="local",
                    name=info.pkgbase,
                    version=info.version,
                    source=pkg_dir,
                    package_names=info.pkgname,
                )
            )

    # Group configured AUR packages by PackageBase so split packages are built once.
    aur_groups: dict[str, list[LockEntry]] = {}
    for name in config.aur.packages:
        entry = lock.get(name)
        if not entry or not entry.version:
            warning(
                f"packages.lock has no version for AUR package {name!r}; "
                "run arch-check-update first"
            )
            continue
        aur_groups.setdefault(entry.effective_pkgbase, []).append(entry)

    for pkgbase, entries in aur_groups.items():
        needs_build = force or any(
            entry.name not in db_entries
            or db_entries[entry.name].version != entry.version
            or db_entries[entry.name].base != pkgbase
            for entry in entries
        )
        if needs_build:
            primary = entries[0]
            targets.append(
                Target(
                    kind="aur",
                    name=pkgbase,
                    version=primary.version,
                    commit=primary.commit,
                    package_names=tuple(e.name for e in entries),
                )
            )

    return targets


def plan_removals(
    config: Config,
    db_entries: dict[str, DbEntry],
    local_packages: list[tuple[Path, PkgbuildInfo]],
    lock: dict[str, LockEntry],
) -> list[str]:
    """Return DB package names no longer produced by any local or AUR source."""
    local_bases = {info.pkgbase for _, info in local_packages}
    local_names = {name for _, info in local_packages for name in info.pkgname}
    aur_names = set(config.aur.packages)

    aur_bases = set(aur_names)
    for name in aur_names:
        if name in lock:
            aur_bases.add(lock[name].effective_pkgbase)
        if name in db_entries:
            aur_bases.add(db_entries[name].base)

    removals: list[str] = []
    for name, entry in db_entries.items():
        base = entry.base or name
        if base in local_bases:
            if name not in local_names:
                removals.append(name)
        elif base in aur_bases:
            continue
        elif name in local_names:
            # Local pkgbase was renamed; plan_targets rebuilds it instead of removing.
            continue
        else:
            removals.append(name)
    return sorted(removals)


def ensure_build_environment(root: Path = ROOT) -> None:
    if os.geteuid() != 0:
        raise SystemExit("arch-build must run as root (use the Arch Linux container)")
    if subprocess.run(["id", "-u", BUILDER], capture_output=True, check=False).returncode != 0:
        setup_script = root / "scripts" / "setup-builder.sh"
        run_cmd(["bash", setup_script])


def builder_env() -> dict[str, str]:
    """Minimal environment for untrusted PKGBUILD execution (never expose secrets)."""
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


class StagingRepo:
    """Expose newly built and previously published packages to pacman during builds.

    This allows build-time AUR dependencies (makedepends/depends) to be resolved
    through ``pacman -S`` as long as the dependency is built earlier in the same
    run or already exists in the published release.
    """

    def __init__(
        self,
        work_dir: Path,
        published_repo: tuple[str, str] | None = None,
        pacman_conf: Path = PACMAN_CONF,
    ) -> None:
        self.stage_dir = work_dir / "stage-repo"
        self.stage_db = self.stage_dir / f"{STAGE_REPO_NAME}.db.tar.gz"
        self.pacman_conf = pacman_conf
        self.has_staged = False
        self.published_repo: tuple[str, str] | None = None

        if self.stage_dir.exists():
            shutil.rmtree(self.stage_dir)
        self.stage_dir.mkdir(parents=True)

        if published_repo is not None:
            repo_name, base_url = published_repo
            if self._remote_db_available(f"{base_url}/{repo_name}.db"):
                self.published_repo = (repo_name, base_url)
                self._sync_pacman_conf()
                run_cmd(["pacman", "-Sy", "--noconfirm", "--noprogressbar"])

    @staticmethod
    def _remote_db_available(url: str) -> bool:
        try:
            response = httpx.head(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=15.0,
                follow_redirects=True,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def add(self, packages: list[BuiltPackage]) -> None:
        if not packages:
            return
        staged_archives: list[Path] = []
        for pkg in packages:
            for src in pkg.upload_paths:
                dst = self.stage_dir / src.name
                if dst.exists():
                    dst.unlink()
                shutil.copy2(src, dst)
            staged_archives.append(self.stage_dir / pkg.archive.name)

        run_cmd(["repo-add", "--quiet", self.stage_db, *staged_archives])
        self.has_staged = True
        self._sync_pacman_conf()
        run_cmd(["pacman", "-Sy", "--noconfirm", "--noprogressbar"])

    def _sync_pacman_conf(self) -> None:
        sections: list[str] = []
        # Stage repo must precede the published repo so newly built packages win.
        if self.has_staged:
            sections.append(
                f"[{STAGE_REPO_NAME}]\n"
                "SigLevel = Optional TrustAll\n"
                f"Server = file://{self.stage_dir}"
            )
        if self.published_repo is not None:
            repo_name, base_url = self.published_repo
            sections.append(
                f"[{repo_name}]\n"
                "SigLevel = Optional TrustAll\n"
                f"Server = {base_url}"
            )

        content = self.pacman_conf.read_text(encoding="utf-8")
        if _MANAGED_BEGIN in content and _MANAGED_END in content:
            before, rest = content.split(_MANAGED_BEGIN, 1)
            _, after = rest.split(_MANAGED_END, 1)
            base = (before.rstrip() + "\n" + after.lstrip()).rstrip()
        else:
            base = content.rstrip()

        if sections:
            block = "\n\n".join(sections)
            updated = f"{base}\n\n{_MANAGED_BEGIN}\n{block}\n{_MANAGED_END}\n"
        else:
            updated = base + "\n"
        self.pacman_conf.write_text(updated, encoding="utf-8")


def _have_pgp_key(key: str) -> bool:
    return (
        subprocess.run(
            as_builder("gpg", "--batch", "--list-keys", key),
            env=builder_env(),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _import_pgp_key(key: str) -> bool:
    for server in ("hkps://keys.openpgp.org", "hkps://keyserver.ubuntu.com"):
        try:
            subprocess.run(
                as_builder("gpg", "--batch", "--quiet", "--keyserver", server, "--recv-keys", key),
                env=builder_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            warning(f"timed out fetching PGP key {key} from {server}")
            continue
        if _have_pgp_key(key):
            log(f"imported PGP key {key} from {server}")
            return True

    url = f"https://keys.openpgp.org/vks/v1/by-fingerprint/{key}"
    try:
        response = httpx.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True
        )
        response.raise_for_status()
        data = response.content
    except httpx.HTTPError as exc:
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
        timeout=30,
        check=False,
    )
    if _have_pgp_key(key):
        log(f"imported PGP key {key} from keys.openpgp.org")
        return True
    return False


def _prepare_signing(srcinfo: dict[str, list[str]], skip_pgpcheck: bool) -> list[str]:
    if skip_pgpcheck:
        log("PGP signature verification disabled by [build].skip_pgpcheck")
        return ["--skippgpcheck"]

    for key in sorted(set(srcinfo.get("validpgpkeys", []))):
        if _have_pgp_key(key):
            continue
        if not _import_pgp_key(key):
            raise RuntimeError(
                f"cannot import PGP key {key}; set [build].skip_pgpcheck = true to build anyway"
            )
    return []


def _read_srcinfo(build_dir: Path) -> dict[str, list[str]]:
    proc = subprocess.run(
        as_builder("makepkg", "--printsrcinfo"),
        cwd=build_dir,
        env=builder_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"makepkg --printsrcinfo failed in {build_dir.name}: {proc.stderr.strip()}"
        )
    return parse_srcinfo(proc.stdout)


def _install_dependencies(srcinfo: dict[str, list[str]]) -> None:
    dependencies = list(
        dict.fromkeys(dep for field_name in DEPENDENCY_FIELDS for dep in srcinfo.get(field_name, []))
    )
    if not dependencies:
        return

    # pacman -T prints only unsatisfied dependencies.
    proc = subprocess.run(
        ["pacman", "-T", "--", *dependencies], capture_output=True, text=True, check=False
    )
    missing = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not missing:
        log("all build dependencies are already installed")
        return

    log(f"installing build dependencies: {' '.join(missing)}")
    run_cmd(["pacman", "-S", "--noconfirm", "--needed", "--noprogressbar", "--asdeps", *missing])


def _prepare_source(target: Target, destination: Path) -> None:
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
        run_cmd(["git", "clone", "--quiet", AUR_GIT.format(pkgbase=target.name), destination])
        if target.commit:
            run_cmd(["git", "-C", destination, "checkout", "--quiet", target.commit])


def build_target(target: Target, pkgs_dir: Path, skip_pgpcheck: bool) -> list[BuiltPackage]:
    build_dir = WORK / "build" / f"{target.kind}-{target.name}"
    _prepare_source(target, build_dir)
    run_cmd(["chown", "-R", f"{BUILDER}:{BUILDER}", build_dir])

    srcinfo = _read_srcinfo(build_dir)
    extra_args = _prepare_signing(srcinfo, skip_pgpcheck)
    _install_dependencies(srcinfo)
    run_cmd(
        as_builder("makepkg", "--noconfirm", "--force", "--nodeps", *extra_args),
        cwd=build_dir,
        env=builder_env(),
    )

    collected = collect_built_packages(build_dir, pkgs_dir)
    shutil.rmtree(build_dir, ignore_errors=True)
    return collected
