"""Build outdated packages, update the pacman database, and publish to GitHub."""

from __future__ import annotations

import argparse
import shutil
import sys

from arch_build import ROOT
from arch_build.builder import (
    WORK,
    StagingRepo,
    build_target,
    ensure_build_environment,
    load_local_packages,
    plan_removals,
    plan_targets,
)
from arch_build.config import load_config, read_lock
from arch_build.gha import error, log, log_group
from arch_build.pacman import BuiltPackage, read_database, referenced_filenames, update_database
from arch_build.publish import (
    commit_database,
    fetch_release_state,
    github_slug,
    prune_assets,
    publish,
    verify_release,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="rebuild all packages from scratch and reset release"
    )
    parser.add_argument("--dry-run", action="store_true", help="only print the build plan")
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="skip release reconciliation/upload and git push",
    )
    args = parser.parse_args(argv)

    config = load_config()
    repo_name = config.resolved_repo_name()
    tag = config.repo.tag
    db_path = ROOT / "archlinux" / f"{repo_name}.db"

    db_entries = read_database(db_path)
    local_packages = load_local_packages()
    lock = read_lock()

    slug: str | None = None
    repair_packages: set[str] = set()
    stale_assets: list[str] = []

    if not args.no_publish:
        slug = github_slug()
        if not slug:
            raise RuntimeError("cannot determine GitHub repository (set GITHUB_REPOSITORY)")
        release = fetch_release_state(slug, tag)
        if not args.force:
            diff = verify_release(repo_name, db_path, db_entries, release)
            repair_packages = diff.missing_or_corrupt_packages
            stale_assets = diff.stale_assets
        elif not local_packages and not config.aur.packages:
            stale_assets = sorted(release.assets)

    targets = plan_targets(
        config, db_entries, args.force, local_packages, lock, repair_packages=repair_packages
    )
    removals = plan_removals(config, db_entries, local_packages, lock)

    if not targets and not removals and not stale_assets:
        log("All packages and release assets are up to date; nothing to do.")
        return 0

    log("Build plan:")
    for target in targets:
        log(f"  - build {target.kind}:{target.name} {target.version}")
    for name in removals:
        log(f"  - remove {name}")
    for asset in stale_assets:
        log(f"  - prune {asset}")

    if args.dry_run:
        return 0

    if not targets and not removals:
        assert slug is not None
        prune_assets(slug, tag, stale_assets)
        return 0

    ensure_build_environment()
    WORK.mkdir(parents=True, exist_ok=True)

    built: list[BuiltPackage] = []
    failures: list[str] = []

    if targets:
        pkgs_dir = WORK / "pkgs"
        if pkgs_dir.exists():
            shutil.rmtree(pkgs_dir)
        pkgs_dir.mkdir(parents=True)

        # In --force mode, never mount the potentially inconsistent published release.
        published_repo: tuple[str, str] | None = None
        if not args.force and db_path.is_file() and slug:
            published_repo = (
                repo_name,
                f"https://github.com/{slug}/releases/download/{tag}",
            )
        staging_repo = StagingRepo(WORK, published_repo=published_repo)

        for target in targets:
            with log_group(f"Building {target.name} {target.version}"):
                try:
                    target_pkgs = build_target(target, pkgs_dir, config.build.skip_pgpcheck)
                    built.extend(target_pkgs)
                    staging_repo.add(target_pkgs)
                except Exception as exc:  # noqa: BLE001 - continue to surface all failures
                    failures.append(f"{target.name}: {exc}")
                    error(f"failed to build {target.name}: {exc}")

    # All or nothing: leave database and release untouched if any target failed.
    if failures:
        for failure in failures:
            error(f"build failure: {failure}")
        error("build failed; database and release assets were left untouched")
        return 1

    if built or removals or args.force:
        new_entries = update_database(repo_name, built, removals, db_path, WORK, fresh=args.force)
        referenced = referenced_filenames(new_entries)
        if args.no_publish:
            log("--no-publish given; skipping release upload and git push")
        else:
            publish(repo_name, tag, built, db_path, referenced)
            commit_database(repo_name, built, removals)
    return 0


def cli() -> None:
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as exc:
        error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    cli()
