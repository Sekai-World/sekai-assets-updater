from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bundle
import helpers
import main


class _HangingProcess:
    def __init__(self, *, terminate_exits: bool) -> None:
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.terminate_calls = 0
        self.wait_calls = 0
        self._terminate_exits = terminate_exits
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_called = True
        self.terminate_calls += 1
        if self._terminate_exits:
            self.returncode = -15
            self._exited.set()

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._exited.wait()
        return self.returncode or 0

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._exited.wait()
        return b"", b""


class _ArtifactProcess:
    def __init__(self, output_path: Path | None, *, communicate: bool = False) -> None:
        self.returncode: int | None = None
        self.order: list[str] = []
        self._communicate = communicate
        self._exited = asyncio.Event()
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"partial")

    def terminate(self) -> None:
        self.order.append("terminate")

    def kill(self) -> None:
        self.order.append("kill")
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        self.order.append("wait")
        await self._exited.wait()
        return self.returncode or 0

    async def communicate(self) -> tuple[bytes, bytes]:
        assert self._communicate
        self.order.append("communicate")
        await self._exited.wait()
        return b"", b""


@pytest.mark.parametrize("terminate_exits", [True, False])
def test_wait_timeout_terminates_and_waits_for_process(monkeypatch, terminate_exits: bool) -> None:
    process = _HangingProcess(terminate_exits=terminate_exits)
    monkeypatch.setattr(bundle, "_EXTERNAL_PROCESS_TERMINATE_GRACE", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bundle._wait_for_process(process, 0.001))

    assert process.terminate_called
    assert process.kill_called is (not terminate_exits)
    assert process.returncode is not None


def test_wait_cancellation_terminates_before_reraising() -> None:
    process = _HangingProcess(terminate_exits=True)

    async def scenario() -> None:
        task = asyncio.create_task(bundle._wait_for_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminate_called
    assert process.returncode is not None


def test_helpers_wait_cancellation_terminates_before_reraising() -> None:
    process = _HangingProcess(terminate_exits=True)

    async def scenario() -> None:
        task = asyncio.create_task(helpers._wait_for_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminate_called
    assert process.returncode is not None


def test_communicate_cancellation_terminates_before_reraising() -> None:
    process = _HangingProcess(terminate_exits=True)

    async def scenario() -> None:
        task = asyncio.create_task(bundle._communicate_with_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminate_called
    assert process.returncode is not None


@pytest.mark.parametrize("cancel", [False, True])
def test_ffmpeg_video_partial_output_is_cleaned_after_timeout_or_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    output = tmp_path / "movie.mp4"
    output.write_bytes(b"existing")
    process_holder: dict[str, _ArtifactProcess] = {}

    async def create_process(*args, **_kwargs):
        process = _ArtifactProcess(Path(args[-1]))
        process_holder["process"] = process
        return process

    monkeypatch.setattr(bundle, "_get_ffmpeg_video_encoder", _no_hardware_encoder)
    monkeypatch.setattr(bundle.asyncio, "create_subprocess_exec", create_process)
    config = SimpleNamespace(EXTERNAL_PROCESS_TIMEOUT=10)

    async def scenario() -> None:
        process, _ = await bundle._run_ffmpeg_video_to_mp4(
            bundle.Path((tmp_path / "movie.m2v").as_posix()), bundle.Path(output.as_posix()), config
        )
        if cancel:
            task = asyncio.create_task(bundle._wait_for_process(process, 10))
            await asyncio.sleep(0)
            expected = asyncio.CancelledError
            task.cancel()
        else:
            task = asyncio.create_task(bundle._wait_for_process(process, 0.001))
            expected = asyncio.TimeoutError
        with pytest.raises(expected):
            await task

    monkeypatch.setattr(bundle, "_EXTERNAL_PROCESS_TERMINATE_GRACE", 0.001)
    asyncio.run(scenario())
    process = process_holder["process"]
    assert process.order == ["wait", "terminate", "wait", "kill", "wait"]
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".ffmpeg-*"))


def _no_hardware_encoder(_config):
    async def result():
        return None, []

    return result()


@pytest.mark.parametrize("cancel", [False, True])
def test_ffmpeg_audio_partial_output_is_cleaned_after_timeout_or_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    output = tmp_path / "audio.mp3"
    output.write_bytes(b"existing")
    process_holder: dict[str, _ArtifactProcess] = {}

    async def create_process(*args, **_kwargs):
        process = _ArtifactProcess(Path(args[-1]))
        process_holder["process"] = process
        return process

    monkeypatch.setattr(bundle.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(bundle, "_EXTERNAL_PROCESS_TERMINATE_GRACE", 0.001)

    async def scenario() -> None:
        task = asyncio.create_task(
            bundle._run_ffmpeg_audio_encode(
                bundle.Path((tmp_path / "audio.wav").as_posix()),
                bundle.Path(output.as_posix()),
                SimpleNamespace(
                    MAX_CONCURRENCY_AUDIO_ENCODERS=1,
                    EXTERNAL_PROCESS_TIMEOUT=0.001 if not cancel else 10,
                ),
            )
        )
        if cancel:
            await asyncio.sleep(0)
            expected = asyncio.CancelledError
            task.cancel()
        else:
            expected = asyncio.TimeoutError
        with pytest.raises(expected):
            await task

    asyncio.run(scenario())
    process = process_holder["process"]
    assert process.order == ["wait", "terminate", "wait", "kill", "wait"]
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".ffmpeg-*"))


@pytest.mark.parametrize("cancel", [False, True])
def test_vgmstream_partial_output_is_cleaned_after_timeout_or_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    output = tmp_path / "audio.wav"
    output.write_bytes(b"existing")
    process_holder: dict[str, _ArtifactProcess] = {}

    async def create_process(*args, **_kwargs):
        output_path = Path(args[args.index("-o") + 1])
        process = _ArtifactProcess(output_path, communicate=True)
        process_holder["process"] = process
        return process

    monkeypatch.setattr(bundle, "_get_vgmstream_cli", lambda: "vgmstream-cli")
    monkeypatch.setattr(bundle.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(bundle, "_EXTERNAL_PROCESS_TERMINATE_GRACE", 0.001)

    async def scenario() -> None:
        task = asyncio.create_task(
            bundle._run_hca_to_wav_with_vgmstream(
                bundle.Path((tmp_path / "audio.hca").as_posix()),
                bundle.Path(output.as_posix()),
                SimpleNamespace(EXTERNAL_PROCESS_TIMEOUT=0.001 if not cancel else 10),
            )
        )
        if cancel:
            await asyncio.sleep(0)
            expected = asyncio.CancelledError
            task.cancel()
        else:
            expected = asyncio.TimeoutError
        with pytest.raises(expected):
            await task

    asyncio.run(scenario())
    process = process_holder["process"]
    assert process.order == ["communicate", "terminate", "wait", "kill", "wait"]
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".hca-*"))


def test_vgmstream_success_removes_nonempty_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "audio.wav"

    class _SuccessfulVgmstreamProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_process(*args, **_kwargs):
        staged_output = Path(args[args.index("-o") + 1])
        staged_output.write_bytes(b"decoded")
        (staged_output.parent / "decoder.log").write_text("diagnostic")
        return _SuccessfulVgmstreamProcess()

    monkeypatch.setattr(bundle, "_get_vgmstream_cli", lambda: "vgmstream-cli")
    monkeypatch.setattr(bundle.asyncio, "create_subprocess_exec", create_process)

    decoded = asyncio.run(
        bundle._run_hca_to_wav_with_vgmstream(
            bundle.Path((tmp_path / "audio.hca").as_posix()),
            bundle.Path(output.as_posix()),
            SimpleNamespace(EXTERNAL_PROCESS_TIMEOUT=1),
        )
    )

    assert decoded is True
    assert output.read_bytes() == b"decoded"
    assert not list(tmp_path.glob(".hca-*"))


@pytest.mark.parametrize("terminate_exits", [True, False])
def test_upload_timeout_cleans_up_process_and_redacts_remote_query(
    tmp_path, monkeypatch, caplog, terminate_exits
) -> None:
    source = tmp_path / "asset.bin"
    source.write_bytes(b"asset")
    process = _HangingProcess(terminate_exits=terminate_exits)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(helpers.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(helpers, "_EXTERNAL_PROCESS_TERMINATE_GRACE", 0.01)
    config = SimpleNamespace(EXTERNAL_PROCESS_TIMEOUT=0.001)

    with caplog.at_level("DEBUG", logger="asset_updater"):
        with pytest.raises(RuntimeError, match=r"1 upload\(s\) failed"):
            asyncio.run(
                helpers.upload_to_storage(
                    [source],
                    tmp_path,
                    "remote:bucket?Signature=remote-secret",
                    "rclone",
                    ["copyto", "src", "dst", "--header", "secret-arg"],
                    config=config,
                )
            )

    assert process.terminate_called
    assert process.kill_called is (not terminate_exits)
    assert process.returncode is not None
    records = "\n".join(record.getMessage() for record in caplog.records)
    assert "remote:bucket?Signature=%3Credacted%3E" in records
    assert "remote-secret" not in records
    assert "secret-arg" not in records


def _valid_config() -> SimpleNamespace:
    values = {
        name: 1
        for name in (
            "MAX_CONCURRENCY",
            "MAX_CONCURRENCY_DOWNLOADS",
            "MAX_CONCURRENCY_EXTRACTS",
            "MAX_CONCURRENCY_UPLOAD_STAGE",
            "PIPELINE_STAGE_QUEUE_SIZE",
            "MAX_CONCURRENT_AUDIO_FILES",
            "MAX_CONCURRENCY_HCA_DECODES",
            "MAX_CONCURRENCY_AUDIO_ENCODERS",
            "MAX_CONCURRENCY_AUDIO_TRANSCODES",
            "MAX_CONCURRENCY_VIDEO_TRANSCODES",
            "MAX_CONCURRENCY_USM_DEMUXES",
            "MAX_CONCURRENCY_UPLOADS",
        )
    }
    return SimpleNamespace(
        **values,
        EXTERNAL_PROCESS_TIMEOUT=10,
        DOWNLOAD_MAX_RETRIES=3,
        AES_KEY=b"0123456789012345",
        AES_IV=b"0123456789012345",
        HCA_DECODE_BACKEND="auto",
        ASSET_REMOTE_STORAGE=[],
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("MAX_CONCURRENCY_UPLOADS", 0, "MAX_CONCURRENCY_UPLOADS"),
        ("DOWNLOAD_MAX_RETRIES", 0, "DOWNLOAD_MAX_RETRIES"),
        ("DOWNLOAD_MAX_RETRIES", "3", "DOWNLOAD_MAX_RETRIES"),
        ("DOWNLOAD_MAX_RETRIES", 1.5, "DOWNLOAD_MAX_RETRIES"),
        ("EXTERNAL_PROCESS_TIMEOUT", 0, "EXTERNAL_PROCESS_TIMEOUT"),
        ("AES_KEY", b"short", "AES_KEY"),
        ("AES_IV", b"short", "AES_IV"),
    ],
)
def test_validate_config_rejects_invalid_values(
    field: str, value, expected: str, monkeypatch
) -> None:
    monkeypatch.setattr(main.shutil, "which", lambda _program: "/usr/bin/fake")
    config = _valid_config()
    setattr(config, field, value)

    with pytest.raises(ValueError, match=expected):
        main.validate_config(config)  # type: ignore[arg-type]


def test_validate_config_requires_selected_executables(monkeypatch) -> None:
    monkeypatch.setattr(main.shutil, "which", lambda _program: None)
    config = _valid_config()
    config.HCA_DECODE_BACKEND = "vgmstream"

    with pytest.raises(ValueError, match="vgmstream-cli"):
        main.validate_config(config)  # type: ignore[arg-type]


def test_repeated_cancel_during_terminate_runs_single_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation must not start concurrent terminate sequences."""
    process = _HangingProcess(terminate_exits=True)
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    original_terminate = bundle._terminate_process

    async def slow_terminate(proc) -> None:
        calls["n"] += 1
        started.set()
        await release.wait()
        await original_terminate(proc)

    monkeypatch.setattr(bundle, "_terminate_process", slow_terminate)

    async def scenario() -> None:
        task = asyncio.create_task(bundle._wait_for_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls["n"] == 1
    assert process.terminate_calls == 1
    assert process.wait_calls == 2  # initial wait + terminate reap wait
    assert process.returncode is not None


def test_helpers_repeated_cancel_during_terminate_runs_single_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess(terminate_exits=True)
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    original_terminate = helpers._terminate_process

    async def slow_terminate(proc) -> None:
        calls["n"] += 1
        started.set()
        await release.wait()
        await original_terminate(proc)

    monkeypatch.setattr(helpers, "_terminate_process", slow_terminate)

    async def scenario() -> None:
        task = asyncio.create_task(helpers._wait_for_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls["n"] == 1
    assert process.terminate_calls == 1
    assert process.wait_calls == 2
    assert process.returncode is not None


def test_communicate_repeated_cancel_during_terminate_runs_single_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess(terminate_exits=True)
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    original_terminate = bundle._terminate_process

    async def slow_terminate(proc) -> None:
        calls["n"] += 1
        started.set()
        await release.wait()
        await original_terminate(proc)

    monkeypatch.setattr(bundle, "_terminate_process", slow_terminate)

    async def scenario() -> None:
        task = asyncio.create_task(bundle._communicate_with_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls["n"] == 1
    assert process.terminate_calls == 1
    assert process.wait_calls == 1  # communicate path reaps via wait only in terminate
    assert process.returncode is not None


@pytest.mark.parametrize(
    ("module", "waiter", "task_attribute"),
    [
        (bundle, bundle._wait_for_process, "_bundle_terminate_task"),
        (helpers, helpers._wait_for_process, "_helpers_terminate_task"),
    ],
)
def test_cancelled_wait_preserves_cancellation_when_termination_fails(
    monkeypatch: pytest.MonkeyPatch,
    module,
    waiter,
    task_attribute: str,
) -> None:
    process = _HangingProcess(terminate_exits=True)
    termination_started = asyncio.Event()
    fail_termination = asyncio.Event()
    terminate_calls = {"count": 0}

    async def failing_terminate(proc) -> None:
        terminate_calls["count"] += 1
        termination_started.set()
        await fail_termination.wait()
        raise RuntimeError("termination failed")

    monkeypatch.setattr(module, "_terminate_process", failing_terminate)

    async def scenario() -> asyncio.CancelledError:
        task = asyncio.create_task(waiter(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        await termination_started.wait()
        task.cancel()
        fail_termination.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    cancellation = asyncio.run(scenario())
    assert cancellation.__cause__ is None
    assert cancellation.__suppress_context__
    assert cancellation.__context__ is None
    assert terminate_calls["count"] == 1
    assert process.terminate_calls == 0
    assert process._terminate_exits
    termination_task = getattr(process, task_attribute)
    assert termination_task.done()
    assert termination_task.exception() is not None


def test_cancelled_communicate_preserves_cancellation_when_termination_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess(terminate_exits=True)
    termination_started = asyncio.Event()
    fail_termination = asyncio.Event()
    terminate_calls = {"count": 0}

    async def failing_terminate(_proc) -> None:
        terminate_calls["count"] += 1
        termination_started.set()
        await fail_termination.wait()
        raise RuntimeError("termination failed")

    monkeypatch.setattr(bundle, "_terminate_process", failing_terminate)

    async def scenario() -> asyncio.CancelledError:
        task = asyncio.create_task(bundle._communicate_with_process(process, 10))
        await asyncio.sleep(0)
        task.cancel()
        await termination_started.wait()
        task.cancel()
        fail_termination.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    cancellation = asyncio.run(scenario())
    assert cancellation.__cause__ is None
    assert cancellation.__suppress_context__
    assert cancellation.__context__ is None
    assert terminate_calls["count"] == 1
    assert process.terminate_calls == 0
    termination_task = getattr(process, "_bundle_terminate_task")
    assert termination_task.done()
    assert termination_task.exception() is not None
