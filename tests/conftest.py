"""Shared fixtures for the focused pipeline tests.

The project intentionally does not need a pytest async plugin for these
fixtures.  Async production functions can be exercised with ``asyncio.run``
while the local HTTP server runs in its own event-loop thread.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from collections.abc import Callable, Generator, Mapping
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from anyio import Path as AnyioPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from updater import pipeline  # noqa: E402  (requires PROJECT_ROOT on sys.path)


def pipeline_config(extracted_root: Path | None) -> SimpleNamespace:
    """Minimal single-worker pipeline stage configuration for integration tests."""

    return SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        ASSET_LOCAL_EXTRACTED_DIR=AnyioPath(extracted_root) if extracted_root else None,
        ASSET_REMOTE_STORAGE=[
            {"type": "normal", "base": "remote", "program": "uploader", "args": []}
        ],
        UNITY_VERSION=None,
        MAX_CONCURRENCY_DOWNLOADS=1,
        MAX_CONCURRENCY_EXTRACTS=1,
        MAX_CONCURRENCY_UPLOAD_STAGE=1,
        PIPELINE_STAGE_QUEUE_SIZE=1,
        MAX_CONCURRENCY_UPLOADS=1,
        REQUEST_TIMEOUT=1,
    )


def install_pipeline_fakes(
    monkeypatch: pytest.MonkeyPatch,
    contents: dict[str, bytes],
    uploads: list[tuple[str, Path, Path, bytes]],
    *,
    failing_url: str | None = None,
) -> None:
    """Replace the download, extract, and upload seams with in-memory fakes."""

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_extract(bundle_path, bundle, output_root, **_kwargs):
        output = output_root / "shared.txt"
        await output.write_bytes(contents[bundle["bundleName"]])
        return [output]

    async def fake_upload(files, root, *_args, **_kwargs):
        assert len(files) == 1
        exported_file = files[0]
        root_path = Path((await root.resolve()).as_posix())
        exported_path = Path((await exported_file.resolve()).as_posix())
        assert exported_path.parent == root_path
        data = await exported_file.read_bytes()
        uploads.append((exported_path.name, root_path, exported_path, data))
        if uploads[-1][1].name and failing_url is not None:
            if data == contents["failed"]:
                raise RuntimeError("synthetic upload failure")

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)


class LocalAiohttpServer:
    """Small, dependency-free-in-tests aiohttp server running on localhost."""

    def __init__(self, routes: Mapping[str, Callable[..., Any]]) -> None:
        self._routes = routes
        self._loop = asyncio.new_event_loop()
        self._runner: web.AppRunner | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self.requests: list[tuple[str, str]] = []
        self.url = ""

        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.close()
            raise RuntimeError("Timed out starting local aiohttp server")
        if self._startup_error is not None:
            error = self._startup_error
            self.close()
            raise RuntimeError("Failed to start local aiohttp server") from error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start())
        except BaseException as error:  # pragma: no cover - startup-only guard
            self._startup_error = error
            self._ready.set()
            self._loop.close()
            return

        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    async def _start(self) -> None:
        app = web.Application()
        for path, handler in self._routes.items():
            app.router.add_route("*", path, self._wrap_handler(handler))

        runner = web.AppRunner(app)
        self._runner = runner
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        socket = site._server.sockets[0]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{socket.getsockname()[1]}"

    def _wrap_handler(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            self.requests.append((request.method, request.path))
            result = handler(request)
            if inspect.isawaitable(result):
                return await result
            return result

        return wrapped

    def close(self) -> None:
        if not self._thread.is_alive():
            return

        async def cleanup() -> None:
            if self._runner is not None:
                await self._runner.cleanup()
            self._loop.call_soon(self._loop.stop)

        asyncio.run_coroutine_threadsafe(cleanup(), self._loop).result(timeout=5)
        self._thread.join(timeout=5)

    def __enter__(self) -> LocalAiohttpServer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class FakeSubprocess:
    """Recorder for subprocess-based codecs and upload commands."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        async def create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeSubprocess:
            self.calls.append((args, kwargs))
            return self

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """A named alias for pytest's isolated temporary directory fixture."""

    return tmp_path


@pytest.fixture
def local_aiohttp_server() -> Generator[Callable[..., LocalAiohttpServer], None, None]:
    """Create one or more localhost aiohttp servers and clean them up after a test."""

    servers: list[LocalAiohttpServer] = []

    def factory(
        routes: Mapping[str, Callable[..., Any]] | None = None,
        *,
        handler: Callable[..., Any] | None = None,
    ) -> LocalAiohttpServer:
        if routes is None:
            routes = {"/": handler or (lambda _request: web.Response(body=b"ok"))}
        server = LocalAiohttpServer(routes)
        servers.append(server)
        return server

    try:
        yield factory
    finally:
        for server in reversed(servers):
            server.close()


@pytest.fixture
def aiohttp_server(
    local_aiohttp_server: Callable[..., LocalAiohttpServer],
) -> Callable[..., LocalAiohttpServer]:
    """Compatibility alias for tests that use the shorter fixture name."""

    return local_aiohttp_server


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeSubprocess]:
    """Install a recording ``asyncio.create_subprocess_exec`` implementation."""

    def factory(**kwargs: Any) -> FakeSubprocess:
        return FakeSubprocess(monkeypatch, **kwargs)

    return factory


@pytest.fixture
def synthetic_unityfs_paths() -> Mapping[str, str]:
    """Representative UnityFS container paths used by extraction tests."""

    return {
        "container": "assets/sekai/assetbundle/resources/characters/unit.prefab",
        "builtin": "assets/sekai/builtinassets/assetbundle/resources/shared.mat",
        "builtin_alt": "assets/sekai/builtinassets/resources/common.asset",
    }


@pytest.fixture
def unityfs_path() -> Callable[..., PurePosixPath]:
    """Build a synthetic UnityFS path without requiring a Unity bundle fixture."""

    def factory(*parts: str, root: str = "assets/sekai/assetbundle/resources") -> PurePosixPath:
        return PurePosixPath(root, *parts)

    return factory
