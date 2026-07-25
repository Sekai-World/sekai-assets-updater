from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bundle


class _FakeTextAsset:
    def __init__(self, script: str) -> None:
        self.m_Script = script


class _FakeUnityObject:
    def __init__(self, type_name: str, tree=None, script: str = "") -> None:
        self.type = SimpleNamespace(name=type_name)
        self.serialized_type = SimpleNamespace(node=tree is not None)
        self._tree = tree
        self._script = script

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
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", _FakeTextAsset)
    monkeypatch.setattr(bundle.UnityPy, "load", lambda path: acb_by_bundle[path])

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
    monkeypatch.setattr(bundle.UnityPy, "load", lambda _path: _movie_unity_file())
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", _FakeTextAsset)

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
        bundle.UnityPy,
        "load",
        lambda path: (
            SimpleNamespace(container={"audio/voice.acb.bytes": _CachedUnityObject()})
            if path == cache_bundle.as_posix()
            else None
        ),
    )
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", _FakeTextAsset)

    assert bundle._extract_acb_from_cached_bundles_sync(
        bundle_path,
        "voice.acb.bytes",
        output,
        None,
        cache_root,
    )
    assert output.read_bytes() == b"cached acb"
    assert cache_bundle.read_bytes() == b"cache"


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
    monkeypatch.setattr(bundle.UnityPy, "load", lambda path: loaded_files.get(path))
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", _FakeTextAsset)

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

    monkeypatch.setattr(bundle.UnityPy, "load", lambda _path: _acb_unity_file("acb"))
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", _FakeTextAsset)

    def unsafe_extract(_data, _target_dir, _acb_path, _cue_name):
        outside.write_bytes(b"unsafe")
        return [outside.as_posix()]

    monkeypatch.setattr(bundle, "extract_acb", unsafe_extract)
    with pytest.raises((ValueError, bundle.SecurityError)):
        _extract_sync(bundle_path, output_root)
    assert not list(output_root.rglob(".acb-*"))
