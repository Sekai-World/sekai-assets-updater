from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater.bundle import acb_cache as bundle_acb_cache
from updater.bundle import pipeline as bundle


class _FakeTextAsset:
    def __init__(self, script: str) -> None:
        self.m_Script = script


class _FakeUnityObject:
    def __init__(self, type_name: str, tree=None, script: str = "") -> None:
        self.type = SimpleNamespace(name=type_name)
        self.class_id = {"MonoBehaviour": 114, "TextAsset": 49}[type_name]
        self.file_index = 0
        self.path_id = id(self)
        self.serialized_type = SimpleNamespace(node=tree is not None)
        self._tree = tree
        self._script = script
        self._environment = SimpleNamespace(
            studio=SimpleNamespace(
                read_text=lambda *_args: self._script.encode("utf-8", "surrogateescape")
            )
        )

    def read(self):
        return _FakeTextAsset(self._script)

    def read_typetree(self):
        return self._tree


def _acb_unity_file(script: str):
    tree = {
        "acbFiles": [
            {
                "cueSheetName": "voice",
                "formatType": 0,
                "spilitFileNum": 0,
                "assetBundleFileName": "voice.acb.bytes",
            }
        ]
    }
    return SimpleNamespace(
        container={
            "assets/sekai/assetbundle/resources/audio/voice.asset": _FakeUnityObject(
                "MonoBehaviour", tree
            ),
            "assets/sekai/assetbundle/resources/audio/voice.acb.bytes": _FakeUnityObject(
                "TextAsset", script=script
            ),
        }
    )


def _acb_reference_unity_file():
    tree = {
        "acbFiles": [
            {
                "cueSheetName": "voice",
                "formatType": 0,
                "spilitFileNum": 0,
                "assetBundleFileName": "voice.acb.bytes",
            }
        ]
    }
    return SimpleNamespace(
        container={
            "assets/sekai/assetbundle/resources/audio/voice.asset": _FakeUnityObject(
                "MonoBehaviour", tree
            ),
        }
    )


def _movie_unity_file():
    tree = {"movieBundleDatas": [{"usmFileName": "movie.usm.bytes"}]}
    return SimpleNamespace(
        container={
            "assets/sekai/assetbundle/resources/video/movie.asset": _FakeUnityObject(
                "MonoBehaviour", tree
            ),
            "assets/sekai/assetbundle/resources/video/movie.usm.bytes": _FakeUnityObject(
                "TextAsset", script="usm"
            ),
        }
    )


def _extract_sync(bundle_path: Path, output_root: Path):
    output_root.mkdir(parents=True, exist_ok=True)
    return bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": bundle_path.stem},
        output_root.as_posix(),
        None,
        ("png",),
    )


def test_acb_audio_outputs_are_isolated_between_sibling_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_bundle = tmp_path / "bundles" / "first.bundle"
    second_bundle = tmp_path / "bundles" / "second.bundle"
    first_bundle.parent.mkdir()
    first_bundle.write_bytes(b"first bundle")
    second_bundle.write_bytes(b"second bundle")
    first_root = tmp_path / "artifacts" / "first"
    second_root = tmp_path / "artifacts" / "second"
    acb_by_bundle = {
        first_bundle.as_posix(): _acb_unity_file("first acb"),
        second_bundle.as_posix(): _acb_unity_file("second acb"),
    }
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda path, _version: acb_by_bundle[path.as_posix()],
    )

    def fake_extract_acb(_data, target_dir: str, _acb_path: str, _cue_name):
        output = Path(target_dir) / "voice.wav"
        output.write_bytes(Path(_acb_path).read_bytes())
        return [output.as_posix()]

    monkeypatch.setattr(bundle, "extract_acb", fake_extract_acb)
    first_exported, first_audio_jobs, first_video_jobs = _extract_sync(first_bundle, first_root)
    second_exported, second_audio_jobs, second_video_jobs = _extract_sync(
        second_bundle, second_root
    )

    assert first_video_jobs == second_video_jobs == []
    assert first_audio_jobs == [((first_root).as_posix(), [(first_root / "voice.wav").as_posix()])]
    assert second_audio_jobs == [
        ((second_root).as_posix(), [(second_root / "voice.wav").as_posix()])
    ]
    assert (first_root / "voice.wav").read_bytes() == b"first acb"
    assert (second_root / "voice.wav").read_bytes() == b"second acb"
    assert all(Path(path).is_relative_to(first_root) for path in first_exported)
    assert all(Path(path).is_relative_to(second_root) for path in second_exported)
    assert not (first_root / "audio" / "voice.acb").exists()
    assert not (second_root / "audio" / "voice.acb").exists()

    async def fake_encode(input_path, output_path, _config):
        await output_path.write_bytes(await input_path.read_bytes() + output_path.suffix.encode())
        return True

    monkeypatch.setattr(bundle, "_run_ffmpeg_audio_encode", fake_encode)
    config = SimpleNamespace()
    first_audio = asyncio.run(
        bundle._process_extracted_audio_file(
            (first_root / "voice.wav").as_posix(),
            bundle.Path(first_root.as_posix()),
            config,
            asyncio.Semaphore(1),
        )
    )
    second_audio = asyncio.run(
        bundle._process_extracted_audio_file(
            (second_root / "voice.wav").as_posix(),
            bundle.Path(second_root.as_posix()),
            config,
            asyncio.Semaphore(1),
        )
    )
    assert {path.as_posix() for path in first_audio} == {
        (first_root / "voice.wav").as_posix(),
        (first_root / "voice.mp3").as_posix(),
    }
    assert {path.as_posix() for path in second_audio} == {
        (second_root / "voice.wav").as_posix(),
        (second_root / "voice.mp3").as_posix(),
    }
    assert (first_root / "voice.mp3").read_bytes() == b"first acb.mp3"
    assert (second_root / "voice.mp3").read_bytes() == b"second acb.mp3"


def test_movie_video_outputs_and_cleanup_are_isolated_between_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_bundle = tmp_path / "first.bundle"
    second_bundle = tmp_path / "second.bundle"
    first_bundle.write_bytes(b"first")
    second_bundle.write_bytes(b"second")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda _path, _version: _movie_unity_file(),
    )

    first_exported, _, first_video_jobs = _extract_sync(first_bundle, first_root)
    second_exported, _, second_video_jobs = _extract_sync(second_bundle, second_root)

    async def fake_demux(usm_path, _config):
        output = usm_path.parent / "movie.m2v"
        await output.write_bytes(await usm_path.read_bytes())
        return output

    class _FakeProcess:
        def __init__(self, output_path, content: bytes) -> None:
            self.output_path = output_path
            self.content = content
            self.returncode = 0

        async def wait(self):
            await self.output_path.write_bytes(self.content)

    async def fake_video_encode(_input_path, output_path, _config):
        return _FakeProcess(output_path, output_path.parent.name.encode()), None

    monkeypatch.setattr(bundle, "_demux_usm_to_m2v", fake_demux)
    monkeypatch.setattr(bundle, "_run_ffmpeg_video_to_mp4", fake_video_encode)
    results = asyncio.run(
        bundle._process_video_jobs(first_video_jobs + second_video_jobs, SimpleNamespace())
    )

    first_video, first_discarded = results[0]
    second_video, second_discarded = results[1]
    assert [path.as_posix() for path in first_video] == [(first_root / "movie.mp4").as_posix()]
    assert [path.as_posix() for path in second_video] == [(second_root / "movie.mp4").as_posix()]
    assert {path.name for path in first_discarded} == {"movie.usm", "movie.m2v"}
    assert {path.name for path in second_discarded} == {"movie.usm", "movie.m2v"}
    assert not (first_root / "movie.usm").exists()
    assert not (second_root / "movie.usm").exists()
    assert (first_root / "movie.mp4").read_bytes() == b"first"
    assert (second_root / "movie.mp4").read_bytes() == b"second"
    assert all(Path(path).is_relative_to(first_root) for path in first_exported)
    assert all(Path(path).is_relative_to(second_root) for path in second_exported)


def test_video_promotion_failure_is_contained_to_one_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_usm = first_root / "movie.usm"
    second_usm = second_root / "movie.usm"
    first_usm.write_bytes(b"first")
    second_usm.write_bytes(b"second")

    async def fake_demux(usm_path, _config):
        m2v_path = usm_path.parent / "movie.m2v"
        await m2v_path.write_bytes(await usm_path.read_bytes())
        return m2v_path

    class _SuccessfulProcess:
        returncode = 0

        def __init__(self, staged_output: Path, staging_dir: Path, content: bytes) -> None:
            self._bundle_output_path = staged_output
            self._bundle_staging_dir = staging_dir
            self._content = content

        async def wait(self):
            self._bundle_output_path.write_bytes(self._content)
            return 0

    async def fake_video_encode(_input_path, output_path, _config):
        output_path = Path(output_path.as_posix())
        staging_dir = output_path.parent / f".video-{output_path.parent.name}"
        staging_dir.mkdir()
        staged_output = staging_dir / output_path.name
        process = _SuccessfulProcess(staged_output, staging_dir, output_path.parent.name.encode())
        return process, None

    original_validate_output_target = bundle.validate_output_target

    def reject_first_promotion(root, output):
        if output.as_posix() == (first_root / "movie.mp4").as_posix():
            raise bundle.SecurityError("synthetic promotion failure")
        return original_validate_output_target(root, output)

    monkeypatch.setattr(bundle, "_demux_usm_to_m2v", fake_demux)
    monkeypatch.setattr(bundle, "_run_ffmpeg_video_to_mp4", fake_video_encode)
    monkeypatch.setattr(bundle, "validate_output_target", reject_first_promotion)

    results = asyncio.run(
        bundle._process_video_jobs([first_usm.as_posix(), second_usm.as_posix()], SimpleNamespace())
    )

    first_video, first_discarded = results[0]
    second_video, second_discarded = results[1]
    assert first_video == []
    assert second_video == [bundle.Path((second_root / "movie.mp4").as_posix())]
    assert {path.name for path in first_discarded} == {"movie.usm", "movie.m2v"}
    assert {path.name for path in second_discarded} == {"movie.usm", "movie.m2v"}
    assert not (first_root / "movie.mp4").exists()
    assert (second_root / "movie.mp4").read_bytes() == b"second"
    assert not (first_root / ".video-first").exists()
    assert not (second_root / ".video-second").exists()


def test_cached_acb_lookup_uses_explicit_non_bundle_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache-store"
    cache_bundle = cache_root / "source.bundle"
    cache_bundle.parent.mkdir()
    cache_bundle.write_bytes(b"cache")
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    bundle_path = artifact_root / "current.bundle"
    bundle_path.write_bytes(b"current")
    output = artifact_root / "voice.acb"

    class _CachedUnityObject:
        type = SimpleNamespace(name="TextAsset")

        def read(self):
            return _FakeTextAsset("cached acb")

    monkeypatch.setattr(
        bundle_acb_cache,
        "load_bundle",
        lambda _path, _version: SimpleNamespace(
            container={"audio/voice.acb.bytes": _CachedUnityObject()}
        ),
    )
    monkeypatch.setattr(bundle_acb_cache, "read_text_bytes", lambda _obj: b"cached acb")

    assert bundle._extract_acb_from_cached_bundles_sync(
        bundle_path,
        "voice.acb.bytes",
        output,
        None,
        cache_root,
    )
    assert output.read_bytes() == b"cached acb"
    assert cache_bundle.read_bytes() == b"cache"


def test_cached_acb_lookup_skips_directories_before_file_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache_root = tmp_path / "cache-store"
    cache_root.mkdir()
    (cache_root / "nested").mkdir()
    cache_bundle = cache_root / "source.bundle"
    cache_bundle.write_bytes(b"cache")
    bundle_path = tmp_path / "current.bundle"
    bundle_path.write_bytes(b"current")
    output = tmp_path / "artifact" / "voice.acb"
    output.parent.mkdir()

    class _CachedUnityObject:
        type = SimpleNamespace(name="TextAsset")

        def read(self):
            return _FakeTextAsset("cached acb")

    monkeypatch.setattr(
        bundle_acb_cache,
        "load_bundle",
        lambda _path, _version: SimpleNamespace(
            container={"audio/voice.acb.bytes": _CachedUnityObject()}
        ),
    )
    monkeypatch.setattr(bundle_acb_cache, "read_text_bytes", lambda _obj: b"cached acb")

    with caplog.at_level("WARNING", logger="live2d"):
        assert bundle._extract_acb_from_cached_bundles_sync(
            bundle_path, "voice.acb.bytes", output, None, cache_root
        )

    assert output.read_bytes() == b"cached acb"
    assert "Ignoring unsafe cached bundle path" not in caplog.text


def test_extraction_uses_configured_cache_root_with_custom_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "asset-cache"
    cache_bundle = cache_root / "source.bundle"
    cache_bundle.parent.mkdir()
    cache_bundle.write_bytes(b"cache")
    bundle_path = tmp_path / "current.bundle"
    bundle_path.write_bytes(b"current")
    output_root = tmp_path / "artifact"
    output_root.mkdir()

    class _CachedUnityObject:
        type = SimpleNamespace(name="TextAsset")

        def read(self):
            return _FakeTextAsset("cached acb")

    loaded_files = {
        bundle_path.as_posix(): _acb_reference_unity_file(),
        cache_bundle.as_posix(): SimpleNamespace(
            container={"audio/voice.acb.bytes": _CachedUnityObject()}
        ),
    }
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda path, _version: loaded_files.get(path.as_posix()),
    )
    monkeypatch.setattr(
        bundle_acb_cache,
        "load_bundle",
        lambda path, _version: loaded_files.get(Path(path).as_posix()),
    )
    monkeypatch.setattr(bundle_acb_cache, "read_text_bytes", lambda _obj: b"cached acb")

    def fake_extract_acb(_data, target_dir: str, acb_path: str, _cue_name):
        output = Path(target_dir) / "voice.wav"
        output.write_bytes(Path(acb_path).read_bytes())
        return [output.as_posix()]

    monkeypatch.setattr(bundle, "extract_acb", fake_extract_acb)

    _, audio_jobs, _ = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "current"},
        output_root.as_posix(),
        None,
        ("png",),
        cache_root.as_posix(),
    )

    assert audio_jobs == [(output_root.as_posix(), [output_root.joinpath("voice.wav").as_posix()])]
    assert output_root.joinpath("voice.wav").read_bytes() == b"cached acb"


def test_acb_decoder_rejects_unsafe_output_and_cleans_private_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "current.bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "artifact"
    outside = tmp_path / "outside.wav"

    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda _path, _version: _acb_unity_file("acb"),
    )

    def unsafe_extract(_data, _target_dir, _acb_path, _cue_name):
        outside.write_bytes(b"unsafe")
        return [outside.as_posix()]

    monkeypatch.setattr(bundle, "extract_acb", unsafe_extract)
    with pytest.raises((ValueError, bundle.SecurityError)):
        _extract_sync(bundle_path, output_root)
    assert not list(output_root.rglob(".acb-*"))
