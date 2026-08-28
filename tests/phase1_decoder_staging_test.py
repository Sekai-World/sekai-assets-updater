from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater import security
from updater.bundle import pipeline as bundle
from updater.net import download as net_download


def test_hca_decoder_decodes_in_memory_and_writes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "voice.hca"
    output_path = tmp_path / "voice.wav"
    input_path.write_bytes(b"hca")
    decoder_calls: list[bytes] = []

    def fake_decode_bytes(hca_data: bytes) -> bytes:
        decoder_calls.append(hca_data)
        return b"decoded"

    monkeypatch.setattr(bundle, "decode_hca_to_wav_bytes", fake_decode_bytes)
    decoded = asyncio.run(
        bundle._run_hca_to_wav_with_cridecoder(input_path, output_path, SimpleNamespace())
    )

    assert decoded is True
    assert decoder_calls == [b"hca"]
    assert output_path.read_bytes() == b"decoded"
    # No staging directories or temporary files remain next to the output.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["voice.hca", "voice.wav"]


def test_hca_decoder_reports_failure_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "voice.hca"
    output_path = tmp_path / "voice.wav"
    input_path.write_bytes(b"hca")

    def failing_decode_bytes(_hca_data: bytes) -> bytes:
        raise ValueError("corrupt hca")

    monkeypatch.setattr(bundle, "decode_hca_to_wav_bytes", failing_decode_bytes)
    decoded = asyncio.run(
        bundle._run_hca_to_wav_with_cridecoder(input_path, output_path, SimpleNamespace())
    )

    assert decoded is False
    assert not output_path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["voice.hca"]


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
            self.class_id = {"MonoBehaviour": 114, "TextAsset": 49}[type_name]
            self.file_index = 0
            self.path_id = id(self)
            self.serialized_type = SimpleNamespace(node=tree is not None)
            self._tree = tree
            self._environment = SimpleNamespace(
                studio=SimpleNamespace(read_text=lambda *_args: b"acb data")
            )

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

    monkeypatch.setattr(bundle, "_load_unity_bundle", lambda _path, _version: fake_unity_file)
    monkeypatch.setattr(bundle, "extract_acb", fake_extract_acb)

    exported, audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        "2022.3.52f1",
        ("png",),
    )

    assert len(extract_calls) == 1
    staged_dir = Path(extract_calls[0][0])
    assert staged_dir != output_root
    assert staged_dir.parent == output_root
    # The ACB is decoded in place (its parent is where sibling .awb archives
    # live); only decoded outputs are staged before promotion.
    assert Path(extract_calls[0][1]) == output_root / "voice.acb"
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
    download = net_download.download_deobfuscate_bundle(
        "https://example.invalid/bundle", tmp_path, relative_destination, {}
    )
    with pytest.raises(security.SecurityError):
        asyncio.run(download)


def test_download_rejects_preexisting_symlink_destination(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.bundle"
    outside_path.write_bytes(b"outside")
    symlink_path = tmp_path / "bundle"
    symlink_path.symlink_to(outside_path)

    download = net_download.download_deobfuscate_bundle(
        "https://example.invalid/bundle", tmp_path, "bundle", {}
    )
    with pytest.raises(security.SecurityError):
        asyncio.run(download)
