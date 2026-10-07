from types import SimpleNamespace

import pytest

from updater import runtime as bundle_runtime


class FakeExecutor:
    instances: list["FakeExecutor"] = []

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self.instances.append(self)

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


@pytest.mark.parametrize("value", [None, "invalid", 0, -1])
def test_sanitize_concurrency_rejects_invalid_values(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        bundle_runtime.sanitize_concurrency(value)


def test_runtime_reuses_and_resizes_semaphores() -> None:
    runtime = bundle_runtime.BundleRuntime()

    first = runtime.audio_file_semaphore(SimpleNamespace(MAX_CONCURRENT_AUDIO_FILES=2))
    reused = runtime.audio_file_semaphore(SimpleNamespace(MAX_CONCURRENT_AUDIO_FILES=2))
    resized = runtime.audio_file_semaphore(SimpleNamespace(MAX_CONCURRENT_AUDIO_FILES=3))

    assert reused is first
    assert resized is not first


def test_runtime_reuses_replaces_and_shuts_down_process_pools(monkeypatch) -> None:
    FakeExecutor.instances.clear()
    monkeypatch.setattr(bundle_runtime, "ProcessPoolExecutor", FakeExecutor)
    runtime = bundle_runtime.BundleRuntime()

    first = runtime.extract_process_pool(SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=2))
    reused = runtime.extract_process_pool(SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=2))
    replacement = runtime.extract_process_pool(SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=3))

    assert reused is first
    assert replacement is not first
    assert first.shutdown_calls == [(False, False)]

    runtime.shutdown()

    assert replacement.shutdown_calls == [(False, False)]
    assert runtime._extract_process_pool is None


def test_extract_executor_kind_selects_thread_pool(monkeypatch) -> None:
    FakeExecutor.instances.clear()
    monkeypatch.setattr(bundle_runtime, "ProcessPoolExecutor", FakeExecutor)

    class FakeThreadExecutor(FakeExecutor):
        def __init__(self, max_workers=None, thread_name_prefix=""):
            super().__init__(max_workers=max_workers)
            self.thread_name_prefix = thread_name_prefix

    monkeypatch.setattr(bundle_runtime, "ThreadPoolExecutor", FakeThreadExecutor)
    runtime = bundle_runtime.BundleRuntime()

    process_pool = runtime.extract_process_pool(
        SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=2, EXTRACT_EXECUTOR="process")
    )
    assert isinstance(process_pool, FakeExecutor)
    assert not isinstance(process_pool, FakeThreadExecutor)

    thread_pool = runtime.extract_process_pool(
        SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=2, EXTRACT_EXECUTOR="thread")
    )
    assert isinstance(thread_pool, FakeThreadExecutor)
    # Switching kinds replaced the pool and shut down the old one.
    assert process_pool.shutdown_calls == [(False, False)]

    reused = runtime.extract_process_pool(
        SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=2, EXTRACT_EXECUTOR="thread")
    )
    assert reused is thread_pool
    runtime.shutdown()


def test_extract_executor_kind_rejects_unknown_value() -> None:
    config = SimpleNamespace(EXTRACT_EXECUTOR="fork")
    with pytest.raises(ValueError, match="EXTRACT_EXECUTOR"):
        bundle_runtime.get_extract_executor_kind(config)


def test_extract_core_and_media_concurrency_defaults() -> None:
    legacy = SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=4)
    assert bundle_runtime.get_extract_core_concurrency(legacy) == 4
    assert bundle_runtime.get_extract_media_concurrency(legacy) == 4

    split = SimpleNamespace(EXTRACT_CORE_CONCURRENCY=2, EXTRACT_MEDIA_CONCURRENCY=1)
    assert bundle_runtime.get_extract_core_concurrency(split) == 2
    assert bundle_runtime.get_extract_media_concurrency(split) == 1


@pytest.mark.parametrize(
    ("setting", "get_concurrency"),
    [
        ("EXTRACT_CORE_CONCURRENCY", bundle_runtime.get_extract_core_concurrency),
        ("EXTRACT_MEDIA_CONCURRENCY", bundle_runtime.get_extract_media_concurrency),
    ],
)
@pytest.mark.parametrize("value", ["invalid", 0, -1])
def test_extract_pool_concurrency_rejects_invalid_overrides(
    setting, get_concurrency, value
) -> None:
    config = SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=4, **{setting: value})

    with pytest.raises(ValueError, match="positive integer"):
        get_concurrency(config)


def test_extract_pool_for_routes_cost_class_and_caches(monkeypatch) -> None:
    FakeExecutor.instances.clear()
    monkeypatch.setattr(bundle_runtime, "ProcessPoolExecutor", FakeExecutor)
    runtime = bundle_runtime.BundleRuntime()
    config = SimpleNamespace(
        MAX_CONCURRENCY_EXTRACTS=8,
        EXTRACT_CORE_CONCURRENCY=3,
        EXTRACT_MEDIA_CONCURRENCY=1,
    )

    core = runtime.extract_pool_for(config, "light")
    default = runtime.extract_pool_for(config, None)
    media = runtime.extract_pool_for(config, "media")

    assert core is default
    assert core is not media
    assert core.max_workers == 3
    assert media.max_workers == 1
    assert runtime.extract_pool_for(config, "media") is media
    assert runtime.extract_pool_for(config, "light") is core

    runtime.shutdown()

    assert runtime._extract_process_pool is None
    assert runtime._media_extract_pool is None
    assert core.shutdown_calls == [(False, False)]
    assert media.shutdown_calls == [(False, False)]


def test_extract_pool_for_uses_default_for_explicit_none_concurrency(monkeypatch) -> None:
    FakeExecutor.instances.clear()
    monkeypatch.setattr(bundle_runtime, "ProcessPoolExecutor", FakeExecutor)
    runtime = bundle_runtime.BundleRuntime()
    config = SimpleNamespace(
        MAX_CONCURRENCY_EXTRACTS=4,
        EXTRACT_CORE_CONCURRENCY=None,
        EXTRACT_MEDIA_CONCURRENCY=None,
    )

    fixed = runtime.extract_pool_for(config, None)
    core = runtime.extract_pool_for(config, "light")
    media = runtime.extract_pool_for(config, "media")

    assert fixed is core
    assert core is not media
    assert core.max_workers == 4
    assert media.max_workers == 4

    runtime.shutdown()
