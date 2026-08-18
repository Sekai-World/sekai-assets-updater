from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from asset_bundle_info import normalize_asset_bundle_info
import state


def _queue():
    return [["https://example.test/a", {"bundleName": "a", "hash": "h", "fileSize": 1}]]


def _metadata():
    return {"version": "v1", "bundles": {"a": {"bundleName": "a", "hash": "h"}}}


def _version():
    return {"appVersion": "1.0", "assetVersion": "2"}


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [["url", {"bundleName": "a"}, "extra"]],
        [["", {"bundleName": "a"}]],
        [["url", {}]],
        [["url", {"bundleName": "a"}], "bad"],
    ],
)
def test_pending_queue_validation_is_strict(payload) -> None:
    with pytest.raises(state.StateValidationError):
        state.validate_pending_queue(payload)


def test_pending_queue_rejects_duplicate_bundle_names() -> None:
    with pytest.raises(state.StateValidationError, match="duplicate"):
        state.validate_pending_queue(
            [
                ["url-a", {"bundleName": "same", "hash": "a"}],
                ["url-b", {"bundleName": "same", "hash": "b"}],
            ]
        )


def test_bundle_crc_compatibility_accepts_empty_hash_and_normalizes_numeric_crc() -> None:
    bundle = state.validate_pending_queue(
        [["url", {"bundleName": "regional", "hash": "", "crc": 12345}]]
    )[0][1]
    assert bundle["hash"] == ""
    assert bundle["crc"] == "12345"


def test_null_hash_with_crc_is_normalized_before_journal_creation(tmp_path: Path) -> None:
    metadata = normalize_asset_bundle_info(
        {
            "version": "v1",
            "bundles": {"regional": {"bundleName": "regional", "hash": None, "crc": 12345}},
        }
    )
    queue = [["https://example.test/regional", metadata["bundles"]["regional"]]]

    journal = state.create_journal(tmp_path / "journal.json", queue, metadata, _version(), "tx")

    assert journal["queue"][0][1]["hash"] == ""
    assert journal["queue"][0][1]["crc"] == "12345"


def test_invalid_numeric_metadata_version_uses_authoritative_asset_ver_fallback(
    tmp_path: Path,
) -> None:
    metadata = normalize_asset_bundle_info(
        {
            "version": 38,
            "bundles": {"regional": {"bundleName": "regional", "hash": "abc"}},
        },
        fallback_asset_ver="39",
    )

    journal = state.create_journal(
        tmp_path / "journal.json",
        [["https://example.test/regional", metadata["bundles"]["regional"]]],
        metadata,
        _version(),
        "tx",
    )

    assert journal["asset_metadata"]["version"] == "39"


def test_invalid_metadata_version_uses_authoritative_asset_ver_fallback() -> None:
    metadata = normalize_asset_bundle_info(
        {"version": None, "bundles": {"regional": {"bundleName": "regional", "hash": "abc"}}},
        fallback_asset_ver="38",
    )

    validated = state.validate_asset_metadata(metadata)

    assert validated["version"] == "38"


def test_invalid_metadata_version_without_asset_ver_fallback_is_rejected() -> None:
    metadata = normalize_asset_bundle_info(
        {"version": "", "bundles": {"regional": {"bundleName": "regional", "hash": "abc"}}},
        fallback_asset_ver=None,
    )

    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(metadata)


@pytest.mark.parametrize("version", [None, True, False, "", 1.5, {}])
def test_invalid_metadata_versions_are_not_normalized(version: object) -> None:
    metadata = normalize_asset_bundle_info(
        {"version": version, "bundles": {"regional": {"bundleName": "regional", "hash": "abc"}}}
    )

    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(metadata)


@pytest.mark.parametrize(
    "bundle",
    [
        {"bundleName": "missing"},
        {"bundleName": "null-hash", "hash": None},
        {"bundleName": "empty-hash", "hash": ""},
    ],
)
def test_bundle_without_hash_or_crc_is_rejected(bundle: dict) -> None:
    with pytest.raises(state.StateValidationError):
        state.validate_pending_queue([["https://example.test/bundle", bundle]])


def test_metadata_and_version_validation_is_strict() -> None:
    invalid_metadata = {"bundles": {"a": {}}}
    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(invalid_metadata)
    invalid_metadata = {"bundles": []}
    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(invalid_metadata)
    invalid_version = {}
    with pytest.raises(state.StateValidationError):
        state.validate_game_version(invalid_version)
    invalid_version = {"appVersion": ""}
    with pytest.raises(state.StateValidationError):
        state.validate_game_version(invalid_version)
    invalid_metadata = {
        "version": "v1",
        "os": "ios",
        "bundles": {"key": {"bundleName": "other"}},
    }
    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(invalid_metadata)
    invalid_metadata = {"version": 1, "bundles": {"a": {"bundleName": "a"}}}
    with pytest.raises(state.StateValidationError):
        state.validate_asset_metadata(invalid_metadata)
    invalid_version = {"appVersion": "1", "assetVersion": 2}
    with pytest.raises(state.StateValidationError):
        state.validate_game_version(invalid_version)
    invalid_journal = {
        "schema_version": True,
        "queue": _queue(),
        "asset_metadata": _metadata(),
        "game_version": _version(),
        "transaction_id": "tx",
        "operation": "update",
    }
    with pytest.raises(state.StateValidationError):
        state.validate_journal(invalid_journal)
    invalid_journal = {
        "schema_version": 1,
        "queue": _queue(),
        "asset_metadata": _metadata(),
        "game_version": _version(),
        "transaction_id": "tx",
        "operation": "repair",
    }
    with pytest.raises(state.StateValidationError):
        state.validate_journal(invalid_journal)


def test_missing_and_unreadable_state_are_distinct(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(state.StateNotFoundError):
        state.load_pending_queue(tmp_path / "missing.json")
    target = tmp_path / "directory"
    target.mkdir()
    with pytest.raises(state.StatePersistenceError):
        state.load_pending_queue(target)


def test_atomic_write_flushes_file_and_parent_and_replaces_json(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("old")
    fsync_calls: list[int] = []
    events: list[str] = []
    real_replace = state.os.replace
    monkeypatch.setattr(
        state.os,
        "fsync",
        lambda descriptor: (fsync_calls.append(descriptor), events.append("fsync")),
    )

    def recording_replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(state.os, "replace", recording_replace)

    state.atomic_write_json(target, _queue(), state.validate_pending_queue)

    assert json.loads(target.read_text()) == _queue()
    assert len(fsync_calls) == 2
    assert events == ["fsync", "replace", "fsync"]
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_file_fsync_failure_preserves_old_target_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('[["old", {"bundleName": "old"}]]')
    monkeypatch.setattr(
        state.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("fsync"))
    )
    with pytest.raises(state.StatePersistenceError):
        state.atomic_write_json(target, _queue(), state.validate_pending_queue)
    assert target.read_text() == '[["old", {"bundleName": "old"}]]'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_parent_fsync_failure_after_replace_leaves_new_target(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    old = '[["old", {"bundleName": "old"}]]'
    target.write_text(old)
    calls = 0
    real_fsync_parent = state._fsync_parent

    def fail_parent(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise state.StatePersistenceError("directory fsync")
        return real_fsync_parent(path)

    monkeypatch.setattr(state, "_fsync_parent", fail_parent)
    with pytest.raises(state.StatePersistenceError):
        state.atomic_write_json(target, _queue(), state.validate_pending_queue)
    assert target.read_text() != old


def test_atomic_write_requires_validator_and_existing_parent(tmp_path: Path) -> None:
    queue = _queue()
    with pytest.raises(state.StateValidationError):
        state.atomic_write_json(tmp_path / "state.json", queue, 0)  # type: ignore[arg-type]
    missing_target = tmp_path / "absent" / "state.json"
    with pytest.raises(state.StatePersistenceError):
        state.atomic_write_json(missing_target, queue, state.validate_pending_queue)


def test_replace_failure_preserves_old_target_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state.json"
    target.write_text('[["old", {"bundleName": "old"}]]')

    def fail_replace(_source, _target):
        raise OSError("replace fault")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(state.StatePersistenceError):
        state.atomic_write_json(target, _queue(), state.validate_pending_queue)
    assert target.read_text() == '[["old", {"bundleName": "old"}]]'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_durable_unlink_flushes_parent(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state.json"
    target.write_text("state")
    fsync_calls: list[int] = []
    monkeypatch.setattr(state.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))
    state.durable_unlink(target)
    assert not target.exists()
    assert len(fsync_calls) == 1


def test_durable_unlink_directory_fsync_failure_reports_uncertain_durability(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("state")
    monkeypatch.setattr(
        state,
        "_fsync_parent",
        lambda _path: (_ for _ in ()).throw(state.StatePersistenceError("directory fsync")),
    )
    with pytest.raises(state.StatePersistenceError, match="directory fsync"):
        state.durable_unlink(target)
    assert not target.exists()


def test_paths_are_predictable_without_backup_selection(tmp_path: Path) -> None:
    paths = state.derive_state_paths(tmp_path / "nested" / "dl.json")
    assert paths.journal == tmp_path / "nested" / "dl.json.journal"
    assert paths.lock == tmp_path / "nested" / ".updater-state.lock"
    assert paths.asset_metadata.name == "asset_bundle_info.json"
    assert paths.game_version.name == "version.json"


def test_live2d_state_paths_are_owned_separately_but_share_region_lock(tmp_path: Path) -> None:
    assets = state.derive_active_state_paths(
        "assets",
        tmp_path / "dl.json",
        tmp_path / "metadata.json",
        tmp_path / "version.json",
    )
    live2d = state.derive_active_state_paths(
        "live2d",
        tmp_path / "dl.json",
        tmp_path / "metadata.json",
        tmp_path / "version.json",
    )

    assert assets.queue == tmp_path / "dl.json"
    assert assets.asset_metadata == tmp_path / "metadata.json"
    assert assets.game_version == tmp_path / "version.json"
    assert assets.journal == tmp_path / "dl.json.journal"
    assert live2d.queue == tmp_path / "live2d_dl_list.json"
    assert live2d.asset_metadata == tmp_path / "live2d_asset_bundle_info.json"
    assert live2d.game_version == tmp_path / "live2d_version.json"
    assert live2d.journal == tmp_path / "live2d_dl_list.json.journal"
    assert live2d.lock == assets.lock


def test_state_paths_reject_cross_directory_and_alias_sets(tmp_path: Path) -> None:
    with pytest.raises(state.StateValidationError, match="share one parent"):
        state.derive_state_paths(tmp_path / "dl.json", tmp_path / "other" / "metadata.json")
    with pytest.raises(state.StateValidationError, match="distinct"):
        state.derive_state_paths(tmp_path / "dl.json", tmp_path / "dl.json")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(state.StateValidationError, match="distinct"):
        state.derive_state_paths(tmp_path / "dl.json", alias / "dl.json")


def test_journal_replay_repairs_corrupt_targets_in_required_order(
    tmp_path: Path, monkeypatch
) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    state.create_journal(paths.journal, _queue(), _metadata(), _version(), "tx-1")
    paths.queue.write_text("corrupt")
    paths.asset_metadata.write_text("corrupt")
    paths.game_version.write_text("corrupt")
    order: list[str] = []
    original_write = state.atomic_write_json
    original_unlink = state.durable_unlink

    def recording_write(path, value, validator=state.validate_pending_queue):
        order.append(Path(path).name)
        return original_write(path, value, validator)

    def recording_unlink(path):
        order.append(Path(path).name)
        return original_unlink(path)

    monkeypatch.setattr(state, "atomic_write_json", recording_write)
    monkeypatch.setattr(state, "durable_unlink", recording_unlink)
    assert state.replay_journal(
        paths.journal, paths.queue, paths.asset_metadata, paths.game_version
    )
    assert order == ["dl.json", "metadata.json", "version.json", "dl.json.journal"]
    assert state.load_pending_queue(paths.queue) == _queue()
    assert state.load_asset_metadata(paths.asset_metadata) == _metadata()
    assert state.load_game_version(paths.game_version) == _version()
    assert not paths.journal.exists()


def test_create_journal_rejects_every_existing_journal(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    for existing in (b"valid", b"{bad", b'{"schema_version": 99}'):
        journal.write_bytes(existing)
        before = journal.read_bytes()
        with pytest.raises(state.StateAlreadyExistsError):
            state.create_journal(journal, _queue(), _metadata(), _version(), "tx")
        assert journal.read_bytes() == before
        journal.unlink()


def test_create_journal_file_fsync_failure_leaves_no_partial_journal(
    tmp_path: Path, monkeypatch
) -> None:
    journal = tmp_path / "journal.json"
    monkeypatch.setattr(
        state.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("journal fsync")),
    )
    with pytest.raises(state.StatePersistenceError):
        state.create_journal(journal, _queue(), _metadata(), _version(), "tx")
    assert not journal.exists()
    assert not list(tmp_path.glob(".journal.json.*.tmp"))


def test_create_journal_publication_failure_leaves_no_partial_journal(
    tmp_path: Path, monkeypatch
) -> None:
    journal = tmp_path / "journal.json"
    monkeypatch.setattr(
        state.os,
        "link",
        lambda _source, _target: (_ for _ in ()).throw(OSError("link fault")),
    )
    with pytest.raises(state.StatePersistenceError):
        state.create_journal(journal, _queue(), _metadata(), _version(), "tx")
    assert not journal.exists()
    assert not list(tmp_path.glob(".journal.json.*.tmp"))


def test_create_journal_collision_preserves_existing_bytes_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    journal = tmp_path / "journal.json"
    journal.write_bytes(b"existing journal bytes")
    before = journal.read_bytes()
    with pytest.raises(state.StateAlreadyExistsError):
        state.create_journal(journal, _queue(), _metadata(), _version(), "tx")
    assert journal.read_bytes() == before
    assert not list(tmp_path.glob(".journal.json.*.tmp"))


def test_create_journal_parent_fsync_failure_leaves_full_journal(
    tmp_path: Path, monkeypatch
) -> None:
    journal = tmp_path / "journal.json"
    monkeypatch.setattr(
        state,
        "_fsync_parent",
        lambda _path: (_ for _ in ()).throw(state.StatePersistenceError("parent fsync")),
    )
    with pytest.raises(state.StatePersistenceError):
        state.create_journal(journal, _queue(), _metadata(), _version(), "tx")
    assert state.load_journal(journal)["transaction_id"] == "tx"
    assert not list(tmp_path.glob(".journal.json.*.tmp"))


@pytest.mark.parametrize("failure_index", [0, 1, 2])
def test_replay_target_failure_preserves_journal_and_later_replay_repairs(
    tmp_path: Path, monkeypatch, failure_index: int
) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    state.create_journal(paths.journal, _queue(), _metadata(), _version(), "tx")
    original = state.atomic_write_json
    calls = 0

    def fail_once(path, value, validator):
        nonlocal calls
        if calls == failure_index:
            calls += 1
            raise state.StatePersistenceError("injected replay failure")
        calls += 1
        return original(path, value, validator)

    monkeypatch.setattr(state, "atomic_write_json", fail_once)
    with pytest.raises(state.StatePersistenceError):
        state.replay_journal(paths.journal, paths.queue, paths.asset_metadata, paths.game_version)
    assert paths.journal.exists()
    monkeypatch.setattr(state, "atomic_write_json", original)
    assert state.replay_journal(
        paths.journal, paths.queue, paths.asset_metadata, paths.game_version
    )
    assert state.load_pending_queue(paths.queue) == _queue()
    assert state.load_asset_metadata(paths.asset_metadata) == _metadata()
    assert state.load_game_version(paths.game_version) == _version()


def test_replay_post_replace_directory_fsync_failure_keeps_journal_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    state.create_journal(paths.journal, _queue(), _metadata(), _version(), "tx")
    real_parent_fsync = state._fsync_parent
    calls = 0

    def fail_after_replace(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise state.StatePersistenceError("post-replace directory fsync")
        return real_parent_fsync(path)

    monkeypatch.setattr(state, "_fsync_parent", fail_after_replace)
    with pytest.raises(state.StatePersistenceError, match="post-replace"):
        state.replay_journal(paths)
    assert paths.journal.exists()
    monkeypatch.setattr(state, "_fsync_parent", real_parent_fsync)
    assert state.replay_journal(paths)
    assert not paths.journal.exists()


def test_replay_verification_mismatch_retains_journal(tmp_path: Path, monkeypatch) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    state.create_journal(paths.journal, _queue(), _metadata(), _version(), "tx")
    original_load = state.load_asset_metadata

    def mismatched_load(path):
        if Path(path) == paths.asset_metadata:
            return {"version": "wrong", "bundles": {"a": {"bundleName": "a"}}}
        return original_load(path)

    monkeypatch.setattr(state, "load_asset_metadata", mismatched_load)
    with pytest.raises(state.StatePersistenceError, match="metadata"):
        state.replay_journal(paths)
    assert paths.journal.exists()


def test_replay_unlink_failure_leaves_journal(tmp_path: Path, monkeypatch) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    state.create_journal(paths.journal, _queue(), _metadata(), _version(), "tx")
    monkeypatch.setattr(
        state,
        "durable_unlink",
        lambda _path: (_ for _ in ()).throw(state.StatePersistenceError("unlink fault")),
    )
    with pytest.raises(state.StatePersistenceError):
        state.replay_journal(paths.journal, paths.queue, paths.asset_metadata, paths.game_version)
    assert paths.journal.exists()


def test_unsupported_complete_journal_fails_closed_without_mutation(tmp_path: Path) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    for path, payload in (
        (paths.queue, _queue()),
        (paths.asset_metadata, _metadata()),
        (paths.game_version, _version()),
    ):
        path.write_text(json.dumps(payload))
    before = [path.read_bytes() for path in (paths.queue, paths.asset_metadata, paths.game_version)]
    paths.journal.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "queue": _queue(),
                "asset_metadata": _metadata(),
                "game_version": _version(),
                "transaction_id": "tx",
                "operation": "update",
            }
        )
    )
    with pytest.raises(state.StateValidationError):
        state.replay_journal(paths)
    assert [
        path.read_bytes() for path in (paths.queue, paths.asset_metadata, paths.game_version)
    ] == before
    assert paths.journal.exists()


def test_state_lock_requires_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(state.StateLockError, match="does not exist"):
        state.StateLock(tmp_path / "missing" / ".updater-state.lock").acquire()


def test_overlapping_manual_state_paths_share_one_lock_identity(tmp_path: Path) -> None:
    paths = state.derive_state_paths(tmp_path / "dl.json")
    overlap = state.StatePaths(
        queue=paths.queue,
        asset_metadata=paths.asset_metadata,
        game_version=paths.game_version,
        journal=paths.journal,
        lock=paths.lock,
    )
    first = state.StateLock(paths.lock).acquire()
    second = state.StateLock(overlap.lock)
    try:
        with pytest.raises(state.StateLockError, match="already held"):
            second.acquire()
    finally:
        first.release()


def test_distinct_state_paths_with_shared_targets_share_lock_and_contend(tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.json"
    version = tmp_path / "version.json"
    first = state.StatePaths(
        queue=tmp_path / "first.json",
        asset_metadata=metadata,
        game_version=version,
        journal=tmp_path / "first.journal",
        lock=tmp_path / ".updater-state.lock",
    )
    second = state.StatePaths(
        queue=tmp_path / "second.json",
        asset_metadata=metadata,
        game_version=version,
        journal=tmp_path / "second.journal",
        lock=tmp_path / ".updater-state.lock",
    )
    assert first.lock == second.lock
    holder = state.StateLock(first.lock).acquire()
    contender = state.StateLock(second.lock)
    try:
        with pytest.raises(state.StateLockError, match="already held"):
            contender.acquire()
    finally:
        holder.release()


def test_invalid_journal_fails_closed_without_target_mutation(tmp_path: Path) -> None:
    paths = state.derive_state_paths(
        tmp_path / "dl.json", tmp_path / "metadata.json", tmp_path / "version.json"
    )
    paths.queue.write_text(json.dumps(_queue()))
    paths.asset_metadata.write_text(json.dumps(_metadata()))
    paths.game_version.write_text(json.dumps(_version()))
    before = [path.read_bytes() for path in (paths.queue, paths.asset_metadata, paths.game_version)]
    paths.journal.write_text(json.dumps({"schema_version": 999}))

    with pytest.raises(state.StateValidationError):
        state.replay_journal(paths.journal, paths.queue, paths.asset_metadata, paths.game_version)
    assert [
        path.read_bytes() for path in (paths.queue, paths.asset_metadata, paths.game_version)
    ] == before
    assert paths.journal.exists()


def test_lock_contention_is_actionable_and_lock_file_survives(tmp_path: Path) -> None:
    lock_path = tmp_path / "updater.lock"
    first = state.StateLock(lock_path).acquire()
    second = state.StateLock(lock_path)
    try:
        with pytest.raises(state.StateLockError, match="already acquired"):
            first.acquire()
        with pytest.raises(state.StateLockError, match="already held"):
            second.acquire()
        assert lock_path.exists()
    finally:
        first.release()
        second.release()
    assert lock_path.exists()


def test_lock_release_allows_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "updater.lock"
    lock = state.StateLock(lock_path).acquire()
    lock.release()
    lock.acquire()
    lock.release()


def test_lock_contention_is_real_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "updater.lock"
    holder = state.StateLock(lock_path).acquire()
    script = "import state, sys; lock=state.StateLock(sys.argv[1]); lock.acquire()"
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert result.returncode != 0
        assert "already held" in result.stderr
    finally:
        holder.release()
