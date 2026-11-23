"""Pacman database, .SRCINFO, and package artifact helpers."""

from __future__ import annotations

import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from arch_build import run_cmd
from arch_build.gha import log

PACKAGE_ARCHIVE_RE = re.compile(r"\.pkg\.tar\.(?:zst|xz|gz|bz2|lz4|lzo|lz|Z)$")
PACKAGE_ASSET_RE = re.compile(r"\.pkg\.tar\.(?:zst|xz|gz|bz2|lz4|lzo|lz|Z)(?:\.sig)?$")
_GITHUB_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class DbEntry:
    name: str
    base: str
    version: str
    filename: str = ""


@dataclass(frozen=True)
class BuiltPackage:
    archive: Path
    signature: Path | None = None

    @property
    def upload_paths(self) -> list[Path]:
        return [self.archive, self.signature] if self.signature else [self.archive]


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
    if not path.is_file():
        return {}

    entries: dict[str, DbEntry] = {}
    with tarfile.open(path, "r:*") as archive:
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


def parse_srcinfo(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            fields.setdefault(key, []).append(value)
    return fields


def sanitize_asset_name(filename: str) -> str:
    """Replace characters GitHub Releases rewrites (e.g. ':' in epoch, '+') before repo-add.

    repo-add writes the file's basename verbatim into %FILENAME%, so sanitizing
    beforehand keeps pacman's download URL aligned with the Release asset name.
    """
    return _GITHUB_UNSAFE_RE.sub("_", filename)


def collect_built_packages(build_dir: Path, dest_dir: Path) -> list[BuiltPackage]:
    archives = sorted(p for p in build_dir.iterdir() if p.is_file() and PACKAGE_ARCHIVE_RE.search(p.name))
    if not archives:
        raise RuntimeError("makepkg produced no package files")

    collected: list[BuiltPackage] = []
    for archive in archives:
        safe_name = sanitize_asset_name(archive.name)
        dest_archive = dest_dir / safe_name
        if dest_archive.exists():
            raise RuntimeError(f"duplicate package filename after sanitization: {safe_name}")
        shutil.copy2(archive, dest_archive)

        src_sig = archive.with_name(f"{archive.name}.sig")
        dest_sig: Path | None = None
        if src_sig.is_file():
            dest_sig = dest_dir / f"{safe_name}.sig"
            shutil.copy2(src_sig, dest_sig)

        if safe_name != archive.name:
            log(f"built {archive.name} (stored as {safe_name})")
        else:
            log(f"built {safe_name}")
        collected.append(BuiltPackage(archive=dest_archive, signature=dest_sig))
    return collected


def update_database(
    repo_name: str,
    packages: list[BuiltPackage],
    removals: list[str],
    db_path: Path,
    work_dir: Path,
) -> dict[str, DbEntry]:
    staging = work_dir / "repo"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    arch_dir = db_path.parent
    work_db = staging / f"{repo_name}.db.tar.gz"
    if db_path.is_file():
        shutil.copy2(db_path, work_db)
    existing_files = arch_dir / f"{repo_name}.files"
    if existing_files.is_file():
        shutil.copy2(existing_files, staging / f"{repo_name}.files.tar.gz")

    if removals:
        run_cmd(["repo-remove", "--quiet", work_db, *removals])
    if packages:
        run_cmd(["repo-add", "--quiet", work_db, *(pkg.archive for pkg in packages)])

    arch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work_db, db_path)

    work_files = staging / f"{repo_name}.files.tar.gz"
    if work_files.is_file():
        shutil.copy2(work_files, arch_dir / f"{repo_name}.files")

    return read_database(db_path)
