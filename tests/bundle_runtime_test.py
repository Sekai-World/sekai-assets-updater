from types import SimpleNamespace

import pytest

import bundle_runtime


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
    with pytest.raises(ValueError, match="EXTRACT_EXECUTOR"):
        bundle_runtime.get_extract_executor_kind(SimpleNamespace(EXTRACT_EXECUTOR="fork"))
