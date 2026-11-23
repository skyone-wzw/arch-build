"""Check local and AUR packages for updates (all-or-nothing)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from arch_build import LOCK_FILE, ROOT
from arch_build.aur import aur_head_commit, aur_info
from arch_build.config import Config, LockEntry, load_config, read_lock, render_lock
from arch_build.gha import add_step_summary, error, log, log_group, set_output, warning
from arch_build.pkgbuild import local_package_dirs, read_pkgbuild


def _git_dirty(paths: list[str], root: Path = ROOT) -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def run_local_checks(failures: list[str], root: Path = ROOT) -> list[str]:
    changes: list[str] = []

    for pkg_dir in local_package_dirs(root):
        name = pkg_dir.name
        update_script = pkg_dir / "update.sh"
        if not update_script.is_file():
            continue

        try:
            before = read_pkgbuild(pkg_dir)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"`{name}`: cannot read PKGBUILD ({exc})")
            continue

        with log_group(f"Running {update_script.relative_to(root)}"):
            try:
                proc = subprocess.run(
                    ["bash", str(update_script)], cwd=pkg_dir, timeout=600, check=False
                )
            except subprocess.TimeoutExpired:
                failures.append(f"`{name}`: update.sh timed out after 10 minutes")
                continue

        if proc.returncode != 0:
            failures.append(f"`{name}`: update.sh exited with code {proc.returncode}")
            continue

        try:
            after = read_pkgbuild(pkg_dir)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"`{name}`: cannot read PKGBUILD after update ({exc})")
            continue

        if before.version != after.version:
            changes.append(f"- **{name}** (local): `{before.version}` → `{after.version}`")
            log(f"{name}: {before.version} -> {after.version}")
        elif _git_dirty([str(pkg_dir.relative_to(root))], root=root):
            changes.append(f"- **{name}** (local): files updated (version unchanged)")
            log(f"{name}: files changed without a version bump")

    return changes


def run_aur_check(
    config: Config, old_lock: dict[str, LockEntry], failures: list[str]
) -> tuple[dict[str, LockEntry], list[str]]:
    names = config.aur.packages
    new_lock: dict[str, LockEntry] = {}
    changes: list[str] = []

    if not names:
        for name in sorted(old_lock):
            changes.append(f"- **{name}** (AUR): removed from packages.toml")
        return new_lock, changes

    with log_group(f"Checking {len(names)} AUR package(s)"):
        try:
            info = aur_info(names)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"AUR lookup failed: {exc}")
            return dict(old_lock), changes

        commit_cache: dict[str, str] = {}
        for name in names:
            previous = old_lock.get(name)
            result = info.get(name)

            if result is None:
                failures.append(f"`{name}`: not found on the AUR")
                if previous:
                    new_lock[name] = previous
                continue

            if not result.version:
                failures.append(f"`{name}`: AUR returned an empty version")
                if previous:
                    new_lock[name] = previous
                continue

            if result.out_of_date is not None:
                warning(f"{name} is flagged out-of-date on the AUR")

            pkgbase = result.pkgbase
            commit = ""
            if (
                previous
                and previous.version == result.version
                and previous.effective_pkgbase == pkgbase
            ):
                commit = previous.commit
            if not commit:
                commit = commit_cache.get(pkgbase, "")
            if not commit:
                try:
                    commit = aur_head_commit(pkgbase)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"`{name}`: cannot resolve AUR commit ({exc})")
                    if previous:
                        new_lock[name] = previous
                    continue
                commit_cache[pkgbase] = commit

            new_lock[name] = LockEntry(
                name=name,
                source="aur",
                version=result.version,
                commit=commit,
                pkgbase=pkgbase if pkgbase != name else None,
            )

            if previous is None:
                changes.append(f"- **{name}** (AUR): added at `{result.version}`")
            elif previous.version != result.version:
                changes.append(f"- **{name}** (AUR): `{previous.version}` → `{result.version}`")
            elif previous.effective_pkgbase != pkgbase:
                changes.append(
                    f"- **{name}** (AUR): pkgbase `{previous.effective_pkgbase}` → `{pkgbase}`"
                )
            elif previous.commit != commit:
                changes.append(
                    f"- **{name}** (AUR): pinned commit `{previous.commit[:10]}` → `{commit[:10]}`"
                )

    for name in sorted(set(old_lock) - set(names)):
        changes.append(f"- **{name}** (AUR): removed from packages.toml")

    return new_lock, changes


def format_summary(changes: list[str], failures: list[str]) -> str:
    if failures:
        lines = ["### Update check failed", "", *(f"- {f}" for f in failures)]
        if changes:
            lines.extend(["", "#### Detected changes (discarded due to failures)", "", *changes])
        return "\n".join(lines)
    if changes:
        return "### Package updates\n\n" + "\n".join(changes)
    return "No package updates detected."


def main() -> int:
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        error(f"invalid packages.toml: {exc}")
        return 1

    failures: list[str] = []
    try:
        old_lock = read_lock()
        local_changes = run_local_checks(failures)
        new_lock, aur_changes = run_aur_check(config, old_lock, failures)
    except Exception as exc:  # noqa: BLE001
        error(f"update check crashed: {exc}")
        return 1

    lock_changed = False
    if not failures:
        new_lock_text = render_lock(new_lock)
        old_lock_text = LOCK_FILE.read_text(encoding="utf-8") if LOCK_FILE.is_file() else None
        # Avoid writing a header-only lockfile when no AUR packages have ever been locked.
        lock_changed = new_lock_text != old_lock_text and (
            bool(new_lock) or old_lock_text is not None
        )
        if lock_changed:
            LOCK_FILE.write_text(new_lock_text, encoding="utf-8")
            log(f"Wrote {LOCK_FILE.relative_to(ROOT)}")

    changed = (not failures) and (lock_changed or _git_dirty(["local", "packages.lock"]))
    changes = local_changes + aur_changes
    body = format_summary(changes, failures)

    log("")
    log(body)

    set_output("changed", "true" if changed else "false")
    set_output("failed", "true" if failures else "false")
    set_output("summary", body)
    add_step_summary(body)

    if failures:
        for failure in failures:
            error(f"update check failed: {failure}")
        return 1
    return 0


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
