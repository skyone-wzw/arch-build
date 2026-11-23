"""Local PKGBUILD discovery and metadata evaluation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from arch_build import ROOT

_PKGBUILD_PROBE = r"""
set -e
source ./PKGBUILD
printf '%s\0' "${pkgver}" "${pkgrel}" "${epoch:-}" "${pkgbase:-}" "${pkgname[@]}"
"""


@dataclass(frozen=True)
class PkgbuildInfo:
    pkgver: str
    pkgrel: str
    epoch: str
    pkgbase: str
    pkgname: tuple[str, ...]
    version: str


def full_version(pkgver: str, pkgrel: str, epoch: str = "") -> str:
    version = f"{pkgver}-{pkgrel}"
    if epoch and epoch != "0":
        version = f"{epoch}:{version}"
    return version


def read_pkgbuild(pkg_dir: Path) -> PkgbuildInfo:
    """Source a trusted local PKGBUILD in bash to extract version and names."""
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
            check=False,
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
    names = tuple(fields[4:])
    if not pkgver or not pkgrel:
        raise ValueError(f"{pkgbuild}: pkgver/pkgrel must not be empty")

    return PkgbuildInfo(
        pkgver=pkgver,
        pkgrel=pkgrel,
        epoch=epoch,
        pkgbase=pkgbase or names[0],
        pkgname=names,
        version=full_version(pkgver, pkgrel, epoch),
    )


def local_package_dirs(root: Path = ROOT) -> list[Path]:
    base = root / "local"
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir() if (path / "PKGBUILD").is_file())
