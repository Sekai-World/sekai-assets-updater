from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from anyio import Path as AnyioPath

import bundle
import worker


class _Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class _CancelledContent:
    async def iter_chunked(self, _size):
        yield b"UnityFS partial"
        raise asyncio.CancelledError


class _Response:
    def __init__(self, body: bytes, *, status=200, headers=None):
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self.content = _Content([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class _SequenceSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _run(tmp_path: Path, body: bytes, *, bundle_data=None, headers=None, existing=b"old"):
    target = tmp_path / "nested" / "bundle"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(existing)
    response = _Response(body, headers=headers)
    config = SimpleNamespace(DOWNLOAD_MAX_RETRIES=1, REQUEST_TIMEOUT=1)
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            AnyioPath(target.parent),
            "bundle",
            {},
            config=config,
            session=_Session(response),
            expected_bundle=bundle_data,
        )
    )
    return target


def _unityfs(*, declared_size: int | None = None) -> bytes:
    fields = b"2024.1\0rev\0"
    fixed_size = 8 + 4 + len(fields) + 20 + 1
    total_size = declared_size if declared_size is not None else fixed_size
    return (
        b"UnityFS\0"
        + struct.pack(">I", 6)
        + fields
        + struct.pack(">QIII", total_size, 1, 1, 0)
        + b"I"
    )


def test_plain_unityfs_is_promoted_atomically(tmp_path: Path) -> None:
    data = _unityfs()
    target = _run(
        tmp_path,
        data,
        bundle_data={"fileSize": len(data)},
    )
    assert target.read_bytes() == data


def test_obfuscated_unityfs_is_deobfuscated_and_promoted(tmp_path: Path) -> None:
    plain = _unityfs(declared_size=150) + b"payload" * 15 + b"x"
    mask = (b"\xff" * 5 + b"\x00" * 3) * 16
    encoded_header = bytes(a ^ b for a, b in zip(plain[:128], mask))
    body = b"\x10\x00\x00\x00" + encoded_header + plain[128:]
    target = _run(tmp_path, body, bundle_data={"fileSize": len(plain)})
    assert target.read_bytes() == plain


@pytest.mark.parametrize(
    "body",
    [b"", b"short", b"NotUnityFS payload", b"\x20\x00\x00\x00", b"\x10\x00\x00\x00short"],
)
def test_invalid_or_truncated_download_never_promotes(tmp_path: Path, body: bytes) -> None:
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, body)
    assert (tmp_path / "nested" / "bundle").read_bytes() == b"old"
    assert not list((tmp_path / "nested").glob(".bundle.*.tmp"))


def test_content_length_mismatch_preserves_existing(tmp_path: Path) -> None:
    body = _unityfs()
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, body, headers={"Content-Length": "999"})
    assert (tmp_path / "nested" / "bundle").read_bytes() == b"old"


def test_file_size_mismatch_preserves_existing(tmp_path: Path) -> None:
    body = b"\x20\x00\x00\x00" + _unityfs()
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, body, bundle_data={"fileSize": 1})
    assert (tmp_path / "nested" / "bundle").read_bytes() == b"old"


def test_size_only_metadata_is_validated(tmp_path: Path) -> None:
    data = _unityfs()
    target = _run(tmp_path, data, bundle_data={"size": len(data)})
    assert target.read_bytes() == data


def test_conflicting_size_metadata_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, _unityfs(), bundle_data={"fileSize": 1, "size": 2})


def test_same_size_malformed_unityfs_is_rejected(tmp_path: Path) -> None:
    data = b"UnityFS\0" + b"x" * 35
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, data, bundle_data={"fileSize": len(data)})
    assert (tmp_path / "nested" / "bundle").read_bytes() == b"old"


def test_invalid_expected_metadata_is_rejected_before_promotion(tmp_path: Path) -> None:
    body = _unityfs()
    with pytest.raises(bundle.DownloadIntegrityError):
        _run(tmp_path, body, bundle_data={"fileSize": "not-an-int"})
    assert (tmp_path / "nested" / "bundle").read_bytes() == b"old"


def test_hash_does_not_participate_in_byte_validation(tmp_path: Path) -> None:
    body = b"\x20\x00\x00\x00" + _unityfs()
    target = _run(
        tmp_path,
        body,
        bundle_data={"hash": "arbitrary-selection-hash", "crc": "also-not-validated"},
    )
    assert target.read_bytes() == body[4:]


def test_non_200_response_does_not_promote(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    target.write_bytes(b"old")
    response = _Response(_unityfs(), status=503)
    config = SimpleNamespace(DOWNLOAD_MAX_RETRIES=1, REQUEST_TIMEOUT=1)
    with pytest.raises(bundle.DownloadIntegrityError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                AnyioPath(tmp_path),
                "bundle",
                {},
                config=config,
                session=_Session(response),
            )
        )
    assert target.read_bytes() == b"old"


def test_cancellation_cleans_temp_without_retry_or_replacing_existing(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    target.write_bytes(b"old")
    response = _Response(b"", headers={})
    response.content = _CancelledContent()  # type: ignore[assignment]
    session = _Session(response)
    config = SimpleNamespace(DOWNLOAD_MAX_RETRIES=3, REQUEST_TIMEOUT=1)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                AnyioPath(tmp_path),
                "bundle",
                {},
                config=config,
                session=session,
            )
        )

    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".bundle.*.tmp"))


def _retry_config(**overrides) -> SimpleNamespace:
    values = {
        "DOWNLOAD_MAX_RETRIES": 2,
        "DOWNLOAD_RETRY_BASE_DELAY": 1.0,
        "DOWNLOAD_RETRY_MAX_DELAY": 4.0,
        "REQUEST_TIMEOUT": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _successful_response() -> _Response:
    return _Response(_unityfs())


def _connector_error() -> aiohttp.ClientConnectorError:
    key = aiohttp.client_reqrep.ConnectionKey("example.test", 443, True, False, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError("connection refused"))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_failures_retry_and_then_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    outcomes = [_Response(b"", status=status), _successful_response()]
    session = _SequenceSession(outcomes)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(bundle.random, "uniform", lambda low, high: high)
    target = tmp_path / "bundle"
    target.write_bytes(b"old")
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            tmp_path,
            "bundle",
            {},
            config=_retry_config(),
            session=session,
        )
    )
    assert session.calls == 2
    assert sleeps and sleeps[0] <= 4.0
    assert target.read_bytes() == _unityfs()


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.TimeoutError(),
        _connector_error(),
        aiohttp.ClientConnectionError("connection failed"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("payload failed"),
    ],
    ids=["timeout", "connector", "connection", "disconnect", "payload"],
)
def test_retryable_connection_failures_retry_and_then_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    session = _SequenceSession([failure, _successful_response()])
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            tmp_path,
            "bundle",
            {},
            config=_retry_config(),
            session=session,
        )
    )
    assert session.calls == 2
    assert len(sleeps) == 1
    assert (tmp_path / "bundle").read_bytes() == _unityfs()


def test_http_408_response_is_retried_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    session = _SequenceSession([_Response(b"", status=408), _successful_response()])
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            tmp_path,
            "bundle",
            {},
            config=_retry_config(),
            session=session,
        )
    )
    assert session.calls == 2
    assert len(sleeps) == 1
    assert (tmp_path / "bundle").read_bytes() == _unityfs()


@pytest.mark.parametrize("status", [400, 404, 401, 403, 600])
def test_permanent_http_errors_are_single_attempt(tmp_path: Path, status: int) -> None:
    session = _SequenceSession([_Response(b"", status=status), _successful_response()])
    target = tmp_path / "bundle"
    target.write_bytes(b"old")
    with pytest.raises(bundle.DownloadIntegrityError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                tmp_path,
                "bundle",
                {},
                config=_retry_config(),
                session=session,
            )
        )
    assert session.calls == 1
    assert target.read_bytes() == b"old"


def test_integrity_error_is_single_attempt(tmp_path: Path) -> None:
    session = _SequenceSession([_Response(b"not-a-bundle"), _successful_response()])
    with pytest.raises(bundle.DownloadIntegrityError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                tmp_path,
                "bundle",
                {},
                config=_retry_config(),
                session=session,
            )
        )
    assert session.calls == 1


def test_retry_exhaustion_uses_total_attempt_semantics(tmp_path: Path, monkeypatch) -> None:
    session = _SequenceSession([aiohttp.ClientConnectionError("down") for _ in range(3)])
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    with pytest.raises(bundle.RetryableDownloadError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                tmp_path,
                "bundle",
                {},
                config=_retry_config(DOWNLOAD_MAX_RETRIES=3),
                session=session,
            )
        )
    assert session.calls == 3
    assert len(sleeps) == 2


def test_full_jitter_bounds_follow_capped_exponential_schedule(tmp_path: Path, monkeypatch) -> None:
    session = _SequenceSession(
        [aiohttp.ClientConnectionError("down") for _ in range(3)] + [_successful_response()]
    )
    bounds: list[tuple[float, float]] = []
    sleeps: list[float] = []

    def fake_uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high / 2

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.random, "uniform", fake_uniform)
    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            tmp_path,
            "bundle",
            {},
            config=_retry_config(DOWNLOAD_MAX_RETRIES=4),
            session=session,
        )
    )
    assert bounds == [(0.0, 1.0), (0.0, 2.0), (0.0, 4.0)]
    assert sleeps == [0.5, 1.0, 2.0]


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_is_capped_and_used_as_jitter_upper_bound(
    tmp_path: Path, monkeypatch, status: int
) -> None:
    session = _SequenceSession(
        [_Response(b"", status=status, headers={"Retry-After": "99"}), _successful_response()]
    )
    bounds = []
    sleeps = []
    monkeypatch.setattr(
        bundle.random, "uniform", lambda low, high: bounds.append((low, high)) or high
    )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bundle.asyncio, "sleep", fake_sleep)
    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            tmp_path,
            "bundle",
            {},
            config=_retry_config(DOWNLOAD_RETRY_MAX_DELAY=3),
            session=session,
        )
    )
    assert bounds == [(0.0, 3.0)]
    assert sleeps == [3.0]


def test_cancellation_during_backoff_does_not_make_next_request(
    tmp_path: Path, monkeypatch
) -> None:
    session = _SequenceSession([aiohttp.ClientConnectionError("down"), _successful_response()])
    target = tmp_path / "bundle"
    target.write_bytes(b"old")

    async def cancelled_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(bundle.asyncio, "sleep", cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            bundle.download_deobfuscate_bundle(
                "https://example.test/bundle",
                tmp_path,
                "bundle",
                {},
                config=_retry_config(),
                session=session,
            )
        )
    assert session.calls == 1
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".bundle.*.tmp"))


def _worker_config() -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        ASSET_LOCAL_EXTRACTED_DIR=None,
        UNITY_VERSION=None,
        REQUEST_TIMEOUT=1,
        DOWNLOAD_MAX_RETRIES=1,
        MAX_CONCURRENCY_DOWNLOADS=1,
        MAX_CONCURRENCY_EXTRACTS=1,
        MAX_CONCURRENCY_UPLOAD_STAGE=1,
        PIPELINE_STAGE_QUEUE_SIZE=1,
        ASSET_REMOTE_STORAGE=[],
    )


def test_download_stage_cancellation_removes_temporary_destination(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def blocked_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"partial")
        captured["path"] = Path(root) / relative
        started.set()
        await release.wait()

    monkeypatch.setattr(worker, "download_deobfuscate_bundle", blocked_download)
    queue = asyncio.Queue()
    output = asyncio.Queue()
    queue.put_nowait(("url", {"bundleName": "bundle"}))

    async def run():
        task = asyncio.create_task(
            worker._download_stage(
                "id",
                "download",
                queue,
                output,
                _worker_config(),
                {},
                None,
                [],
                asyncio.Lock(),
                None,
                object(),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not captured["path"].exists()


def test_run_pipeline_cancellation_cleans_inflight_temporary_destination(monkeypatch) -> None:
    started = asyncio.Event()
    captured = {}

    async def blocked_download(_url, root, relative, **_kwargs):
        captured["path"] = Path(root) / relative
        await root.joinpath(relative).write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "download_deobfuscate_bundle", blocked_download)

    async def run():
        task = asyncio.create_task(
            worker.run_pipeline([("url", {"bundleName": "bundle"})], _worker_config(), {})
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not captured["path"].exists()


def test_download_uses_async_open_file_for_temp_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _unityfs()
    target = tmp_path / "bundle"
    target.write_bytes(b"old")
    response = _Response(data)
    config = SimpleNamespace(DOWNLOAD_MAX_RETRIES=1, REQUEST_TIMEOUT=1)
    created_descriptors: list[int] = []
    original_mkstemp = bundle.tempfile.mkstemp
    open_file_calls: list[object] = []
    original_open_file = bundle.open_file

    def record_mkstemp(*args, **kwargs):
        descriptor, temporary_name = original_mkstemp(*args, **kwargs)
        created_descriptors.append(descriptor)
        return descriptor, temporary_name

    async def record_open_file(file, *args, **kwargs):
        open_file_calls.append(file)
        return await original_open_file(file, *args, **kwargs)

    monkeypatch.setattr(bundle.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(bundle, "open_file", record_open_file)

    def fail_fdopen(*_args, **_kwargs):
        raise AssertionError("sync os.fdopen used in async download path")

    monkeypatch.setattr(bundle.os, "fdopen", fail_fdopen)

    asyncio.run(
        bundle.download_deobfuscate_bundle(
            "https://example.test/bundle",
            AnyioPath(tmp_path),
            "bundle",
            {},
            config=config,
            session=_Session(response),
        )
    )

    assert target.read_bytes() == data
    assert open_file_calls
    assert open_file_calls[0] in created_descriptors
    for descriptor in created_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not list(tmp_path.glob(".bundle.*.tmp"))
