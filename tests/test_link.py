"""conf.d link/edit/mask CLI tests (D5, D8, D9)."""

from __future__ import annotations

import io
import os
import re
import runpy
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import collect, confd, doctor  # noqa: E402
from macosctl import link  # noqa: E402


DEFAULTS = '''\
schema = 1
[defaults]
working_directory = "/srv"
log_dir = "/tmp/services"
throttle_seconds = 10
path = "/usr/bin:/bin"
log_rotate_interval_seconds = 900
'''


def _fragment(name: str, *, port: int = 9999, extra: str = "") -> str:
    return f'''\
schema = 1
[[service]]
name = "{name}"
label = "com.korellas.{name}"
port = {port}
group = "test"
exec = ["/bin/echo", "{name}"]
depends_on = []
{extra}'''


def _root(base: Path, *, fragment: str | None = None) -> Path:
    root = base / "config"
    (root / "conf.d").mkdir(parents=True)
    root.chmod(0o755)
    (root / "macosctl.toml").write_text(DEFAULTS)
    policy = root / "policy.json"
    policy.write_text(
        '{"schema":1,"label_prefix":"com.korellas.",'
        '"label_exceptions":{},"service_user":"example",'
        '"groups":["test"]}'
    )
    policy.chmod(0o644)
    if fragment is not None:
        (root / "conf.d" / "30-base.toml").write_text(fragment)
    return root


def _run(root: Path, *argv: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [
            sys.executable, str(REPO / "bin" / "macosctl"),
            "--config-root", str(root), *argv,
        ],
        capture_output=True, text=True, check=False, env=env,
    )


def test_unlink_never_deletes_the_target():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        target = base / "service.toml"
        target.write_text(_fragment("x"))
        root = _root(base)
        link.link(target, root)

        link.unlink("service", root)

        assert target.exists(), "unlink가 프로젝트 레포의 원본을 지웠다"
        assert not (root / "conf.d" / "30-service.toml").exists()


def test_unlink_refuses_non_symlink():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp))
        destination = root / "conf.d" / "30-service.toml"
        destination.write_text(_fragment("x"))
        try:
            link.unlink("service", root)
        except link.LinkError as exc:
            assert "심링크" in str(exc)
        else:
            raise AssertionError("unlink가 일반 파일을 지웠다")
        assert destination.exists()


def test_unlink_works_on_broken_link_without_loader():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp))
        destination = root / "conf.d" / "30-service.toml"
        os.symlink(Path(tmp) / "already-gone.toml", destination)

        link.unlink("service", root)

        assert not os.path.lexists(destination)


def test_link_refuses_on_basename_collision():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        first = base / "one" / "service.toml"
        second = base / "two" / "service.toml"
        first.parent.mkdir(); second.parent.mkdir()
        first.write_text(_fragment("one")); second.write_text(_fragment("two"))
        link.link(first, root)

        try:
            link.link(second, root)
        except link.LinkError as exc:
            assert "30-service.toml" in str(exc)
        else:
            raise AssertionError("같은 basename을 덮어썼다")
        assert (root / "conf.d" / "30-service.toml").resolve() == first.resolve()


def test_link_assigns_30_prefix_and_name_override():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))

        destination = link.link(target, root, name="project")

        assert destination.name == "30-project.toml"
        assert destination.is_symlink()
        assert destination.resolve() == target.resolve()


def test_link_creates_nothing_when_validation_fails():
    invalid_fragments = (
        "schema = 99\n",
        "schema = 1\nforbidden = true\n",
        _fragment("base"),
        _fragment("malformed", extra="env = 42\n"),
    )
    for invalid in invalid_fragments:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base, fragment=_fragment("base", port=8888))
            target = base / "candidate.toml"
            target.write_text(invalid)
            before = tuple((root / "conf.d").iterdir())

            try:
                link.link(target, root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("검증 실패 조각을 연결했다")
            assert tuple((root / "conf.d").iterdir()) == before


def test_cli_link_normalizes_malformed_field_type_without_traceback_or_symlink():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "malformed.toml"
        target.write_text(_fragment("malformed", extra="env = 42\n"))

        result = _run(root, "link", str(target))

        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert "items" in result.stderr or "검증" in result.stderr
        assert not os.path.lexists(root / "conf.d" / "30-malformed.toml")


def test_listing_uses_lstat_and_reports_service_counts():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "services.toml"
        target.write_text(
            _fragment("one") + "\n"
            + _fragment("two", port=9998).removeprefix("schema = 1\n")
        )
        link.link(target, root)
        broken = root / "conf.d" / "30-broken.toml"
        os.symlink(base / "gone.toml", broken)

        rows = link.listing(root)

        assert [(row.name, row.services, row.broken) for row in rows] == [
            ("broken", None, True), ("services", 2, False),
        ]


def test_unlink_warns_about_dropins_and_only_explicit_purge_removes_them():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))
        link.link(target, root)
        dropins = root / "conf.d" / "demo.d"
        dropins.mkdir()
        (dropins / "70-local.toml").write_text("schema = 1\nport = 7777\n")

        result = link.unlink("service", root)

        assert result.dropins == (dropins / "70-local.toml",)
        assert dropins.exists()

        link.link(target, root)
        link.unlink("service", root, purge_dropins=True)
        assert not dropins.exists()


def test_mask_writes_canonical_dropin_and_refuses_foreign_file():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))

        path = link.mask("demo", root)
        assert path.read_bytes() == link.MASK_CONTENT
        assert confd.load(root).by_name("demo").lifecycle == "masked"

        path.write_text("schema = 1\nmanaged = false\n# handwritten\n")
        try:
            link.mask("demo", root)
        except link.LinkError as exc:
            assert "예약" in str(exc) or "canonical" in str(exc)
        else:
            raise AssertionError("외부 90-mask.toml을 덮어썼다")
        assert "handwritten" in path.read_text()


def test_mask_does_not_treat_an_orphan_canonical_file_as_success():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp))
        mask_path = root / "conf.d" / "ghost.d" / "90-mask.toml"
        mask_path.parent.mkdir()
        mask_path.write_bytes(link.MASK_CONTENT)
        try:
            link.mask("ghost", root)
        except link.LinkError as exc:
            assert "그런 서비스" in str(exc)
        else:
            raise AssertionError("orphan canonical mask를 성공으로 처리했다")
        assert mask_path.read_bytes() == link.MASK_CONTENT


def test_unmask_only_removes_canonical_mask():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        path = link.mask("demo", root)
        link.unmask("demo", root)
        assert not path.exists()

        path.parent.mkdir(exist_ok=True)
        path.write_text("schema = 1\nmanaged = false\n# owner data\n")
        try:
            link.unmask("demo", root)
        except link.LinkError:
            pass
        else:
            raise AssertionError("외부 mask 파일을 삭제했다")
        assert path.exists()


def test_unmask_accepts_the_legacy_svc_canonical_mask_after_namespace_migration():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp), fragment=_fragment("demo"))
        dropin = root / "conf.d" / "demo.d"
        dropin.mkdir()
        mask_path = dropin / "90-mask.toml"
        mask_path.write_bytes(link.LEGACY_MASK_CONTENT)

        removed = link.unmask("demo", root)

        assert removed == mask_path
        assert not mask_path.exists()


def test_mask_refuses_foreign_symlink_and_uses_0644_for_canonical_file():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        mask_path = root / "conf.d" / "demo.d" / "90-mask.toml"
        mask_path.parent.mkdir()
        foreign = base / "foreign-mask.toml"
        foreign.write_bytes(link.MASK_CONTENT)
        os.symlink(foreign, mask_path)

        try:
            link.mask("demo", root)
        except link.LinkError:
            pass
        else:
            raise AssertionError("foreign symlink를 canonical mask로 받아들였다")
        assert mask_path.is_symlink() and foreign.read_bytes() == link.MASK_CONTENT

        mask_path.unlink()
        path = link.mask("demo", root)
        assert path.stat().st_mode & 0o777 == 0o644


def test_unmask_rejects_name_traversal_without_touching_neighbor():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        victim = root / "victim.d" / "90-mask.toml"
        victim.parent.mkdir()
        victim.write_bytes(link.MASK_CONTENT)

        try:
            link.unmask("../victim", root)
        except link.LinkError:
            pass
        else:
            raise AssertionError("name traversal로 config_root 이웃을 지웠다")
        assert victim.exists()


def test_purge_refuses_unsafe_dropin_tree_before_unlinking_fragment():
    for unsafe_kind in ("symlink-directory", "unexpected-file"):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base)
            target = base / "service.toml"
            target.write_text(_fragment("demo"))
            attached = link.link(target, root)
            dropins = root / "conf.d" / "demo.d"
            if unsafe_kind == "symlink-directory":
                external = base / "external"
                external.mkdir()
                (external / "70-local.toml").write_text("schema = 1\nport = 1\n")
                os.symlink(external, dropins)
            else:
                dropins.mkdir()
                (dropins / "README").write_text("owner data")

            try:
                link.unlink("service", root, purge_dropins=True)
            except link.LinkError:
                pass
            else:
                raise AssertionError(f"unsafe purge를 허용했다: {unsafe_kind}")
            assert attached.is_symlink(), "purge 검증 전에 fragment link를 지웠다"
            assert dropins.exists()


def test_link_prevalidation_matches_apply_policy_and_fatal_validation():
    invalid = (
        _fragment("demo", extra='group = "unknown"\n').replace(
            'group = "test"\n', ""
        ),
        _fragment("demo").replace('/bin/echo', '/missing/launcher'),
    )
    for fragment in invalid:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base)
            target = base / "service.toml"
            target.write_text(fragment)
            try:
                link.link(target, root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("apply가 거부할 조각을 link했다")
            assert not tuple((root / "conf.d").iterdir())


def test_link_prevalidates_orphan_dropins_that_candidate_would_activate():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        dropins = root / "conf.d" / "demo.d"
        dropins.mkdir()
        (dropins / "70-valid.toml").write_text("schema = 1\nport = 7777\n")
        (dropins / "90-invalid.toml").write_text("schema = 99\n")
        target = base / "service.toml"
        target.write_text(_fragment("demo"))

        try:
            link.link(target, root)
        except link.LinkError:
            pass
        else:
            raise AssertionError("candidate가 활성화할 invalid dropin을 놓쳤다")
        assert not os.path.lexists(root / "conf.d" / "30-service.toml")


def test_unlink_rejects_glob_and_invalid_prefixed_names():
    for unsafe in ("*", "?", "../service", "99-service", "09-service"):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base)
            target = base / "service.toml"
            target.write_text(_fragment("demo"))
            attached = link.link(target, root)
            try:
                link.unlink(unsafe, root)
            except link.LinkError:
                pass
            else:
                raise AssertionError(f"unsafe unlink name을 허용했다: {unsafe!r}")
            assert attached.is_symlink()


def test_broken_unlink_warns_conservatively_and_purge_refuses_unknown_scope():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("owned"))
        owned = root / "conf.d" / "owned.d"
        orphan = root / "conf.d" / "orphan.d"
        owned.mkdir(); orphan.mkdir()
        broken = root / "conf.d" / "30-broken.toml"
        os.symlink(base / "gone.toml", broken)

        try:
            link.unlink("broken", root, purge_dropins=True)
        except link.LinkError as exc:
            assert "범위" in str(exc) or "증명" in str(exc)
        else:
            raise AssertionError("broken target의 불명확한 dropin을 purge했다")
        assert broken.is_symlink() and orphan.exists() and owned.exists()

        result = link.unlink("broken", root)
        assert orphan in result.dropins
        assert owned not in result.dropins
        assert not os.path.lexists(broken)


def test_unlink_reports_an_empty_dropin_directory():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))
        link.link(target, root)
        empty = root / "conf.d" / "demo.d"
        empty.mkdir()

        result = link.unlink("service", root)

        assert result.dropins == (empty,)
        assert empty.exists()


def test_invalid_service_name_cannot_purge_outside_conf_d():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "malicious.toml"
        target.write_text(_fragment("../victim"))
        attached = root / "conf.d" / "30-malicious.toml"
        os.symlink(target, attached)
        victim = root / "victim.d"
        victim.mkdir()
        owner_data = victim / "70-local.toml"
        owner_data.write_text("OWNER DATA")

        try:
            link.unlink("malicious", root, purge_dropins=True)
        except link.LinkError:
            pass
        else:
            raise AssertionError("unsafe service name으로 conf.d 밖을 purge했다")
        assert attached.is_symlink()
        assert owner_data.read_text() == "OWNER DATA"


def test_mask_edit_unmask_refuse_symlink_dropin_parent():
    for operation in ("mask", "edit", "unmask"):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base, fragment=_fragment("demo"))
            external = base / "external"
            external.mkdir()
            canonical = external / "90-mask.toml"
            canonical.write_bytes(link.MASK_CONTENT)
            os.symlink(external, root / "conf.d" / "demo.d")
            try:
                if operation == "edit":
                    link.edit("demo", root, editor="/usr/bin/true")
                else:
                    getattr(link, operation)("demo", root)
            except (link.LinkError, confd.FragmentUnreadable):
                pass
            else:
                raise AssertionError(f"symlink dropin parent를 허용했다: {operation}")
            assert canonical.read_bytes() == link.MASK_CONTENT
            assert not (external / "70-local.toml").exists()


def test_unlink_atomic_quarantine_preserves_racing_owner_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))
        attached = link.link(target, root)
        real_rename = os.rename
        replaced = False

        def racing_rename(src, dst, *args, **kwargs):
            nonlocal replaced
            if not replaced and src == attached.name:
                replaced = True
                attached.unlink()
                attached.write_text("OWNER DATA")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(link.os, "rename", side_effect=racing_rename):
            try:
                link.unlink("service", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("racing regular replacement를 삭제했다")
        assert attached.read_text() == "OWNER DATA"


def test_quarantine_stat_failure_restores_fragment_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))
        attached = link.link(target, root)
        real_stat = os.stat
        injected = False

        def fail_quarantine_stat(path, *args, **kwargs):
            nonlocal injected
            if not injected and isinstance(path, str) and ".svc-unlink-" in path:
                injected = True
                raise OSError("injected stat failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(link.os, "stat", side_effect=fail_quarantine_stat):
            try:
                link.unlink("service", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("quarantine stat 실패를 성공 처리했다")
        assert attached.is_symlink() and attached.resolve() == target.resolve()


def test_unmask_quarantine_stat_failure_restores_canonical_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        mask_path = link.mask("demo", root)
        real_stat = os.stat
        injected = False

        def fail_quarantine_stat(path, *args, **kwargs):
            nonlocal injected
            if not injected and isinstance(path, str) and ".svc-unmask-" in path:
                injected = True
                raise OSError("injected stat failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(link.os, "stat", side_effect=fail_quarantine_stat):
            try:
                link.unmask("demo", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("unmask quarantine stat 실패를 성공 처리했다")
        assert mask_path.read_bytes() == link.MASK_CONTENT


def test_unmask_final_quarantine_stat_failure_restores_canonical_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        mask_path = link.mask("demo", root)
        real_stat = os.stat
        quarantine_stats = 0

        def fail_final_quarantine_stat(path, *args, **kwargs):
            nonlocal quarantine_stats
            if isinstance(path, str) and ".svc-unmask-" in path:
                quarantine_stats += 1
                if quarantine_stats == 2:
                    raise OSError("injected final stat failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(link.os, "stat", side_effect=fail_final_quarantine_stat):
            try:
                link.unmask("demo", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("final quarantine stat 실패를 성공 처리했다")
        assert mask_path.read_bytes() == link.MASK_CONTENT


def test_dropin_directory_open_rejects_directory_swap():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and path == "demo.d" and kwargs.get("dir_fd") is not None:
                swapped = True
                original = root / "conf.d" / "demo.d"
                moved = root / "conf.d" / "demo-old.d"
                original.rename(moved)
                original.mkdir()
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(link.os, "open", side_effect=swap_before_open):
            try:
                link.mask("demo", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("dropin directory swap을 허용했다")
        assert not (root / "conf.d" / "demo.d" / "90-mask.toml").exists()
        assert not (root / "conf.d" / "demo-old.d" / "90-mask.toml").exists()


def test_unlink_open_rejects_conf_dir_swap():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))
        attached = link.link(target, root)
        conf_dir = root / "conf.d"
        old_dir = root / "conf-old"
        foreign_target = base / "foreign.toml"
        foreign_target.write_text("OWNER DATA")
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == conf_dir:
                swapped = True
                conf_dir.rename(old_dir)
                conf_dir.mkdir()
                os.symlink(foreign_target, conf_dir / attached.name)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(link.os, "open", side_effect=swap_before_open):
            try:
                link.unlink("service", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("conf.d directory swap을 허용했다")
        preserved = old_dir / attached.name
        assert preserved.is_symlink() and preserved.resolve() == target.resolve()
        replacement = conf_dir / attached.name
        assert replacement.is_symlink() and replacement.resolve() == foreign_target.resolve()


def test_purge_rename_failure_rolls_back_fragment_and_all_dropins():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "services.toml"
        target.write_text(
            _fragment("one") + "\n"
            + _fragment("two", port=9998).removeprefix("schema = 1\n")
        )
        attached = link.link(target, root)
        dirs = (root / "conf.d" / "one.d", root / "conf.d" / "two.d")
        for directory in dirs:
            directory.mkdir()
            (directory / "70-local.toml").write_text("schema = 1\nport = 1\n")
        real_rename = os.rename
        calls = 0

        def fail_second_dropin(src, dst, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected rename failure")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(link.os, "rename", side_effect=fail_second_dropin):
            try:
                link.unlink("services", root, purge_dropins=True)
            except link.LinkError:
                pass
            else:
                raise AssertionError("부분 purge를 성공으로 처리했다")
        assert attached.is_symlink()
        assert all(directory.is_dir() for directory in dirs)


def test_purge_rollback_never_clobbers_racing_original_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "services.toml"
        target.write_text(
            _fragment("one") + "\n"
            + _fragment("two", port=9998).removeprefix("schema = 1\n")
        )
        attached = link.link(target, root)
        dirs = (root / "conf.d" / "one.d", root / "conf.d" / "two.d")
        for directory in dirs:
            directory.mkdir()
            (directory / "70-local.toml").write_text("schema = 1\nport = 1\n")
        real_rename = os.rename
        real_link = os.link
        calls = 0
        raced = False

        def fail_during_rename(src, dst, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected rename failure")
            return real_rename(src, dst, *args, **kwargs)

        def race_before_exclusive_restore(src, dst, *args, **kwargs):
            nonlocal raced
            if not raced and dst == attached.name and kwargs.get("dst_dir_fd") is not None:
                raced = True
                attached.write_text("OWNER DATA")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(link.os, "rename", side_effect=fail_during_rename), \
             mock.patch.object(link.os, "link", side_effect=race_before_exclusive_restore):
            try:
                link.unlink("services", root, purge_dropins=True)
            except link.LinkError:
                pass
            else:
                raise AssertionError("racing rollback을 성공 처리했다")
        assert attached.read_text() == "OWNER DATA"
        quarantines = tuple((root / "conf.d").glob(".30-services.toml.svc-unlink-*"))
        assert quarantines and quarantines[0].is_symlink()


def test_directory_rollback_open_failure_removes_empty_public_dir_and_closes_fd():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp))
        conf_dir = root / "conf.d"
        quarantine = ".demo.d.svc-unlink-test"
        quarantined = conf_dir / quarantine
        quarantined.mkdir()
        (quarantined / "70-local.toml").write_text("OWNER DATA")
        real_open = os.open
        opened: list[int] = []
        calls = 0

        def fail_second_open(path, flags, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected original open failure")
            fd = real_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        conf_fd = real_open(conf_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(link.os, "open", side_effect=fail_second_open):
                try:
                    link._restore_quarantined_directory(
                        conf_fd, quarantine, "demo.d"
                    )
                except OSError:
                    pass
                else:
                    raise AssertionError("rollback open 실패를 성공 처리했다")
        finally:
            os.close(conf_fd)
        assert not (conf_dir / "demo.d").exists()
        assert (quarantined / "70-local.toml").read_text() == "OWNER DATA"
        assert len(opened) == 1
        try:
            os.fstat(opened[0])
        except OSError:
            pass
        else:
            os.close(opened[0])
            raise AssertionError("rollback open 실패 뒤 fd가 누수됐다")


def test_mask_no_clobber_publish_preserves_racing_foreign_file():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        final = root / "conf.d" / "demo.d" / "90-mask.toml"
        real_link = os.link

        def racing_link(src, dst, *args, **kwargs):
            final.write_text("OWNER DATA")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(link.os, "link", side_effect=racing_link):
            try:
                link.mask("demo", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("racing foreign mask를 덮어썼다")
        assert final.read_text() == "OWNER DATA"


def test_mask_write_failure_cleans_temp_final_and_new_dropin_directory():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        directory = root / "conf.d" / "demo.d"
        with mock.patch.object(link.os, "write", side_effect=OSError("injected")):
            try:
                link.mask("demo", root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("mask write 실패를 성공 처리했다")
        assert not directory.exists()
        assert not tuple((root / "conf.d").glob(".90-mask.toml.svc-*"))


def test_open_conf_dir_fstat_failure_closes_fd_and_normalizes_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp))
        captured: list[int] = []
        real_open = os.open

        def capture_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            captured.append(fd)
            return fd

        with mock.patch.object(link.os, "open", side_effect=capture_open), \
             mock.patch.object(link.os, "fstat", side_effect=OSError("injected")):
            try:
                link._open_conf_dir(root)
            except link.LinkError:
                pass
            else:
                raise AssertionError("conf.d fstat 실패를 성공 처리했다")
        assert len(captured) == 1
        try:
            os.fstat(captured[0])
        except OSError:
            pass
        else:
            os.close(captured[0])
            raise AssertionError("fstat 실패 뒤 conf.d fd가 누수됐다")


def test_edit_validates_editor_before_creating_template():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        target = root / "conf.d" / "demo.d" / "70-local.toml"
        try:
            link.edit("demo", root, editor="'")
        except link.LinkError:
            pass
        else:
            raise AssertionError("malformed EDITOR를 허용했다")
        assert not target.exists()


def test_cli_controlled_link_errors_keep_their_actionable_reason():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(Path(tmp), fragment=_fragment("demo"))
        cases = (
            (("edit", "demo"), dict(os.environ, EDITOR="'"), "$EDITOR"),
            (("cat", "ghost"), None, "그런 서비스"),
            (("mask", "ghost"), None, "그런 서비스"),
            (("link",), None, "path가 필요"),
        )
        for argv, env, expected in cases:
            result = _run(root, *argv, env=env)
            assert result.returncode == 2
            assert expected in result.stderr, (argv, result.stderr)
            assert "Traceback" not in result.stderr


def test_cat_and_link_do_not_leak_raw_values_or_tracebacks():
    invalids = (
        'schema = "SUPER_SECRET_RAW"\n',
        _fragment("demo").replace("port = 9999", 'port = "SUPER_SECRET_RAW"'),
    )
    for invalid in invalids:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base, fragment=invalid)
            cat_result = _run(root, "cat", "demo")
            assert cat_result.returncode == 2
            assert "SUPER_SECRET_RAW" not in cat_result.stderr
            assert "Traceback" not in cat_result.stderr

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(base)
            candidate = base / "candidate.toml"
            candidate.write_text(invalid)
            linked = _run(root, "link", str(candidate))
            assert linked.returncode == 2
            assert "SUPER_SECRET_RAW" not in linked.stderr
            assert "Traceback" not in linked.stderr
            assert not os.path.lexists(root / "conf.d" / "30-candidate.toml")


def test_edit_and_mask_malformed_existing_config_are_clean_failures():
    for command in (("edit", "demo"), ("mask", "demo")):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _root(
                base,
                fragment=_fragment("demo").replace(
                    "port = 9999", 'port = "SUPER_SECRET_RAW"'
                ),
            )
            env = dict(os.environ, EDITOR="/usr/bin/true")
            result = _run(root, *command, env=env)
            assert result.returncode == 2
            assert "Traceback" not in result.stderr
            assert "SUPER_SECRET_RAW" not in result.stderr
            assert not (root / "conf.d" / "demo.d").exists()


def test_cat_provenance_uses_model_not_same_basename_filesystem_probe():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        fragment = root / "conf.d" / "30-base.toml"
        fragment.write_text(_fragment("demo"))
        same_name = root / "conf.d" / "demo.d" / "30-base.toml"
        same_name.parent.mkdir()
        same_name.write_text("schema = 1\n")
        merged = confd.load(root)

        output = link.cat(merged, root, "demo")

        name_header_index = output.index(str(fragment))
        assert name_header_index >= 0
        assert str(same_name) not in output


def test_same_basename_fragment_and_dropin_keep_distinct_provenance_locations():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        fragment = root / "conf.d" / "70-local.toml"
        fragment.write_text(_fragment("demo", port=7000))
        dropin = root / "conf.d" / "demo.d" / "70-local.toml"
        dropin.parent.mkdir()
        dropin.write_text("schema = 1\nport = 7778\n")

        merged = confd.load(root)
        service = merged.by_name("demo")
        assert service.source_of("name") == "70-local.toml"
        assert service.source_of("port") == "70-local.toml"
        assert service.location_of("name") == str(fragment)
        assert service.location_of("port") == str(dropin)

        output = link.cat(merged, root, "demo")
        assert str(fragment) in output
        assert str(dropin) in output
        assert output.index(str(fragment)) < output.index(str(dropin))
        summary = doctor.override_summary(merged)
        port = next(row for row in summary if row.field == "port")
        assert port.winner == str(dropin)
        assert port.overridden == (str(fragment),)


def test_cat_prints_effective_value_and_winning_location_without_raw_excerpt():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo", port=7000))
        dropins = root / "conf.d" / "demo.d"
        dropins.mkdir()
        (dropins / "70-local.toml").write_text("schema = 1\nport = 7778\n")

        result = _run(root, "cat", "demo")

        assert result.returncode == 0, result.stderr
        assert str(root / "conf.d" / "demo.d" / "70-local.toml") in result.stdout
        assert "port = 7778" in result.stdout
        assert "30-base.toml:port" in result.stdout
        assert str(root / "conf.d" / "macosctl.toml") not in result.stdout
        assert result.stdout.count(str(root / "macosctl.toml")) == 1
        assert result.stdout.count("working_directory =") == 1
        assert "mem_budget = # unset" not in result.stdout

        (root / "conf.d" / "30-base.toml").write_text(
            'schema = 1\n[[service]]\nname = "SUPER_SECRET_RAW"\ninvalid = [\n'
        )
        failed = _run(root, "cat", "demo")
        assert failed.returncode != 0
        assert "SUPER_SECRET_RAW" not in failed.stderr
        assert "30-base.toml" in failed.stderr


def test_cat_refuses_root_before_reading_fragments():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_cat"]
    with mock.patch.object(namespace["os"], "geteuid", return_value=0), \
         mock.patch.object(namespace["confd"], "load", side_effect=AssertionError("read")):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = command(SimpleNamespace(config_root=Path("/etc/macosctl"), name="demo"))
    assert code == 2
    assert "비특권" in output.getvalue()


def test_edit_creates_70_local_template_and_invokes_editor():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("demo"))
        env = dict(os.environ, EDITOR="/usr/bin/true")

        result = _run(root, "edit", "demo", env=env)

        path = root / "conf.d" / "demo.d" / "70-local.toml"
        assert result.returncode == 0, result.stderr
        assert path.read_text().startswith("# demo 로컬 오버라이드")
        assert "schema = 1" in path.read_text()
        assert "sudo macosctl apply" in result.stdout


def test_edit_rejects_abnormal_config_name_before_building_a_path():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base, fragment=_fragment("../victim"))
        escaped = root / "victim.d" / "70-local.toml"
        try:
            link.edit("../victim", root, editor="/usr/bin/true")
        except link.LinkError:
            pass
        else:
            raise AssertionError("비정상 service name으로 conf.d 밖을 편집했다")
        assert not escaped.exists()


def test_cli_link_list_unlink_and_mask_commands_print_apply_guidance():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = _root(base)
        target = base / "service.toml"
        target.write_text(_fragment("demo"))

        attached = _run(root, "link", str(target))
        assert attached.returncode == 0, attached.stderr
        assert "sudo macosctl apply" in attached.stdout
        listed = _run(root, "link", "--list")
        assert listed.returncode == 0
        assert "service" in listed.stdout and "1" in listed.stdout
        masked = _run(root, "mask", "demo")
        assert masked.returncode == 0 and "sudo macosctl apply" in masked.stdout
        unmasked = _run(root, "unmask", "demo")
        assert unmasked.returncode == 0 and "sudo macosctl apply" in unmasked.stdout
        detached = _run(root, "unlink", "service")
        assert detached.returncode == 0 and "sudo macosctl apply" in detached.stdout


def test_status_distinguishes_masked_from_project_unmanaged():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_status"]
    defaults = confd._defaults(  # noqa: SLF001 - fixture uses the production parser
        {"schema": 1, "defaults": {}}, Path("macosctl.toml")
    )
    active = confd._service(  # noqa: SLF001
        {"name": "off", "label": "com.korellas.off", "port": 1,
         "managed": False, "exec": ["/bin/true"]}, defaults, "30-a.toml"
    )
    masked = confd._dropin(  # noqa: SLF001
        confd._service(
            {"name": "masked", "label": "com.korellas.masked", "port": 2,
             "exec": ["/bin/true"]}, defaults, "30-b.toml"
        ),
        {"schema": 1, "managed": False}, Path("90-mask.toml"), defaults,
    )
    merged = SimpleNamespace(services=(active, masked))
    state = collect.SystemState(
        installed_labels=frozenset(), jobs={}, listeners=(), ppids={},
        disabled_overrides={},
    )
    with mock.patch.object(namespace["confd"], "load", return_value=merged), \
         mock.patch.dict(command.__globals__, {"_state": lambda services: state}):
        output = io.StringIO()
        with redirect_stdout(output):
            assert command(SimpleNamespace(config_root=Path("/etc/macosctl"))) == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    masked_line = next(line for line in plain.splitlines() if "masked" in line)
    off_line = next(line for line in plain.splitlines() if "off" in line)
    assert "◎ MASKED" in masked_line
    assert "◇ EXTERNAL" in off_line


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
