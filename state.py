"""Durable, single-writer state primitives for the updater.

This module deliberately has no knowledge of the updater lifecycle.  It owns
only validation, durable replacement/removal, journal replay, and the lock
which a caller can hold across a complete run.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


JOURNAL_SCHEMA_VERSION = 1
STATE_LOCK_FILENAME = ".updater-state.lock"
_JOURNAL_KEYS = {
    "schema_version",
    "queue",
    "asset_metadata",
    "game_version",
    "transaction_id",
    "operation",
}
JsonValidator = Callable[[Any], Any]


class StateError(Exception):
    """Base class for invalid state, persistence, and ownership failures."""


class StateValidationError(StateError):
    """Raised when a state payload is malformed or has an unsafe shape."""


class StatePersistenceError(StateError):
    """Raised when a durable filesystem operation cannot complete."""


class StateLockError(StateError):
    """Raised when exclusive updater ownership cannot be acquired."""


class StateNotFoundError(StateError):
    """Raised when a requested state file is absent."""


class StateAlreadyExistsError(StatePersistenceError):
    """Raised when an exclusive state creation would overwrite a file."""


def _fail(message: str) -> None:
    raise StateValidationError(message)


def _validate_json_value(value: Any, path: str = "value") -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not __import__("math").isfinite(value):
            _fail(f"{path} must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    _fail(f"{path} contains unsupported value type {type(value).__name__}")


def validate_pending_queue(value: Any) -> list[list[Any]]:
    """Validate the legacy pending queue without dropping bad entries.

    The on-disk representation is a JSON list of exact two-element lists:
    ``[url, bundle]``.  Bundle names are required to be non-empty strings.
    """

    if not isinstance(value, list):
        _fail("pending queue must be a list")
    result: list[list[Any]] = []
    bundle_names: set[str] = set()
    for index, record in enumerate(value):
        if not isinstance(record, list) or len(record) != 2:
            _fail(f"pending queue entry {index} must be an exact [url, bundle] list")
        url, bundle = record
        if not isinstance(url, str) or not url.strip():
            _fail(f"pending queue entry {index} URL must be non-empty")
        bundle = _validate_bundle(bundle, f"pending queue entry {index} bundle")
        bundle_name = bundle["bundleName"]
        if bundle_name in bundle_names:
            _fail(f"pending queue contains duplicate bundleName {bundle_name!r}")
        bundle_names.add(bundle_name)
        result.append([url, copy.deepcopy(bundle)])
    return result


def _validate_bundle(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError(f"{path} must be an object")
    bundle_name = value.get("bundleName")
    if not isinstance(bundle_name, str) or not bundle_name.strip():
        _fail(f"{path}.bundleName must be non-empty")
    normalized = copy.deepcopy(value)
    for field in {"downloadPath", "md5", "sha256"}:
        if field in normalized and (
            not isinstance(normalized[field], str) or not normalized[field].strip()
        ):
            _fail(f"{path}.{field} must be a non-empty string")
    if "hash" in normalized and (
        not isinstance(normalized["hash"], str)
        or (not normalized["hash"].strip() and "crc" not in normalized)
    ):
        _fail(f"{path}.hash must be a string and may be empty only with CRC")
    if "crc" in normalized:
        crc = normalized["crc"]
        if isinstance(crc, bool) or not isinstance(crc, (str, int, float)):
            _fail(f"{path}.crc must be numeric or a non-empty string")
        if isinstance(crc, float) and not math.isfinite(crc):
            _fail(f"{path}.crc must be finite")
        if isinstance(crc, str) and not crc.strip():
            _fail(f"{path}.crc must be non-empty when present")
        normalized["crc"] = str(crc)
    for field in {"fileSize", "size"}:
        if field in normalized and (type(normalized[field]) is not int or normalized[field] < 0):
            _fail(f"{path}.{field} must be a non-negative integer")
    _validate_json_value(normalized, path)
    return normalized


def validate_asset_metadata(value: Any) -> dict[str, Any]:
    """Validate the current cache shape: ``version``, ``os``, and ``bundles``.

    Bundle mapping keys are bundle names and must equal each value's
    ``bundleName``.  The top-level field set is intentionally strict so a
    malformed cache cannot be mistaken for a compatible generation.
    """

    if not isinstance(value, dict):
        _fail("asset metadata must be an object")
    if set(value) - {"version", "os", "bundles"}:
        _fail("asset metadata contains unknown fields")
    for field in {"version", "os"}:
        if field in value and not isinstance(value[field], str):
            _fail(f"asset metadata.{field} must be a string")
    bundles = value.get("bundles")
    if not isinstance(bundles, dict):
        _fail("asset metadata.bundles must be a mapping")
    assert isinstance(bundles, dict)
    for key, bundle in bundles.items():
        if not isinstance(key, str) or not key.strip():
            _fail("asset metadata bundle keys must be non-empty strings")
        bundle = _validate_bundle(bundle, f"asset metadata.bundles[{key!r}]")
        if bundle["bundleName"] != key:
            _fail(f"asset metadata bundle key {key!r} does not match bundleName")
        bundles[key] = bundle
    _validate_json_value(value, "asset metadata")
    return copy.deepcopy(value)


def validate_game_version(value: Any) -> dict[str, Any]:
    """Validate a JSON game-version object.

    ``appVersion`` is the region-neutral meaningful identifier required by the
    current project fixtures.  Known optional version/hash fields are strings;
    unknown fields remain forward-compatible but must still be JSON values.
    """

    if not isinstance(value, dict) or not value:
        _fail("game version must be a non-empty object")
    if not isinstance(value.get("appVersion"), str) or not value["appVersion"].strip():
        _fail("game version.appVersion must be a non-empty string")
    for field in {"assetVersion", "dataVersion", "assetHash", "appHash", "assetver"}:
        if field in value and (not isinstance(value[field], str) or not value[field].strip()):
            _fail(f"game version.{field} must be a non-empty string")
    _validate_json_value(value, "game version")
    return copy.deepcopy(value)


def _load_json(path: os.PathLike[str] | str, validator: JsonValidator) -> Any:
    target = Path(path)
    try:
        with target.open("rb") as stream:
            value = json.loads(stream.read())
    except FileNotFoundError as exc:
        raise StateNotFoundError(f"state file is absent: {target}") from exc
    except OSError as exc:
        raise StatePersistenceError(f"failed to read state at {target}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise StateValidationError(f"invalid JSON state at {target}: {exc}") from exc
    try:
        return validator(value)
    except StateValidationError:
        raise
    except Exception as exc:  # validators are part of the trust boundary
        raise StateValidationError(f"invalid state at {target}: {exc}") from exc


def load_pending_queue(path: os.PathLike[str] | str) -> list[list[Any]]:
    return _load_json(path, validate_pending_queue)


def load_asset_metadata(path: os.PathLike[str] | str) -> dict[str, Any]:
    return _load_json(path, validate_asset_metadata)


def load_game_version(path: os.PathLike[str] | str) -> dict[str, Any]:
    return _load_json(path, validate_game_version)


def _fsync_parent(path: Path) -> None:
    """Flush a directory entry using the POSIX facilities available on macOS."""

    try:
        descriptor = os.open(path.as_posix(), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise StatePersistenceError(f"failed to fsync parent directory {path}: {exc}") from exc


def prepare_state_directory(path: os.PathLike[str] | str) -> Path:
    """Create a state directory chain and durably publish each directory link.

    Existing components are required to be directories.  Missing components
    are created one at a time and their containing directory is fsynced before
    the next component is created.  This is a durability primitive, not a
    claim of race resistance against concurrent filesystem mutation.
    """

    target = Path(path).resolve(strict=False)
    missing: list[Path] = []
    current = target
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.exists() and not current.is_dir():
        raise StatePersistenceError(f"state directory component is not a directory: {current}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise StatePersistenceError(
                    f"state directory component is not a directory: {directory}"
                )
        _fsync_parent(directory.parent)
    if not target.is_dir():
        raise StatePersistenceError(f"state directory is not a directory: {target}")
    return target


def atomic_write_json(
    path: os.PathLike[str] | str,
    value: Any,
    validator: JsonValidator,
) -> None:
    """Atomically and durably replace JSON at ``path``.

    Validation and serialization happen before opening the temporary file.
    The temporary file is same-directory, flushed and fsynced before replace;
    the parent directory is fsynced after replace.  A failed replace leaves
    the prior target untouched and removes the temporary file.
    """

    target = Path(path)
    if not callable(validator):
        raise StateValidationError("atomic_write_json requires a validator")
    value = validator(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise StateValidationError(f"state is not serializable: {exc}") from exc

    parent = target.parent
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        if not parent.is_dir():
            raise StatePersistenceError(f"state parent directory does not exist: {parent}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        _fsync_parent(parent)
    except StateError:
        raise
    except (OSError, ValueError) as exc:
        raise StatePersistenceError(f"failed to atomically write {target}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def durable_unlink(path: os.PathLike[str] | str) -> None:
    """Unlink a state file and durably flush its parent directory.

    If the unlink succeeds but the directory fsync fails, the file is already
    absent and ``StatePersistenceError`` is raised; callers must retain any
    transaction journal and retry/reconcile rather than assume durability.
    """

    target = Path(path)
    try:
        target.unlink(missing_ok=True)
        _fsync_parent(target.parent)
    except StateError:
        raise
    except OSError as exc:
        raise StatePersistenceError(f"failed to durably remove {target}: {exc}") from exc


@dataclass(frozen=True)
class StatePaths:
    """Canonical files for one state generation and its shared lock identity."""

    queue: Path
    asset_metadata: Path
    game_version: Path
    journal: Path
    lock: Path

    def __post_init__(self) -> None:
        paths = [
            self.queue.resolve(strict=False),
            self.asset_metadata.resolve(strict=False),
            self.game_version.resolve(strict=False),
            self.journal.resolve(strict=False),
            self.lock.resolve(strict=False),
        ]
        if len(set(paths)) != len(paths):
            raise StateValidationError("state paths must be distinct")
        if len({path.parent for path in paths}) != 1:
            raise StateValidationError("all state files must share one parent directory")
        expected_lock = paths[0].parent / STATE_LOCK_FILENAME
        if paths[-1] != expected_lock:
            raise StateValidationError(
                f"state lock must be the canonical shared lock {expected_lock}"
            )
        object.__setattr__(self, "queue", paths[0])
        object.__setattr__(self, "asset_metadata", paths[1])
        object.__setattr__(self, "game_version", paths[2])
        object.__setattr__(self, "journal", paths[3])
        object.__setattr__(self, "lock", paths[4])


def derive_state_paths(
    dl_list_cache_path: os.PathLike[str] | str,
    asset_metadata_path: os.PathLike[str] | str | None = None,
    game_version_path: os.PathLike[str] | str | None = None,
) -> StatePaths:
    """Derive journal/lock siblings; no backup or alternate-path selection occurs."""

    queue = Path(dl_list_cache_path).resolve(strict=False)
    metadata = (
        Path(asset_metadata_path).resolve(strict=False)
        if asset_metadata_path
        else queue.with_name("asset_bundle_info.json")
    )
    version = (
        Path(game_version_path).resolve(strict=False)
        if game_version_path
        else queue.with_name("version.json")
    )
    paths = StatePaths(
        queue=queue,
        asset_metadata=metadata,
        game_version=version,
        journal=Path(f"{queue}.journal").resolve(strict=False),
        lock=queue.parent / STATE_LOCK_FILENAME,
    )
    return paths


def derive_live2d_state_paths(
    dl_list_cache_path: os.PathLike[str] | str,
) -> StatePaths:
    """Derive the Live2D-owned state set beside the legacy asset queue.

    Live2D retries and metadata must not become part of the assets generation.
    The canonical region lock remains shared because both state sets have the
    same parent directory.
    """

    legacy_queue = Path(dl_list_cache_path).resolve(strict=False)
    queue = legacy_queue.with_name("live2d_dl_list.json")
    return derive_state_paths(
        queue,
        queue.with_name("live2d_asset_bundle_info.json"),
        queue.with_name("live2d_version.json"),
    )


def derive_active_state_paths(
    mode: str,
    dl_list_cache_path: os.PathLike[str] | str,
    asset_metadata_path: os.PathLike[str] | str,
    game_version_path: os.PathLike[str] | str,
) -> StatePaths:
    """Resolve durable state ownership for a bundle-pipeline mode.

    Assets retain their configured legacy paths exactly.  Live2D owns a
    sibling state generation; Charts do not use this resolver because they are
    queue-free.
    """

    if mode == "live2d":
        return derive_live2d_state_paths(dl_list_cache_path)
    return derive_state_paths(dl_list_cache_path, asset_metadata_path, game_version_path)


def _validate_journal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _JOURNAL_KEYS:
        _fail("journal envelope has unknown or missing fields")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != JOURNAL_SCHEMA_VERSION
    ):
        _fail(f"unknown journal schema version: {value['schema_version']!r}")
    if not isinstance(value["transaction_id"], str) or not value["transaction_id"].strip():
        _fail("journal transaction_id must be non-empty")
    if value["operation"] != "update":
        _fail("journal operation must be 'update'")
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "queue": validate_pending_queue(value["queue"]),
        "asset_metadata": validate_asset_metadata(value["asset_metadata"]),
        "game_version": validate_game_version(value["game_version"]),
        "transaction_id": value["transaction_id"],
        "operation": value["operation"],
    }


def validate_journal(value: Any) -> dict[str, Any]:
    """Public strict validator for a versioned transaction journal."""

    return _validate_journal(value)


def create_journal(
    path: os.PathLike[str] | str | StatePaths,
    queue: Any,
    asset_metadata: Any,
    game_version: Any,
    transaction_id: str | None = None,
    operation: str = "update",
) -> dict[str, Any]:
    """Validate and durably create an authoritative journal envelope.

    Publication uses ``link(temp, final)`` rather than ``replace``.  POSIX
    ``link`` is an atomic no-replace publication primitive: it succeeds only
    when the final journal is absent, so a collision cannot overwrite an
    existing journal.  The temporary inode is fully written and file-fsynced
    before publication, then removed and the parent directory is fsynced.
    """

    envelope = _validate_journal(
        {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "queue": queue,
            "asset_metadata": asset_metadata,
            "game_version": game_version,
            "transaction_id": transaction_id or uuid.uuid4().hex,
            "operation": operation,
        }
    )
    journal_path = path.journal if isinstance(path, StatePaths) else Path(path)
    if journal_path.exists():
        raise StateAlreadyExistsError(f"journal already exists: {journal_path}")
    try:
        payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False, sort_keys=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise StateValidationError(f"journal is not serializable: {exc}") from exc
    if not journal_path.parent.is_dir():
        raise StatePersistenceError(f"state parent directory does not exist: {journal_path.parent}")
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{journal_path.name}.", suffix=".tmp", dir=journal_path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, journal_path)
        except FileExistsError as exc:
            raise StateAlreadyExistsError(f"journal already exists: {journal_path}") from exc
        except OSError as exc:
            raise StatePersistenceError(f"cannot publish journal {journal_path}: {exc}") from exc
        temporary_path.unlink()
        temporary_path = None
        _fsync_parent(journal_path.parent)
    except StateError:
        raise
    except OSError as exc:
        raise StatePersistenceError(f"failed to create journal {journal_path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return envelope


def load_journal(path: os.PathLike[str] | str) -> dict[str, Any]:
    return _load_json(path, _validate_journal)


def _state_set(
    journal_path: os.PathLike[str] | str,
    queue_path: os.PathLike[str] | str,
    asset_metadata_path: os.PathLike[str] | str,
    game_version_path: os.PathLike[str] | str,
) -> StatePaths:
    """Normalize and validate a replay set without accepting path aliases."""

    journal = Path(journal_path).resolve(strict=False)
    queue = Path(queue_path).resolve(strict=False)
    metadata = Path(asset_metadata_path).resolve(strict=False)
    version = Path(game_version_path).resolve(strict=False)
    paths = StatePaths(
        queue=queue,
        asset_metadata=metadata,
        game_version=version,
        journal=journal,
        lock=queue.parent / ".updater-state.lock",
    )
    return paths


def replay_journal(
    journal_path: os.PathLike[str] | str | StatePaths,
    queue_path: os.PathLike[str] | str | None = None,
    asset_metadata_path: os.PathLike[str] | str | None = None,
    game_version_path: os.PathLike[str] | str | None = None,
) -> bool:
    """Replay a valid journal in queue/metadata/version order, then remove it.

    Journal loading and complete validation occur before any target mutation.
    Therefore an invalid or unknown journal fails closed and leaves every
    target untouched.  A valid journal is authoritative even if targets are
    corrupt or absent.
    """

    if isinstance(journal_path, StatePaths):
        if any(path is not None for path in (queue_path, asset_metadata_path, game_version_path)):
            raise StateValidationError("StatePaths replay cannot be combined with path arguments")
        paths = journal_path
        _state_set(paths.journal, paths.queue, paths.asset_metadata, paths.game_version)
    else:
        if any(path is None for path in (queue_path, asset_metadata_path, game_version_path)):
            raise StateValidationError("replay_journal requires queue, metadata, and version paths")
        paths = _state_set(
            journal_path,
            queue_path,  # type: ignore[arg-type]
            asset_metadata_path,  # type: ignore[arg-type]
            game_version_path,  # type: ignore[arg-type]
        )
    journal = paths.journal
    try:
        envelope = load_journal(journal)
    except StateNotFoundError:
        return False
    atomic_write_json(paths.queue, envelope["queue"], validate_pending_queue)
    atomic_write_json(paths.asset_metadata, envelope["asset_metadata"], validate_asset_metadata)
    atomic_write_json(paths.game_version, envelope["game_version"], validate_game_version)
    if load_pending_queue(paths.queue) != envelope["queue"]:
        raise StatePersistenceError("replayed queue failed verification")
    if load_asset_metadata(paths.asset_metadata) != envelope["asset_metadata"]:
        raise StatePersistenceError("replayed metadata failed verification")
    if load_game_version(paths.game_version) != envelope["game_version"]:
        raise StatePersistenceError("replayed game version failed verification")
    durable_unlink(journal)
    return True


class StateLock:
    """Stable exclusive whole-run lock backed by POSIX ``fcntl.flock``.

    The lock file is intentionally never unlinked, so its inode remains a
    stable coordination point.  This implementation targets POSIX systems
    (including macOS); callers on platforms without ``fcntl`` must provide a
    platform-specific adapter rather than silently falling back unsafely.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> "StateLock":
        if self._stream is not None:
            raise StateLockError(f"updater lock already acquired by this instance: {self.path}")
        try:
            if not self.path.parent.is_dir():
                raise StateLockError(
                    f"state lock parent directory does not exist: {self.path.parent}"
                )
            self._stream = self.path.open("a+")
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, AttributeError) as exc:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EAGAIN):
                raise StateLockError(
                    f"updater lock is already held: {self.path}; stop the other run first"
                ) from exc
            raise StateLockError(f"cannot acquire updater lock {self.path}: {exc}") from exc
        return self

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "StateLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
