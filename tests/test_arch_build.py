"""Unit tests for arch_build core logic."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arch_build.aur import AurPackage
from arch_build.builder import StagingRepo, plan_removals, plan_targets
from arch_build.check_update import format_summary, run_aur_check
from arch_build.config import Config, LockEntry, load_config, read_lock, render_lock
from arch_build.pacman import (
    DbEntry,
    collect_built_packages,
    file_sha256,
    parse_desc,
    parse_srcinfo,
    read_database,
    sanitize_asset_name,
    update_database,
)
from arch_build.pkgbuild import PkgbuildInfo, full_version, read_pkgbuild
from arch_build.publish import ReleaseState, find_stale_assets, verify_release


def test_config_validation_and_duplicates(tmp_path: Path) -> None:
    cfg_path = tmp_path / "packages.toml"
    cfg_path.write_text(
        '[aur]\npackages = ["foo", "bar"]\n[repo]\nname = "my-repo"\ntag = "v1"\n[build]\nskip_pgpcheck = true\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.aur.packages == ["foo", "bar"]
    assert cfg.resolved_repo_name() == "my-repo"
    assert cfg.repo.tag == "v1"
    assert cfg.build.skip_pgpcheck is True

    cfg_path.write_text('[aur]\npackages = ["foo", "foo"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_config(cfg_path)

    cfg_path.write_text('[build]\nskip_pgpcheck = "yes"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_lock_roundtrip_and_split_pkgbase(tmp_path: Path) -> None:
    entries = {
        "foo-sub": LockEntry(
            name="foo-sub", source="aur", version="1.2-1", commit="abc123", pkgbase="foo-base"
        ),
        "bar": LockEntry(name="bar", source="aur", version="2.0-1", commit="def456"),
    }
    rendered = render_lock(entries)
    assert 'pkgbase = "foo-base"' in rendered
    assert 'pkgbase = "bar"' not in rendered

    lock_path = tmp_path / "packages.lock"
    lock_path.write_text(rendered, encoding="utf-8")
    loaded = read_lock(lock_path)
    assert loaded["foo-sub"].effective_pkgbase == "foo-base"
    assert loaded["bar"].effective_pkgbase == "bar"


def test_full_version_and_read_pkgbuild(tmp_path: Path) -> None:
    assert full_version("1.0", "2") == "1.0-2"
    assert full_version("1.0", "2", "0") == "1.0-2"
    assert full_version("1.0", "2", "1") == "1:1.0-2"

    pkg_dir = tmp_path / "demo"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        'pkgbase=demo-base\npkgname=(demo-a demo-b)\npkgver=3.1\npkgrel=2\nepoch=1\n',
        encoding="utf-8",
    )
    info = read_pkgbuild(pkg_dir)
    assert info.pkgbase == "demo-base"
    assert info.pkgname == ("demo-a", "demo-b")
    assert info.version == "1:3.1-2"


def test_read_database_and_parse_desc(tmp_path: Path) -> None:
    desc = (
        "%FILENAME%\nfoo-1_2.0-1-x86_64.pkg.tar.zst\n\n"
        "%NAME%\nfoo\n\n"
        "%BASE%\nfoo-base\n\n"
        "%VERSION%\n1:2.0-1\n\n"
        "%SHA256SUM%\nABCDEF0123456789\n"
    )
    parsed = parse_desc(desc)
    assert parsed["NAME"] == "foo"
    assert parsed["BASE"] == "foo-base"
    assert parsed["FILENAME"] == "foo-1_2.0-1-x86_64.pkg.tar.zst"

    db_path = tmp_path / "test.db"
    with tarfile.open(db_path, "w:gz") as tar:
        data = desc.encode("utf-8")
        info = tarfile.TarInfo("foo-1:2.0-1/desc")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    entries = read_database(db_path)
    assert entries["foo"] == DbEntry(
        name="foo",
        base="foo-base",
        version="1:2.0-1",
        filename="foo-1_2.0-1-x86_64.pkg.tar.zst",
        sha256="abcdef0123456789",
    )


def test_sanitize_and_collect_built_packages(tmp_path: Path) -> None:
    assert (
        sanitize_asset_name("foo-1:2.0+git-1-x86_64.pkg.tar.zst")
        == "foo-1_2.0_git-1-x86_64.pkg.tar.zst"
    )
    assert sanitize_asset_name("normal_pkg-1.0-1-any.pkg.tar.zst") == "normal_pkg-1.0-1-any.pkg.tar.zst"

    build_dir = tmp_path / "build"
    dest_dir = tmp_path / "pkgs"
    build_dir.mkdir()
    dest_dir.mkdir()
    (build_dir / "foo-1:2.0-1-x86_64.pkg.tar.zst").write_bytes(b"pkg")
    (build_dir / "foo-1:2.0-1-x86_64.pkg.tar.zst.sig").write_bytes(b"sig")
    (build_dir / "orphan.sig").write_bytes(b"ignore")

    collected = collect_built_packages(build_dir, dest_dir)
    assert len(collected) == 1
    assert collected[0].archive.name == "foo-1_2.0-1-x86_64.pkg.tar.zst"
    assert collected[0].signature is not None
    assert collected[0].signature.name == "foo-1_2.0-1-x86_64.pkg.tar.zst.sig"


def test_plan_targets_and_removals_with_split_packages(tmp_path: Path) -> None:
    cfg = Config.model_validate({"aur": {"packages": ["split-a", "split-b", "single"]}})
    lock = {
        "split-a": LockEntry(name="split-a", pkgbase="split-base", version="2.0-1", commit="c1"),
        "split-b": LockEntry(name="split-b", pkgbase="split-base", version="2.0-1", commit="c1"),
        "single": LockEntry(name="single", version="1.0-1", commit="c2"),
    }
    db = {
        "split-a": DbEntry(name="split-a", base="split-base", version="1.0-1"),
        "split-b": DbEntry(name="split-b", base="split-base", version="1.0-1"),
        "single": DbEntry(name="single", base="single", version="1.0-1"),
        "removed-pkg": DbEntry(name="removed-pkg", base="removed-pkg", version="0.1-1"),
        "local-pkg": DbEntry(name="local-pkg", base="old-base", version="1.0-1"),
        "local-dropped": DbEntry(name="local-dropped", base="new-base", version="1.0-1"),
    }
    local_pkgs = [
        (
            tmp_path / "local-pkg",
            PkgbuildInfo("1.0", "1", "", "new-base", ("local-pkg",), "1.0-1"),
        )
    ]

    targets = plan_targets(cfg, db, force=False, local_packages=local_pkgs, lock=lock)
    # Local rebuilt due to pkgbase rename; AUR split-a & split-b deduplicated into one split-base target.
    assert [(t.kind, t.name, t.package_names) for t in targets] == [
        ("local", "new-base", ("local-pkg",)),
        ("aur", "split-base", ("split-a", "split-b")),
    ]

    removals = plan_removals(cfg, db, local_pkgs, lock)
    assert removals == ["local-dropped", "removed-pkg"]


def test_run_aur_check_split_package_caches_pkgbase_commit() -> None:
    cfg = Config.model_validate({"aur": {"packages": ["split-a", "split-b"]}})
    rpc_results = {
        "split-a": AurPackage(name="split-a", pkgbase="split-base", version="1.0-1"),
        "split-b": AurPackage(name="split-b", pkgbase="split-base", version="1.0-1"),
    }
    failures: list[str] = []
    with (
        patch("arch_build.check_update.aur_info", return_value=rpc_results),
        patch("arch_build.check_update.aur_head_commit", return_value="deadbeef1234") as mock_head,
    ):
        new_lock, changes = run_aur_check(cfg, {}, failures)

    assert not failures
    mock_head.assert_called_once_with("split-base")
    assert new_lock["split-a"].pkgbase == "split-base"
    assert new_lock["split-b"].commit == "deadbeef1234"
    assert len(changes) == 2


def test_format_summary_no_duplicate_failures() -> None:
    text = format_summary([], ["`foo`: failed"])
    assert text.count("`foo`: failed") == 1
    assert "### Update check failed" in text


def test_parse_srcinfo() -> None:
    srcinfo = (
        "pkgbase = wivrn-server\n"
        "\tdepends = top-dep\n"
        "\tmakedepends = cmake\n"
        "\tvalidpgpkeys = ABC123\n"
        "\n"
        "pkgname = wivrn-server\n"
        "\tdepends = lib32-vulkan-icd-loader\n"
        "\n"
        "pkgname = wivrn-dashboard\n"
        "\tdepends = wivrn-server=26.9-1\n"
    )
    parsed = parse_srcinfo(srcinfo)
    assert parsed == {
        "pkgbase": ["wivrn-server"],
        "depends": ["top-dep"],
        "makedepends": ["cmake"],
        "validpgpkeys": ["ABC123"],
    }


def test_staging_repo_pacman_conf_order(tmp_path: Path) -> None:
    conf = tmp_path / "pacman.conf"
    conf.write_text("[core]\nInclude = /etc/pacman.d/mirrorlist\n", encoding="utf-8")

    with (
        patch.object(StagingRepo, "_remote_db_available", return_value=True),
        patch("arch_build.builder.run_cmd"),
    ):
        repo = StagingRepo(
            tmp_path / "work",
            published_repo=("arch-build", "https://example.com/releases/download/pacman"),
            pacman_conf=conf,
        )
        assert "[arch-build]" in conf.read_text(encoding="utf-8")
        assert "[arch-build-stage]" not in conf.read_text(encoding="utf-8")

        repo.has_staged = True
        repo._sync_pacman_conf()
        updated = conf.read_text(encoding="utf-8")
        assert updated.index("[arch-build-stage]") < updated.index("[arch-build]")


def test_verify_release_db_mismatch_is_fatal(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    files_path = tmp_path / "repo.files"
    db_path.write_bytes(b"db-bytes")
    files_path.write_bytes(b"files-bytes")

    # Release missing altogether while local DB exists -> fatal
    with pytest.raises(RuntimeError, match="out of sync"):
        verify_release("repo", db_path, {}, ReleaseState(exists=False, assets={}))

    # Release DB hash differs -> fatal
    release = ReleaseState(
        exists=True,
        assets={"repo.db": "0" * 64, "repo.files": file_sha256(files_path)},
    )
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        verify_release("repo", db_path, {}, release)

    # Local DB missing while Release already has DB -> fatal
    db_path.unlink()
    files_path.unlink()
    with pytest.raises(RuntimeError, match="local database"):
        verify_release("repo", db_path, {}, release)


def test_verify_release_reconciles_missing_corrupt_and_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    files_path = tmp_path / "repo.files"
    db_path.write_bytes(b"db-bytes")
    files_path.write_bytes(b"files-bytes")

    db_entries = {
        "ok-pkg": DbEntry("ok-pkg", "ok-pkg", "1.0-1", "ok-pkg-1.0-1.pkg.tar.zst", "aaa"),
        "missing-pkg": DbEntry(
            "missing-pkg", "missing-pkg", "1.0-1", "missing-pkg-1.0-1.pkg.tar.zst", "bbb"
        ),
        "corrupt-pkg": DbEntry(
            "corrupt-pkg", "corrupt-pkg", "1.0-1", "corrupt-pkg-1.0-1.pkg.tar.zst", "ccc"
        ),
        "split-sub": DbEntry(
            "split-sub", "split-base", "1.0-1", "split-sub-1.0-1.pkg.tar.zst", "ddd"
        ),
    }
    release = ReleaseState(
        exists=True,
        assets={
            "repo.db": file_sha256(db_path),
            "repo.files": file_sha256(files_path),
            "ok-pkg-1.0-1.pkg.tar.zst": "aaa",
            "corrupt-pkg-1.0-1.pkg.tar.zst": "wronghash",
            "old-pkg-0.9-1.pkg.tar.zst": "eee",
            "unexpected-note.txt": "fff",
        },
    )

    diff = verify_release("repo", db_path, db_entries, release)
    assert diff.missing_or_corrupt_packages == {"missing-pkg", "corrupt-pkg", "split-sub"}
    assert diff.stale_assets == ["old-pkg-0.9-1.pkg.tar.zst", "unexpected-note.txt"]

    # Split subpackage (unlisted in packages.toml) being missing still triggers pkgbase rebuild.
    cfg = Config.model_validate(
        {"aur": {"packages": ["ok-pkg", "missing-pkg", "corrupt-pkg", "split-main"]}}
    )
    lock = {
        "ok-pkg": LockEntry(name="ok-pkg", version="1.0-1", commit="c1"),
        "missing-pkg": LockEntry(name="missing-pkg", version="1.0-1", commit="c2"),
        "corrupt-pkg": LockEntry(name="corrupt-pkg", version="1.0-1", commit="c3"),
        "split-main": LockEntry(name="split-main", pkgbase="split-base", version="1.0-1", commit="c4"),
    }
    db_with_main = {
        **db_entries,
        "split-main": DbEntry(
            "split-main", "split-base", "1.0-1", "split-main-1.0-1.pkg.tar.zst", "ggg"
        ),
    }
    targets = plan_targets(
        cfg,
        db_with_main,
        force=False,
        local_packages=[],
        lock=lock,
        repair_packages=diff.missing_or_corrupt_packages,
    )
    assert [t.name for t in targets] == ["missing-pkg", "corrupt-pkg", "split-base"]


def test_find_stale_assets_strict_whitelist() -> None:
    assets: dict[str, str | None] = {
        "repo.db": "1",
        "repo.files": "2",
        "repo.db.sig": "3",
        "foo-1.0.pkg.tar.zst": "4",
        "foo-1.0.pkg.tar.zst.sig": "5",
        "old-repo.db": "6",
        "orphan.sig": "7",
        "random.txt": "8",
    }
    assert find_stale_assets(assets, {"foo-1.0.pkg.tar.zst"}, "repo") == [
        "old-repo.db",
        "orphan.sig",
        "random.txt",
    ]


def test_update_database_fresh_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "archlinux" / "repo.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"corrupt")

    entries = update_database("repo", [], ["old"], db_path, tmp_path / "work", fresh=True)
    assert entries == {}
    assert db_path.is_file()
    assert (db_path.parent / "repo.files").is_file()
