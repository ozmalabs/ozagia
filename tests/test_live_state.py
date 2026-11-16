import json
from datetime import datetime, timedelta, timezone

import pytest

from ozagia.core.live import (
    MAX_DOCUMENT_BYTES,
    LiveSnapshot,
    SnapshotError,
    diff_snapshots,
    import_snapshot,
    is_stale,
    read_snapshot,
    snapshot_to_dict,
    write_snapshot,
)


def document(**updates):
    value = {
        "schema_version": 1,
        "observed_at": "2026-11-16T10:00:00Z",
        "environment": "staging",
        "service": "api",
        "region": "us-east-1",
        "deployed_images": [{"name": "example/api", "tag": "v2"}],
        "desired_replicas": 3,
        "ready_replicas": 2,
        "target_health": {"status": "degraded", "healthy": 2, "unhealthy": 1, "total": 3},
        "last_pipeline": {
            "name": "deploy-api",
            "status": "succeeded",
            "at": "2026-11-16T09:55:00+00:00",
        },
    }
    value.update(updates)
    return value


def imported(value=None, *, provenance="ci-artifact"):
    return import_snapshot(json.dumps(value or document()), provenance=provenance)


def test_imports_allowlisted_snapshot_and_assigns_provenance():
    snapshot = imported()

    assert isinstance(snapshot, LiveSnapshot)
    assert snapshot.provenance == "ci-artifact"
    assert snapshot.environment == "staging"
    assert snapshot.deployed_images[0].tag == "v2"


def test_rejects_unknown_fields_at_every_level_without_echoing_values():
    value = document(secret_canary="NEVER-RETURN-THIS")
    with pytest.raises(SnapshotError) as error:
        imported(value)
    assert "NEVER-RETURN-THIS" not in str(error.value)

    value = document(target_health={"status": "healthy", "metadata": "canary"})
    with pytest.raises(SnapshotError):
        imported(value)


def test_rejects_duplicate_json_keys():
    text = (
        '{"schema_version":1,"schema_version":1,"observed_at":"2026-11-16T10:00:00Z",'
        '"environment":"staging","service":"api","deployed_images":[],"desired_replicas":0,'
        '"ready_replicas":0,"target_health":{"status":"healthy"}}'
    )
    with pytest.raises(SnapshotError, match="duplicate"):
        import_snapshot(text, provenance="artifact")


def test_rejects_document_size_and_json_depth_limits():
    with pytest.raises(SnapshotError, match="size"):
        import_snapshot(" " * (MAX_DOCUMENT_BYTES + 1), provenance="artifact")
    too_deep = '{"a":' * 10 + "0" + "}" * 10
    with pytest.raises(SnapshotError, match="nesting"):
        import_snapshot(too_deep, provenance="artifact")


@pytest.mark.parametrize(
    "timestamp",
    ["yesterday", "2026-11-16T10:00:00", "2026-02-30T10:00:00Z"],
)
def test_rejects_invalid_or_timezone_naive_timestamp(timestamp):
    with pytest.raises(SnapshotError):
        imported(document(observed_at=timestamp))


def test_diff_reports_changed_missing_and_added_keys_without_values():
    old = [
        imported(document(service="api")),
        imported(document(service="worker")),
    ]
    new = [
        imported(document(service="api", ready_replicas=3)),
        imported(document(environment="production", service="api")),
    ]

    differences = diff_snapshots(old, new)
    assert [(item.environment, item.service, item.change) for item in differences] == [
        ("production", "api", "added"),
        ("staging", "api", "changed"),
        ("staging", "worker", "missing"),
    ]
    changed = next(item for item in differences if item.change == "changed")
    assert changed.changed_fields == ("ready_replicas",)
    assert "v2" not in repr(differences)


def test_diff_output_does_not_reveal_rejected_canary_or_field_values():
    first = imported(document())
    second = imported(document(deployed_images=[{"name": "example/api", "tag": "new"}]))

    diff = diff_snapshots(first, second)
    assert "new" not in repr(diff)
    assert "example/api" not in repr(diff)


def test_staleness_uses_timezone_aware_now_and_ttl():
    snapshot = imported()
    assert is_stale(
        snapshot,
        now=datetime(2026, 11, 16, 10, 5, tzinfo=timezone.utc),
        ttl=timedelta(minutes=4),
    )
    assert not is_stale(
        snapshot,
        now=datetime(2026, 11, 16, 10, 5, tzinfo=timezone.utc),
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(ValueError):
        is_stale(snapshot, now=datetime.now(), ttl=timedelta(minutes=1))


def test_snapshot_round_trip_uses_allowlisted_fields_and_safe_name(tmp_path):
    snapshot = imported()
    path = write_snapshot(tmp_path / "live", snapshot)
    assert path.name == "staging.api.json"
    restored = read_snapshot(path, provenance="reimported")
    assert restored.provenance == "reimported"
    assert snapshot_to_dict(restored) == snapshot_to_dict(snapshot)
    assert snapshot_to_dict(snapshot)["service"] == "api"
    with pytest.raises(SnapshotError, match="regular file"):
        read_snapshot(tmp_path / "missing.json", provenance="x")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(path)
    with pytest.raises(SnapshotError, match="regular file"):
        read_snapshot(symlink, provenance="x")
