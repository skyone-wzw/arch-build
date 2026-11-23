"""AUR RPC v5 metadata and git commit lookups."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from arch_build import USER_AGENT

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT = "https://aur.archlinux.org/{pkgbase}.git"


@dataclass(frozen=True)
class AurPackage:
    name: str
    pkgbase: str
    version: str
    out_of_date: int | None = None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
def _fetch_rpc_chunk(client: httpx.Client, chunk: list[str]) -> dict:
    response = client.get(AUR_RPC, params=[("arg[]", name) for name in chunk])
    response.raise_for_status()
    payload = response.json()
    if payload.get("type") == "error":
        raise RuntimeError(f"AUR RPC error: {payload.get('error', 'unknown error')}")
    return payload


def aur_info(names: list[str], chunk_size: int = 50) -> dict[str, AurPackage]:
    """Fetch AUR RPC metadata for package names in batches."""
    results: dict[str, AurPackage] = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        for offset in range(0, len(names), chunk_size):
            payload = _fetch_rpc_chunk(client, names[offset : offset + chunk_size])
            for item in payload.get("results", []):
                name = item["Name"]
                results[name] = AurPackage(
                    name=name,
                    pkgbase=item.get("PackageBase") or name,
                    version=str(item.get("Version") or ""),
                    out_of_date=item.get("OutOfDate"),
                )
    return results


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2),
    retry=retry_if_exception_type(RuntimeError),
    reraise=True,
)
def aur_head_commit(pkgbase: str) -> str:
    """Resolve HEAD commit for an AUR git repository (keyed by PackageBase)."""
    url = AUR_GIT.format(pkgbase=pkgbase)
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"cannot resolve AUR commit for {pkgbase}: timed out") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"cannot resolve AUR commit for {pkgbase}: {detail}")

    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts:
            return parts[0]
    raise RuntimeError(f"cannot resolve AUR commit for {pkgbase}: empty ls-remote output")
