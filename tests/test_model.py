"""불변 병합 모델 단위 테스트."""

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import model  # noqa: E402
from macosctl.manifest import Defaults  # noqa: E402


def _service(**over):
    base = dict(
        name="webtop",
        label="com.webtop",
        port=7778,
        group="dashboard",
        managed=True,
        exec_argv=("/bin/echo",),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/tmp",
        lifecycle="active",
        sources=(),
    )
    base.update(over)
    return model.MergedService(**base)


def _defaults():
    return Defaults(
        user="example",
        working_directory="/tmp",
        log_dir="/tmp/logs",
        throttle_seconds=10,
        path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )


def test_source_of_points_at_the_last_writer():
    svc = _service(
        sources=(
            model.Provenance("port", "30-webtop.toml"),
            model.Provenance("port", "70-port.toml"),
        ),
    )
    assert svc.source_of("port") == "70-port.toml"
    assert svc.source_of("group") is None


def test_service_is_frozen_and_normalizes_collections_to_tuples():
    svc = _service(
        exec_argv=["/bin/echo", "hello"],
        depends_on=["database"],
        env=[["PORT", "7778"]],
        sources=[model.Provenance("port", "30-webtop.toml")],
    )

    assert svc.exec_argv == ("/bin/echo", "hello")
    assert isinstance(svc.exec_argv, tuple)
    assert svc.depends_on == ("database",)
    assert svc.env == (("PORT", "7778"),)
    assert svc.sources == (model.Provenance("port", "30-webtop.toml"),)

    try:
        svc.port = 9999
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("MergedService must be frozen")


def test_model_finds_service_by_name():
    webtop = _service()
    merged = model.MergedModel(
        defaults=_defaults(), services=(webtop,), warnings=()
    )

    assert merged.by_name("webtop") is webtop
    assert merged.by_name("missing") is None


def test_model_returns_only_managed_services():
    managed = _service()
    unmanaged = _service(
        name="manual",
        label="com.korellas.manual",
        managed=False,
    )
    merged = model.MergedModel(
        defaults=_defaults(), services=(managed, unmanaged), warnings=()
    )

    assert merged.managed_only() == (managed,)


def test_merge_error_exposes_human_readable_detail():
    error = model.MergeError("port was defined twice")

    assert error.detail == "port was defined twice"
    assert str(error) == "port was defined twice"


def test_provenance_and_merged_model_are_frozen_and_collections_are_tuples():
    provenance = model.Provenance("port", "30-webtop.toml")
    merged = model.MergedModel(
        defaults=_defaults(), services=[_service()], warnings=["orphan drop-in"]
    )

    assert isinstance(merged.services, tuple)
    assert merged.warnings == ("orphan drop-in",)

    for instance, field, value in (
        (provenance, "source", "70-local.toml"),
        (merged, "warnings", ()),
    ):
        try:
            setattr(instance, field, value)
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError(f"{type(instance).__name__} must be frozen")


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
