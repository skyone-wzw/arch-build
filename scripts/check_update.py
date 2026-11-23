#!/usr/bin/env python3
"""Check local and external (AUR) packages for updates.

Local packages are refreshed by running ``local/<package>/update.sh`` (when
present) and comparing the PKGBUILD version before/after.  External packages
are resolved through the AUR RPC and pinned in ``packages.lock``.

The script only touches the working tree; creating the pull request is left to
the workflow.  It exits non-zero when any update check failed, and a failed run
is expected to be discarded (no pull request).

Step outputs:

``changed``  -- "true" when the working tree contains something to commit
``failed``   -- "true" when at least one update check failed
``summary``  -- markdown summary used as the pull request body
"""

from __future__ import annotations

import subprocess
import sys

from common import (
    ROOT,
    aur_head_commit,
    aur_info,
    aur_package_names,
    error,
    group,
    endgroup,
    load_config,
    local_package_dirs,
    log,
    read_lock,
    read_pkgbuild,
    render_lock,
    set_output,
    warning,
    add_step_summary,
    LOCK_FILE,
)


def run_local_checks(failures: list[str]) -> list[str]:
    """Run every local update.sh and report version changes."""
    changes: list[str] = []

    for pkg_dir in local_package_dirs():
        name = pkg_dir.name
        update_script = pkg_dir / "update.sh"
        if not update_script.is_file():
            continue

        try:
            before = read_pkgbuild(pkg_dir)
        except Exception as exc:  # noqa: BLE001 - surfaced as a workflow error
            failures.append(f"`{name}`: cannot read PKGBUILD ({exc})")
            continue

        group(f"Running {update_script.relative_to(ROOT)}")
        try:
            try:
                proc = subprocess.run(
                    ["bash", str(update_script)], cwd=pkg_dir, timeout=600
                )
            except subprocess.TimeoutExpired:
                failures.append(f"`{name}`: update.sh timed out after 10 minutes")
                continue
        finally:
            endgroup()

        if proc.returncode != 0:
            failures.append(f"`{name}`: update.sh exited with code {proc.returncode}")
            continue

        try:
            after = read_pkgbuild(pkg_dir)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"`{name}`: cannot read PKGBUILD after update ({exc})")
            continue

        if before["version"] != after["version"]:
            changes.append(
                f"- **{name}** (local): `{before['version']}` → `{after['version']}`"
            )
            log(f"{name}: {before['version']} -> {after['version']}")
        else:
            dirty = subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--",
                    str((pkg_dir / "PKGBUILD").relative_to(ROOT)),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if dirty:
                changes.append(
                    f"- **{name}** (local): PKGBUILD updated (version unchanged)"
                )
                log(f"{name}: PKGBUILD changed without a version bump")

    return changes


def run_aur_check(config: dict, failures: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Resolve current AUR versions and build the new lock entries."""
    names = aur_package_names(config)
    old_lock = read_lock()
    new_lock: dict[str, dict] = {}
    changes: list[str] = []

    if not names:
        for name in sorted(old_lock):
            changes.append(f"- **{name}** (AUR): removed from packages.toml")
        return new_lock, changes

    group(f"Checking {len(names)} AUR package(s)")
    try:
        try:
            info = aur_info(names)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"AUR lookup failed: {exc}")
            # Keep the previous lock untouched rather than dropping packages.
            return {name: old_lock[name] for name in names if name in old_lock}, changes

        for name in names:
            previous = old_lock.get(name)
            result = info.get(name)

            if result is None:
                failures.append(f"`{name}`: not found on the AUR")
                if previous:
                    new_lock[name] = previous
                continue

            version = str(result.get("Version") or "")
            if not version:
                failures.append(f"`{name}`: AUR returned an empty version")
                if previous:
                    new_lock[name] = previous
                continue

            if result.get("OutOfDate") is not None:
                warning(f"{name} is flagged out-of-date on the AUR")

            # Reuse the pinned commit while the version is unchanged; only
            # query the AUR git repository when we actually need a new pin.
            commit = ""
            if previous and previous.get("version") == version:
                commit = previous.get("commit", "")
            if not commit:
                try:
                    commit = aur_head_commit(name)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"`{name}`: cannot resolve AUR commit ({exc})")
                    if previous:
                        new_lock[name] = previous
                    continue

            new_lock[name] = {
                "name": name,
                "source": "aur",
                "version": version,
                "commit": commit,
            }

            if previous is None:
                changes.append(f"- **{name}** (AUR): added at `{version}`")
            elif previous.get("version") != version:
                changes.append(
                    f"- **{name}** (AUR): `{previous.get('version')}` → `{version}`"
                )
            elif previous.get("commit") != commit:
                changes.append(
                    f"- **{name}** (AUR): pinned commit "
                    f"`{previous.get('commit', '')[:10]}` → `{commit[:10]}`"
                )
    finally:
        endgroup()

    for name in sorted(set(old_lock) - set(names)):
        changes.append(f"- **{name}** (AUR): removed from packages.toml")

    return new_lock, changes


def working_tree_dirty() -> bool:
    proc = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "local",
            "packages.toml",
            "packages.lock",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return bool(proc.stdout.strip())


def main() -> int:
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        error(f"invalid packages.toml: {exc}")
        return 1

    failures: list[str] = []
    try:
        local_changes = run_local_checks(failures)
        new_lock, aur_changes = run_aur_check(config, failures)
    except Exception as exc:  # noqa: BLE001 - never leave the workflow ambiguous
        error(f"update check crashed: {exc}")
        return 1

    new_lock_text = render_lock(new_lock)
    old_lock_text = LOCK_FILE.read_text(encoding="utf-8") if LOCK_FILE.is_file() else None
    # Do not create a header-only lock file when the very first AUR lookup
    # failed; that would produce a meaningless pull request.
    lock_changed = new_lock_text != old_lock_text and (bool(new_lock) or old_lock_text is not None)
    if lock_changed:
        LOCK_FILE.write_text(new_lock_text, encoding="utf-8")
        log(f"Wrote {LOCK_FILE.relative_to(ROOT)}")

    changed = lock_changed or working_tree_dirty()
    changes = local_changes + aur_changes

    if changes:
        body = "### Package updates\n\n" + "\n".join(changes)
    elif failures:
        body = "### Update check failed\n\n" + "\n".join(f"- {f}" for f in failures)
    else:
        body = "No package updates detected."

    if failures:
        body += (
            "\n\n> [!WARNING]\n"
            "> The following update checks failed and were skipped:\n"
            + "".join(f"> - {f}\n" for f in failures)
        )

    log("")
    log(body)

    set_output("changed", "true" if changed else "false")
    set_output("failed", "true" if failures else "false")
    set_output("summary", body)
    add_step_summary(body)

    if failures:
        for failure in failures:
            error(f"update check failed: {failure}")
        # Fail the command as well; the workflow also gates PR creation on
        # the "failed" output, and a failed run is discarded entirely.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
