from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientTimeout
from anyio import Path as AnyioPath

import asset_bundle_info
import bundle
import helpers
import main
import worker


class _Response:
    def __init__(self, status=200, *, body=b"", json_value=None, headers=None):
        self.status = status
        self.body = body
        self.json_value = json_value
        self.headers = _Headers(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.json_value

    async def read(self):
        return self.body


class _Headers(dict):
    def getall(self, name, default=None):
        value = self.get(name)
        if value is None:
            return [] if default is None else default
        return [value]


class _Session:
    instances = []
    responses = []

    def __init__(self, **options):
        self.options = options
        self.calls = []
        self.__class__.instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.__class__.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.__class__.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _config(**overrides):
    values = {
        "PROXY_URL": "http://proxy.test:8080",
        "REQUEST_TIMEOUT": 17,
        "USER_AGENT": "public-agent",
        "UNITY_VERSION": "2024.1",
        "GAME_COOKIE_URL": "https://cookie.test/session?token=cookie-secret",
        "GAME_VERSION_JSON_URL": "https://meta.test/version",
        "GAME_VERSION_URL": None,
        "ASSET_VER_URL": None,
        "ASSET_BUNDLE_INFO_URL": "https://cdn.test/info?Signature=asset-secret",
        "AES_KEY": b"key",
        "AES_IV": b"iv",
        "REGION": "jp",
        "APP_VERSION_OVERRIDE": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_common_http_options_include_proxy_and_configured_timeout():
    options = helpers.get_http_session_options(_config())

    assert options["proxy"] == "http://proxy.test:8080"
    assert isinstance(options["timeout"], ClientTimeout)
    assert options["timeout"].total == 17  # type: ignore[union-attr]


def test_download_http_options_omit_proxy_and_keep_configured_timeout():
    options = helpers.get_download_http_session_options(_config())

    assert "proxy" not in options
    assert isinstance(options["timeout"], ClientTimeout)
    assert options["timeout"].total == 17  # type: ignore[union-attr]


def test_worker_cdn_session_omits_proxy(monkeypatch):
    _Session.instances.clear()
    monkeypatch.setattr(worker.aiohttp, "ClientSession", _Session)
    config = SimpleNamespace(
        PROXY_URL="http://proxy.test:8080",
        REQUEST_TIMEOUT=17,
        MAX_CONCURRENCY_DOWNLOADS=1,
        MAX_CONCURRENCY_EXTRACTS=1,
        MAX_CONCURRENCY_UPLOAD_STAGE=1,
        PIPELINE_STAGE_QUEUE_SIZE=1,
    )

    assert asyncio.run(worker.run_pipeline([], config, {})) == []

    session = _Session.instances[-1]
    assert "proxy" not in session.options
    assert session.options["timeout"].total == 17  # type: ignore[union-attr]


def test_public_headers_are_metadata_only_and_cookie_cdn_headers_are_separate():
    config = _config(GAME_COOKIE_URL=None)

    assert helpers.build_metadata_headers(config) == {
        "Accept": "*/*",
        "X-Unity-Version": "2024.1",
        "User-Agent": "public-agent",
    }
    assert helpers.build_cookie_request_headers() == {}
    assert helpers.build_cdn_headers("CloudFront-Policy=private") == {
        "Cookie": "CloudFront-Policy=private"
    }
    assert "User-Agent" not in helpers.build_cdn_headers()


def test_cookie_request_uses_common_options_without_public_headers(monkeypatch):
    _Session.instances.clear()
    _Session.responses[:] = [_Response(headers={"Set-Cookie": "CloudFront-Policy=policy; Path=/"})]
    monkeypatch.setattr(helpers.aiohttp, "ClientSession", _Session)

    config = _config()
    headers, cookie = asyncio.run(helpers.refresh_cookie(config, {}))

    session = _Session.instances[-1]
    assert session.options["proxy"] == config.PROXY_URL
    assert session.options["timeout"].total == config.REQUEST_TIMEOUT  # type: ignore[union-attr]
    assert session.calls[0][2]["headers"] == {}
    assert headers["Cookie"] == cookie == "CloudFront-Policy=policy"


@pytest.mark.parametrize("target", ["metadata", "cookie"])
def test_http_transport_errors_are_sanitized(monkeypatch, target):
    secret = "transport-body-secret"

    class _FailingSession:
        def __init__(self, **_options):
            pass

        def get(self, *_args, **_kwargs):
            raise asset_bundle_info.aiohttp.ClientConnectionError(
                f"signed URL https://cdn.test/?token={secret} body={secret}"
            )

        def post(self, *_args, **_kwargs):
            raise helpers.aiohttp.ClientConnectionError(
                f"Cookie: a={secret}; Authorization: Bearer {secret}"
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    config = _config()
    if target == "metadata":
        monkeypatch.setattr(asset_bundle_info.aiohttp, "ClientSession", _FailingSession)
        request = asset_bundle_info.fetch_asset_bundle_info(config)
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(request)
    else:
        monkeypatch.setattr(helpers.aiohttp, "ClientSession", _FailingSession)
        request = helpers.refresh_cookie(config, {})
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(request)

    assert secret not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_main_pipeline_boundary_does_not_log_raw_transport_exception(monkeypatch, caplog):
    secret = "top-level-body-secret"

    async def failing_pipeline(*_args, **_kwargs):
        raise RuntimeError(f"signed URL https://cdn.test/?token={secret} body={secret}")

    monkeypatch.setattr(main, "run_pipeline", failing_pipeline)
    config = SimpleNamespace()
    paths = SimpleNamespace(queue="pending.json")

    with caplog.at_level(logging.ERROR):
        download = main.do_download([], config, {}, None, paths)
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(download)

    assert secret not in caplog.text
    assert secret not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_metadata_requests_use_common_options_and_public_headers(monkeypatch):
    _Session.instances.clear()
    _Session.responses[:] = [
        _Response(json_value={"appVersion": "1", "dataVersion": "2", "assetVersion": "3"}),
        _Response(body=b"asset-info"),
    ]
    monkeypatch.setattr(asset_bundle_info.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(asset_bundle_info, "unpack", lambda *_args: {"version": "v", "bundles": {}})

    asyncio.run(
        asset_bundle_info.fetch_asset_bundle_info(
            _config(),  # type: ignore[arg-type]
            headers=helpers.build_metadata_headers(_config()),
        )
    )

    assert len(_Session.instances) == 2
    for session in _Session.instances:
        assert session.options["proxy"] == "http://proxy.test:8080"
        assert session.options["timeout"].total == 17  # type: ignore[union-attr]
    assert _Session.instances[0].calls[0][2]["headers"]["User-Agent"] == "public-agent"


def test_http_logs_redact_headers_urls_and_response_bodies(monkeypatch, caplog):
    _Session.instances.clear()
    _Session.responses[:] = [
        _Response(json_value={"appVersion": "1", "dataVersion": "2", "assetVersion": "3"}),
        _Response(
            status=503,
            body=b"response-body-secret",
            headers={"X-Api-Key": "header-secret", "Content-Type": "text/plain"},
        ),
    ]
    monkeypatch.setattr(asset_bundle_info.aiohttp, "ClientSession", _Session)
    config = _config(
        ASSET_BUNDLE_INFO_URL=("https://cdn.test/info?Signature=url-secret&assetVersion=3")
    )

    with caplog.at_level(logging.DEBUG, logger="asset_updater"):
        request = asset_bundle_info.fetch_asset_bundle_info(
            config,
            headers={
                "Cookie": "cookie-secret",
                "Authorization": "authorization-secret",
                "X-Api-Key": "header-secret",
            },
        )
        with pytest.raises(RuntimeError):
            asyncio.run(request)

    records = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        "cookie-secret",
        "authorization-secret",
        "header-secret",
        "url-secret",
        "response-body-secret",
    ):
        assert secret not in records


def test_download_retry_logs_redact_signed_url_and_exception_text(monkeypatch, tmp_path, caplog):
    signed_url = "https://cdn.test/bundle?Signature=url-secret&token=query-secret"
    exception_url = "request failed for https://origin.test/?token=exception-secret"

    class _FailingSession:
        def get(self, *_args, **_kwargs):
            raise bundle.aiohttp.ClientConnectionError(exception_url)

    config = SimpleNamespace(
        DOWNLOAD_MAX_RETRIES=2,
        DOWNLOAD_RETRY_BASE_DELAY=0,
        DOWNLOAD_RETRY_MAX_DELAY=0,
        REQUEST_TIMEOUT=1,
    )
    monkeypatch.setattr(bundle.random, "uniform", lambda *_args: 0)

    download = bundle.download_deobfuscate_bundle(
        signed_url, AnyioPath(tmp_path), "bundle", {}, config=config, session=_FailingSession()
    )
    with caplog.at_level(logging.WARNING, logger="live2d"):
        with pytest.raises(bundle.RetryableDownloadError):
            asyncio.run(download)

    records = "\n".join(record.getMessage() for record in caplog.records)
    assert "url-secret" not in records
    assert "query-secret" not in records
    assert "exception-secret" not in records


def test_worker_download_reuses_pipeline_cookie_for_cdn_headers(monkeypatch, tmp_path):
    download_mock = AsyncMock()
    monkeypatch.setattr(worker, "download_deobfuscate_bundle", download_mock)

    refresh_mock = AsyncMock()
    monkeypatch.setattr(worker, "refresh_cookie", refresh_mock, raising=False)

    config = SimpleNamespace(ASSET_LOCAL_BUNDLE_CACHE_DIR=None)
    input_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue()
    input_queue.put_nowait(("https://cdn.test/bundle", {"bundleName": "music/example"}))
    input_queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._download_stage(
            "test",
            "download",
            input_queue,
            extract_queue,
            config,
            {"User-Agent": "public-agent", "X-Unity-Version": "2024.1"},
            "old-cookie",
            [],
            asyncio.Lock(),
            None,
            AsyncMock(),
        )

    asyncio.run(run())

    refresh_mock.assert_not_awaited()
    download_kwargs = download_mock.await_args.kwargs
    assert download_kwargs["headers"] == {"Cookie": "old-cookie"}


def test_worker_download_exhaustion_does_not_log_sensitive_network_exception(
    monkeypatch, tmp_path, caplog
):
    signed_url = "https://cdn.test/bundle?Signature=worker-url-secret"
    network_error = (
        "request failed for "
        "https://origin.test/?token=exception-url-secret "
        'Cookie: a=cookie-a-secret; b="cookie-b-secret" '
        "token=standalone-token-secret, api_key='api-key-secret' "
        "access_token=access-token-secret; api_token=api-token-secret "
        'api-token=api-hyphen-token-secret; X-Api-Token: "x-api-token-secret" '
        "X-Access-Token='x-access-token-secret' "
        "X-Api-Key: \"x-api-key-secret\"; X_Api_Key='x-underscore-api-key-secret' "
        "x-api-key=lower-hyphen-api-key-secret, x_api_key=lower-underscore-api-key-secret "
        "Authorization: Bearer authorization-secret"
    )

    class _FailingSession:
        def get(self, *_args, **_kwargs):
            raise bundle.aiohttp.ClientConnectionError(network_error)

    async def exhausted_download(*args, **kwargs):
        kwargs["session"] = _FailingSession()
        try:
            return await bundle.download_deobfuscate_bundle(*args, **kwargs)
        except Exception as exc:
            final_exception.append(exc)
            raise

    monkeypatch.setattr(worker, "download_deobfuscate_bundle", exhausted_download)
    final_exception = []
    config = SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        DOWNLOAD_MAX_RETRIES=1,
        DOWNLOAD_RETRY_BASE_DELAY=0,
        DOWNLOAD_RETRY_MAX_DELAY=0,
        REQUEST_TIMEOUT=1,
    )
    input_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue()
    input_queue.put_nowait((signed_url, {"bundleName": "music/example"}))
    input_queue.put_nowait(worker._QUEUE_SENTINEL)
    failed_tasks = []

    async def run() -> None:
        await worker._download_stage(
            "test",
            "download",
            input_queue,
            extract_queue,
            config,
            {"User-Agent": "public-agent", "X-Unity-Version": "2024.1"},
            None,
            failed_tasks,
            asyncio.Lock(),
            None,
            AsyncMock(),
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())

    records = caplog.text
    for secret in (
        "worker-url-secret",
        "exception-url-secret",
        "cookie-a-secret",
        "cookie-b-secret",
        "standalone-token-secret",
        "api-key-secret",
        "access-token-secret",
        "api-token-secret",
        "api-hyphen-token-secret",
        "x-api-token-secret",
        "x-access-token-secret",
        "x-api-key-secret",
        "x-underscore-api-key-secret",
        "lower-hyphen-api-key-secret",
        "lower-underscore-api-key-secret",
        "authorization-secret",
    ):
        assert secret not in records
        assert secret not in str(final_exception[0])
    assert final_exception[0].__context__ is None
    assert final_exception[0].__cause__ is None
    assert len(failed_tasks) == 1


def test_worker_fallback_label_is_sanitized(monkeypatch, caplog):
    secret = "fallback-label-secret"
    download_mock = AsyncMock(side_effect=RuntimeError("download failed"))
    monkeypatch.setattr(worker, "download_deobfuscate_bundle", download_mock)
    config = SimpleNamespace(ASSET_LOCAL_BUNDLE_CACHE_DIR=None)
    input_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue()
    input_queue.put_nowait((f"https://cdn.test/?token={secret}", {}))
    input_queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._download_stage(
            "test",
            "download",
            input_queue,
            extract_queue,
            config,
            {},
            None,
            [],
            asyncio.Lock(),
            None,
            AsyncMock(),
        )

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert secret not in caplog.text
