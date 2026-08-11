from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import bundle
import security


def test_hca_decoder_uses_private_staging_and_promotes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "voice.hca"
    output_path = tmp_path / "voice.wav"
    input_path.write_bytes(b"hca")
    decoder_calls: list[tuple[str, str]] = []
    executor = ThreadPoolExecutor(max_workers=1)

    def fake_decode_hca(input_name: str, staged_output: str) -> None:
        decoder_calls.append((input_name, staged_output))
        Path(staged_output).write_bytes(b"decoded")
        Path(staged_output).with_name("decoder.log").write_text("diagnostic")

    monkeypatch.setattr(bundle, "_get_shared_audio_process_pool", lambda _config: executor)
    monkeypatch.setattr(bundle, "decode_hca_file", fake_decode_hca)
    try:
        decoded = asyncio.run(
            bundle._run_hca_to_wav_with_cridecoder(input_path, output_path, SimpleNamespace())
        )
    finally:
        executor.shutdown(wait=True)

    assert decoded is True
    assert decoder_calls[0][0] == input_path.as_posix()
    staged_output = Path(decoder_calls[0][1])
    assert staged_output.parent != output_path.parent
    assert staged_output.parent.parent == output_path.parent
    assert output_path.read_bytes() == b"decoded"
    assert not staged_output.parent.exists()


def test_acb_extractor_uses_private_staging_and_promotes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "extracted"
    output_root.mkdir()
    extract_calls: list[tuple[str, str]] = []

    class FakeTextAsset:
        m_Script = "acb data"

    class FakeUnityObject:
        def __init__(self, type_name: str, tree=None) -> None:
            self.type = SimpleNamespace(name=type_name)
            self.serialized_type = SimpleNamespace(node=tree is not None)
            self._tree = tree

        def read(self):
            return FakeTextAsset()

        def read_typetree(self):
            return self._tree

    acb_tree = {
        "acbFiles": [
            {
                "cueSheetName": "voice",
                "formatType": 0,
                "spilitFileNum": 0,
                "assetBundleFileName": "voice.acb.bytes",
            }
        ]
    }
    fake_unity_file = SimpleNamespace(
        container={
            "assets/sekai/assetbundle/resources/audio/voice.asset": FakeUnityObject(
                "MonoBehaviour", acb_tree
            ),
            "assets/sekai/assetbundle/resources/audio/voice.acb.bytes": FakeUnityObject(
                "TextAsset"
            ),
        }
    )

    def fake_extract_acb(_acb_file, target_dir: str, acb_file_path: str, _cue_name):
        extract_calls.append((target_dir, acb_file_path))
        output_path = Path(target_dir) / "voice.wav"
        output_path.write_bytes(b"wav")
        return [output_path.as_posix()]

    monkeypatch.setattr(bundle.UnityPy, "load", lambda _path: fake_unity_file)
    monkeypatch.setattr(bundle.UnityPy.classes, "TextAsset", FakeTextAsset)
    monkeypatch.setattr(bundle, "extract_acb", fake_extract_acb)

    exported, audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        None,
        ("png",),
    )

    assert len(extract_calls) == 1
    staged_dir = Path(extract_calls[0][0])
    assert staged_dir != output_root
    assert staged_dir.parent == output_root
    assert Path(extract_calls[0][1]).parent == staged_dir
    promoted_output = output_root / "voice.wav"
    assert promoted_output.read_bytes() == b"wav"
    assert audio_jobs == [(output_root.as_posix(), [promoted_output.as_posix()])]
    assert video_jobs == []
    assert not staged_dir.exists()
    assert not (output_root / "voice.acb").exists()


def test_usm_extractor_uses_private_staging_and_promotes_selected_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usm_path = tmp_path / "movie.usm"
    usm_path.write_bytes(b"usm")
    extractor_calls: list[tuple[str, str]] = []

    def fake_extract_usm(input_name: str, staging_dir: str, _unused, _extract_audio: bool):
        extractor_calls.append((input_name, staging_dir))
        discarded = Path(staging_dir) / "audio.bin"
        selected = Path(staging_dir) / "movie.m2v"
        discarded.write_bytes(b"audio")
        selected.write_bytes(b"video")
        return [discarded.as_posix(), selected.as_posix()]

    monkeypatch.setattr(bundle.cridecoder, "extract_usm", fake_extract_usm)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(bundle, "_get_shared_usm_process_pool", lambda _config: executor)
    try:
        selected = asyncio.run(bundle._demux_usm_to_m2v(usm_path, SimpleNamespace()))
    finally:
        executor.shutdown(wait=True)

    assert len(extractor_calls) == 1
    staging_dir = Path(extractor_calls[0][1])
    assert extractor_calls[0][0] == usm_path.as_posix()
    assert staging_dir != usm_path.parent
    assert staging_dir.parent == usm_path.parent
    promoted_output = usm_path.parent / "movie.m2v"
    assert selected is not None
    assert selected.as_posix() == promoted_output.as_posix()
    assert promoted_output.read_bytes() == b"video"
    assert not (usm_path.parent / "audio.bin").exists()
    assert not staging_dir.exists()


@pytest.mark.parametrize("relative_destination", ["../outside.bundle", "/absolute.bundle"])
def test_download_rejects_unsafe_relative_destination(
    tmp_path: Path, relative_destination: str
) -> None:
    download = bundle.download_deobfuscate_bundle(
        "https://example.invalid/bundle", tmp_path, relative_destination, {}
    )
    with pytest.raises(security.SecurityError):
        asyncio.run(download)


def test_download_rejects_preexisting_symlink_destination(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.bundle"
    outside_path.write_bytes(b"outside")
    symlink_path = tmp_path / "bundle"
    symlink_path.symlink_to(outside_path)

    download = bundle.download_deobfuscate_bundle(
        "https://example.invalid/bundle", tmp_path, "bundle", {}
    )
    with pytest.raises(security.SecurityError):
        asyncio.run(download)
