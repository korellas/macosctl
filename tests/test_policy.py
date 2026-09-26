"""Root-owned svc policy reader tests (D2)."""

import json
import os
import sys
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import policy  # noqa: E402
from macosctl import model  # noqa: E402
from macosctl.manifest import Defaults  # noqa: E402


VALID_POLICY = {
    "schema": 1,
    "label_prefix": "com.korellas.",
    "label_exceptions": {"webtop": "com.webtop"},
    "service_user": "example",
    "groups": ["infra", "dashboard"],
}


def _policy_file(tmp: str) -> Path:
    root = Path(tmp) / "svc"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    path = root / "policy.json"
    path.write_text(json.dumps(VALID_POLICY))
    path.chmod(0o644)
    return path


def _read_as_current_owner(path: Path):
    return policy._read(path, expected_uid=os.getuid(), expected_gid=os.getgid())


def _merged_service(**over):
    fields = dict(
        name="demo",
        label="com.korellas.demo",
        port=9000,
        group="infra",
        managed=True,
        exec_argv=("/bin/echo",),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/tmp",
        lifecycle="active",
        sources=(),
    )
    fields.update(over)
    return model.MergedService(**fields)


def _merged(*services):
    defaults = Defaults(
        user=None,
        working_directory="/tmp",
        log_dir="/tmp/logs",
        throttle_seconds=10,
        path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )
    return model.MergedModel(defaults, services, ())


def test_label_prefix_pattern_accepts_reverse_dns_prefix():
    """The service-name pattern cannot validate a dotted label prefix."""
    assert policy.LABEL_PREFIX_PATTERN.match("com.korellas.")
    assert policy.LABEL_PATTERN.match("com.webtop")
    assert not policy.LABEL_PREFIX_PATTERN.match("com.korellas")
    assert not policy.LABEL_PREFIX_PATTERN.match("../etc/")


def test_parse_returns_a_frozen_policy_with_immutable_collections():
    parsed = policy._parse(json.dumps(VALID_POLICY).encode(), Path("policy.json"))

    assert parsed.label_prefix == "com.korellas."
    assert parsed.label_exceptions["webtop"] == "com.webtop"
    assert parsed.service_user == "example"
    assert dict(parsed.service_users) == {}
    assert parsed.groups == ("infra", "dashboard")

    try:
        parsed.service_user = "root"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("Policy must be frozen")

    try:
        parsed.label_exceptions["demo"] = "com.korellas.demo"
    except TypeError:
        pass
    else:
        raise AssertionError("label_exceptions must be immutable")


def test_policy_constructor_defensively_freezes_nested_values():
    exceptions = {"webtop": "com.webtop"}
    groups = ["infra"]
    parsed = policy.Policy("com.korellas.", exceptions, "example", groups)
    exceptions["demo"] = "com.korellas.demo"
    groups.append("dashboard")

    assert dict(parsed.label_exceptions) == {"webtop": "com.webtop"}
    assert parsed.groups == ("infra",)
    try:
        parsed.label_exceptions["demo"] = "com.korellas.demo"
    except TypeError:
        pass
    else:
        raise AssertionError("Policy constructor retained a mutable mapping")


def test_parse_accepts_and_freezes_exact_service_user_mapping():
    candidate = dict(VALID_POLICY)
    candidate["service_users"] = {
        "public-web": "_public_web",
        "render-worker": "_render",
    }

    parsed = policy._parse(json.dumps(candidate).encode(), Path("policy.json"))
    candidate["service_users"]["public-web"] = "attacker"

    assert dict(parsed.service_users) == {
        "public-web": "_public_web",
        "render-worker": "_render",
    }
    try:
        parsed.service_users["secret-web"] = "_secret_web"
    except TypeError:
        pass
    else:
        raise AssertionError("service_users must be immutable")


def _assert_parse_refused(value, expected):
    try:
        policy._parse(json.dumps(value).encode(), Path("policy.json"))
    except policy.PolicyRefused as exc:
        assert expected in exc.detail, exc.detail
    else:
        raise AssertionError(f"policy was accepted: {value!r}")


def test_parse_refuses_malformed_json_schema_and_missing_fields():
    try:
        policy._parse(b"{", Path("policy.json"))
    except policy.PolicyRefused as exc:
        assert "JSON" in exc.detail
    else:
        raise AssertionError("malformed JSON was accepted")

    for schema in (None, True, 2, "1"):
        candidate = dict(VALID_POLICY)
        candidate["schema"] = schema
        _assert_parse_refused(candidate, "schema")

    for field in ("label_prefix", "label_exceptions", "service_user", "groups"):
        candidate = dict(VALID_POLICY)
        candidate.pop(field)
        _assert_parse_refused(candidate, field)


def test_parse_validates_all_identity_and_group_values():
    invalid_values = (
        ("label_prefix", "com.korellas", "label_prefix"),
        ("label_prefix", "../etc/", "label_prefix"),
        ("label_exceptions", [], "label_exceptions"),
        ("label_exceptions", {"Bad Name": "com.valid"}, "label_exceptions"),
        ("label_exceptions", {"demo": "not-a-label"}, "label_exceptions"),
        ("service_user", "root user", "service_user"),
        ("service_user", "", "service_user"),
        ("groups", "infra", "groups"),
        ("groups", ["infra", "Bad Group"], "groups"),
        ("groups", ["infra", "infra"], "groups"),
        ("service_users", [], "service_users"),
        ("service_users", {"Bad Name": "_render"}, "서비스 이름"),
        ("service_users", {"render": "Bad User"}, "사용자"),
        ("service_users", {"render": 501}, "사용자"),
    )
    for field, value, expected in invalid_values:
        candidate = dict(VALID_POLICY)
        candidate[field] = value
        _assert_parse_refused(candidate, expected)


def test_resolve_label_uses_exception_then_prefix_and_rejects_invalid_name():
    parsed = policy._parse(json.dumps(VALID_POLICY).encode(), Path("policy.json"))

    assert policy.resolve_label("webtop", parsed) == "com.webtop"
    assert policy.resolve_label("demo-2", parsed) == "com.korellas.demo-2"
    assert policy.resolve_label("../etc", parsed) is None
    assert policy.resolve_label("---", parsed) is None
    assert policy.resolve_label("A" * 41, parsed) is None


def test_read_accepts_a_secure_regular_file_and_closes_its_fd():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        opened_fds = []
        opened_policy_fds = []
        read_fds = []
        real_open = os.open
        real_read = os.read

        def recording_open(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            opened_fds.append(fd)
            if target == path.name and kwargs.get("dir_fd") is not None:
                opened_policy_fds.append(fd)
            return fd

        def recording_read(fd, size):
            read_fds.append(fd)
            return real_read(fd, size)

        original = policy.os.open
        original_read = policy.os.read
        policy.os.open = recording_open
        policy.os.read = recording_read
        try:
            parsed = _read_as_current_owner(path)
        finally:
            policy.os.open = original
            policy.os.read = original_read

        assert parsed.service_user == "example"
        assert len(opened_policy_fds) == 1
        assert read_fds and set(read_fds) == set(opened_policy_fds)
        for fd in opened_fds:
            try:
                os.fstat(fd)
            except OSError:
                pass
            else:
                raise AssertionError(f"fd was not closed: {fd}")


def test_refuses_when_policy_missing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "svc"
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        try:
            _read_as_current_owner(root / "policy.json")
        except policy.PolicyRefused as exc:
            assert "정책이 없다" in exc.detail
        else:
            raise AssertionError("missing policy was accepted")


def test_refuses_group_or_other_writable():
    for mode in (0o664, 0o646):
        with tempfile.TemporaryDirectory() as tmp:
            path = _policy_file(tmp)
            path.chmod(mode)
            try:
                _read_as_current_owner(path)
            except policy.PolicyRefused as exc:
                assert "group/other" in exc.detail
            else:
                raise AssertionError(f"writable policy was accepted: {oct(mode)}")


def test_policy_file_mode_must_be_exactly_0644():
    for mode in (0o600, 0o640, 0o755):
        with tempfile.TemporaryDirectory() as tmp:
            path = _policy_file(tmp)
            path.chmod(mode)
            try:
                _read_as_current_owner(path)
            except policy.PolicyRefused as exc:
                assert "0644" in exc.detail and "권한" in exc.detail
            else:
                raise AssertionError(f"non-0644 policy was accepted: {oct(mode)}")

    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        path.chmod(0o644)
        assert _read_as_current_owner(path).service_user == "example"


def test_staging_reader_accepts_only_caller_owned_secure_layout():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        assert policy.read_staging(path).service_user == "example"

        path.chmod(0o600)
        try:
            policy.read_staging(path)
        except policy.PolicyRefused as exc:
            assert "0644" in exc.detail
        else:
            raise AssertionError("staging reader weakened the exact file mode")


def test_staging_reader_accepts_tmp_inherited_gid_outside_primary_group():
    """macOS /tmp children inherit wheel even though the caller's primary gid is staff."""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        path = _policy_file(tmp)
        assert path.parent.stat().st_uid == os.getuid()
        assert path.parent.stat().st_gid != os.getgid()

        parsed = policy.read_staging(path)

        assert parsed.service_user == "example"


def test_refuses_when_not_owned_by_root_via_expected_uid_injection():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        try:
            policy._read(
                path,
                expected_uid=os.getuid() + 1,
                expected_gid=os.getgid(),
                expected_directory_uid=os.getuid(),
                expected_directory_gid=os.getgid(),
            )
        except policy.PolicyRefused as exc:
            assert str(path) in exc.detail
            assert "설정 디렉터리" not in exc.detail
            assert "소유" in exc.detail and "uid=" in exc.detail
        else:
            raise AssertionError("policy with unexpected uid was accepted")


def test_reads_policy_through_one_relative_open_without_reopening_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        calls = []
        real_open = os.open

        def recording_open(target, flags, *args, **kwargs):
            calls.append((target, flags, kwargs.get("dir_fd")))
            return real_open(target, flags, *args, **kwargs)

        original = policy.os.open
        policy.os.open = recording_open
        try:
            _read_as_current_owner(path)
        finally:
            policy.os.open = original

        policy_calls = [call for call in calls if call[0] == "policy.json"]
        assert len(policy_calls) == 1, calls
        assert policy_calls[0][2] is not None, calls
        assert all(call[0] != str(path) for call in calls), calls


def test_refuses_symlinked_policy():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        target = path.with_name("real-policy.json")
        path.rename(target)
        path.symlink_to(target)
        try:
            _read_as_current_owner(path)
        except policy.PolicyRefused as exc:
            assert "열 수 없다" in exc.detail
        else:
            raise AssertionError("symlinked policy was accepted")


def test_refuses_insecure_policy_gid_link_count_size_and_file_type():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        for kwargs, expected in (
            ({"expected_gid": os.getgid() + 1}, "gid="),
            ({"expected_uid": os.getuid() + 1}, "uid="),
        ):
            options = {"expected_uid": os.getuid(), "expected_gid": os.getgid()}
            options.update(kwargs)
            options.update(
                expected_directory_uid=os.getuid(),
                expected_directory_gid=os.getgid(),
            )
            try:
                policy._read(path, **options)
            except policy.PolicyRefused as exc:
                assert expected in exc.detail
            else:
                raise AssertionError(f"metadata mismatch accepted: {kwargs}")

        hardlink = path.with_name("policy-hardlink.json")
        os.link(path, hardlink)
        try:
            _read_as_current_owner(path)
        except policy.PolicyRefused as exc:
            assert "link count" in exc.detail
        else:
            raise AssertionError("multiply linked policy was accepted")

    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        path.write_bytes(b" " * (policy.MAX_POLICY_BYTES + 1))
        try:
            _read_as_current_owner(path)
        except policy.PolicyRefused as exc:
            assert "너무 크다" in exc.detail
        else:
            raise AssertionError("oversized policy was accepted")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "svc"
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        path = root / "policy.json"
        path.mkdir(mode=0o755)
        try:
            _read_as_current_owner(path)
        except policy.PolicyRefused as exc:
            assert "일반 파일" in exc.detail
        else:
            raise AssertionError("directory policy was accepted")


def test_refuses_insecure_or_symlinked_policy_directory():
    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        path.parent.chmod(0o775)
        try:
            _read_as_current_owner(path)
        except policy.PolicyRefused as exc:
            assert "/etc/macosctl" in exc.detail or "설정 디렉터리" in exc.detail
        else:
            raise AssertionError("writable policy directory was accepted")

    with tempfile.TemporaryDirectory() as tmp:
        path = _policy_file(tmp)
        for kwargs, expected in (
            ({"expected_directory_uid": os.getuid() + 1}, "uid="),
            ({"expected_directory_gid": os.getgid() + 1}, "gid="),
        ):
            options = {"expected_uid": os.getuid(), "expected_gid": os.getgid()}
            options.update(kwargs)
            try:
                policy._read(path, **options)
            except policy.PolicyRefused as exc:
                assert "설정 디렉터리" in exc.detail
                assert expected in exc.detail
            else:
                raise AssertionError(f"directory metadata mismatch accepted: {kwargs}")

    with tempfile.TemporaryDirectory() as tmp:
        target_root = Path(tmp) / "real-svc"
        target_root.mkdir(mode=0o755)
        target_root.chmod(0o755)
        target_path = target_root / "policy.json"
        target_path.write_text(json.dumps(VALID_POLICY))
        target_path.chmod(0o644)
        linked_root = Path(tmp) / "svc"
        linked_root.symlink_to(target_root, target_is_directory=True)
        try:
            _read_as_current_owner(linked_root / "policy.json")
        except policy.PolicyRefused as exc:
            assert "디렉터리" in exc.detail
        else:
            raise AssertionError("symlinked policy directory was accepted")


def test_bind_supplies_service_user_and_checks_labels_and_groups():
    parsed = policy._parse(json.dumps(VALID_POLICY).encode(), Path("policy.json"))
    merged = _merged(
        _merged_service(),
        _merged_service(
            name="webtop",
            label="com.webtop",
            group="dashboard",
        ),
    )

    bound = policy.bind(merged, parsed)

    assert bound.defaults.user == "example"
    assert bound.services == merged.services
    assert merged.defaults.user is None


def test_bind_authorizes_exact_service_user_and_keeps_global_fallback():
    candidate = dict(VALID_POLICY)
    candidate["service_users"] = {"demo": "_render"}
    parsed = policy._parse(json.dumps(candidate).encode(), Path("policy.json"))
    merged = _merged(
        _merged_service(user="_render"),
        _merged_service(
            name="webtop",
            label="com.webtop",
            group="dashboard",
        ),
    )

    bound = policy.bind(merged, parsed)

    assert bound.defaults.user == "example"
    assert bound.by_name("demo").user == "_render"
    assert bound.by_name("webtop").user is None
    assert merged.by_name("demo").user == "_render"
    assert merged.by_name("webtop").user is None


def test_bind_refuses_missing_or_mismatched_service_user_authorization():
    cases = (
        ({}, "_render", ("demo", "_render", "허용")),
        ({"demo": "_render"}, "attacker", ("demo", "attacker", "_render")),
    )
    for service_users, requested_user, expected_parts in cases:
        candidate = dict(VALID_POLICY)
        candidate["service_users"] = service_users
        parsed = policy._parse(json.dumps(candidate).encode(), Path("policy.json"))
        try:
            policy.bind(_merged(_merged_service(user=requested_user)), parsed)
        except policy.PolicyRefused as exc:
            assert all(part in exc.detail for part in expected_parts), exc.detail
        else:
            raise AssertionError((service_users, requested_user))


def test_bind_refuses_malformed_requested_service_user():
    candidate = dict(VALID_POLICY)
    candidate["service_users"] = {"demo": "_render"}
    parsed = policy._parse(json.dumps(candidate).encode(), Path("policy.json"))
    for requested_user in (501, "", "Bad User"):
        try:
            policy.bind(_merged(_merged_service(user=requested_user)), parsed)
        except policy.PolicyRefused as exc:
            assert "demo" in exc.detail and "user" in exc.detail, exc.detail
        else:
            raise AssertionError(f"malformed service user accepted: {requested_user!r}")


def test_bind_refuses_policy_authorization_for_unknown_service():
    candidate = dict(VALID_POLICY)
    candidate["service_users"] = {"missing": "_render"}
    parsed = policy._parse(json.dumps(candidate).encode(), Path("policy.json"))

    try:
        policy.bind(_merged(_merged_service()), parsed)
    except policy.PolicyRefused as exc:
        assert "missing" in exc.detail and "서비스" in exc.detail, exc.detail
    else:
        raise AssertionError("authorization for an unknown service was accepted")


def test_bind_refuses_label_or_group_disagreement_before_apply():
    parsed = policy._parse(json.dumps(VALID_POLICY).encode(), Path("policy.json"))
    invalid_services = (
        (_merged_service(label="com.korellas.someone-else"), "label"),
        (_merged_service(name="Bad Name"), "name"),
        (_merged_service(group="unknown"), "group"),
    )
    for service, expected in invalid_services:
        try:
            policy.bind(_merged(service), parsed)
        except policy.PolicyRefused as exc:
            assert expected in exc.detail, exc.detail
        else:
            raise AssertionError(f"policy disagreement was accepted: {service}")


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
