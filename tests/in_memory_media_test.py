"""Tests for the fully in-memory ACB / USM media paths."""

from __future__ import annotations

import asyncio
import io
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import cridecoder
import pytest

from updater.bundle import pipeline as bundle


class _FakeUnityObject:
    def __init__(self, type_name: str, tree=None, text: bytes = b"") -> None:
        self.type = SimpleNamespace(name=type_name)
        self.class_id = {"MonoBehaviour": 114, "TextAsset": 49}[type_name]
        self.file_index = 0
        self.path_id = id(self)
        self.serialized_type = SimpleNamespace(node=tree is not None)
        self._tree = tree
        self._environment = SimpleNamespace(studio=SimpleNamespace(read_text=lambda *_args: text))

    def read_typetree(self):
        return self._tree


def _synthetic_acb_bytes(track_name: str = "voice") -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(
            b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(4800))
        )
    hca = cridecoder.encode_hca_bytes(buf.getvalue())
    return cridecoder.build_acb_bytes([(track_name, 0, hca)])


def test_acb_decodes_fully_in_memory_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "extracted"
    output_root.mkdir()
    acb_bytes = _synthetic_acb_bytes("voice")

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
            "assets/sekai/assetbundle/resources/ondemand/audio/voice.asset": _FakeUnityObject(
                "MonoBehaviour", acb_tree
            ),
            "assets/sekai/assetbundle/resources/ondemand/audio/voice.acb.bytes": _FakeUnityObject(
                "TextAsset", text=acb_bytes
            ),
        }
    )
    monkeypatch.setattr(bundle, "_load_unity_bundle", lambda _path, _version: fake_unity_file)
    monkeypatch.setattr(
        bundle,
        "extract_acb",
        lambda *_args: pytest.fail("path-based ACB decoder must not run"),
    )

    exported, audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        "2022.3.52f1",
        ("png",),
    )

    wav_path = output_root / "audio" / "voice.wav"
    assert wav_path.read_bytes()[:4] == b"RIFF"
    assert audio_jobs == [((output_root / "audio").as_posix(), [wav_path.as_posix()])]
    assert video_jobs == []
    assert not (output_root / "audio" / "voice.acb").exists()
    assert not list(output_root.rglob(".acb-*"))


def _movie_unity_file(usm_filenames: list[str], payloads: dict[str, bytes]) -> SimpleNamespace:
    movie_tree = {"movieBundleDatas": [{"usmFileName": name} for name in usm_filenames]}
    container = {
        "assets/sekai/assetbundle/resources/ondemand/movie/data.asset": _FakeUnityObject(
            "MonoBehaviour", movie_tree
        ),
    }
    for name in usm_filenames:
        container[f"assets/sekai/assetbundle/resources/ondemand/movie/{name}"] = _FakeUnityObject(
            "TextAsset", text=payloads[name]
        )
    return SimpleNamespace(container=container)


def test_usm_single_part_demuxes_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "extracted"
    output_root.mkdir()

    received: list[bytes] = []

    def fake_extract_usm_bytes(usm_data: bytes, _key, _export_audio):
        received.append(usm_data)
        return [{"name": "stream", "extension": "m2v", "data": b"video-stream"}]

    monkeypatch.setattr(bundle.cridecoder, "extract_usm_bytes", fake_extract_usm_bytes)
    monkeypatch.setattr(
        bundle.cridecoder,
        "extract_usm",
        lambda *_args: pytest.fail("path-based USM demux must not run"),
    )
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda _path, _version: _movie_unity_file(
            ["movie.usm.bytes"], {"movie.usm.bytes": b"CRID-payload"}
        ),
    )

    exported, audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        "2022.3.52f1",
        ("png",),
    )

    m2v_path = output_root / "movie" / "movie.m2v"
    assert received == [b"CRID-payload"]
    assert video_jobs == [m2v_path.as_posix()]
    assert m2v_path.read_bytes() == b"video-stream"
    assert not (output_root / "movie" / "movie.usm").exists()
    assert m2v_path.as_posix() not in exported


def test_usm_split_parts_merge_in_memory_without_disk_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "extracted"
    output_root.mkdir()

    received: list[bytes] = []

    def fake_extract_usm_bytes(usm_data: bytes, _key, _export_audio):
        received.append(usm_data)
        return [{"name": "stream", "extension": "m2v", "data": b"merged-video"}]

    monkeypatch.setattr(bundle.cridecoder, "extract_usm_bytes", fake_extract_usm_bytes)
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda _path, _version: _movie_unity_file(
            ["movie-001.usm.bytes", "movie-002.usm.bytes"],
            {"movie-001.usm.bytes": b"part-one|", "movie-002.usm.bytes": b"part-two"},
        ),
    )

    exported, _audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        "2022.3.52f1",
        ("png",),
    )

    m2v_path = output_root / "movie" / "movie.m2v"
    assert received == [b"part-one|part-two"]
    assert video_jobs == [m2v_path.as_posix()]
    assert m2v_path.read_bytes() == b"merged-video"
    # Neither the split parts nor a merged .usm intermediate remain on disk.
    assert not list((output_root / "movie").glob("*.usm"))


def test_usm_over_limit_falls_back_to_disk_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.write_bytes(b"bundle")
    output_root = tmp_path / "extracted"
    output_root.mkdir()

    monkeypatch.setattr(
        bundle.cridecoder,
        "extract_usm_bytes",
        lambda *_args: pytest.fail("in-memory demux must not run above the size limit"),
    )
    monkeypatch.setattr(
        bundle,
        "_load_unity_bundle",
        lambda _path, _version: _movie_unity_file(
            ["movie-001.usm.bytes", "movie-002.usm.bytes"],
            {"movie-001.usm.bytes": b"part-one|", "movie-002.usm.bytes": b"part-two"},
        ),
    )

    _exported, _audio_jobs, video_jobs = bundle._extract_bundle_files_sync(
        bundle_path.as_posix(),
        {"bundleName": "test"},
        output_root.as_posix(),
        "2022.3.52f1",
        ("png",),
        usm_in_memory_limit=0,
    )

    usm_path = output_root / "movie" / "movie.usm"
    assert video_jobs == [usm_path.as_posix()]
    assert usm_path.read_bytes() == b"part-one|part-two"


def test_process_video_job_transcodes_m2v_without_demux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m2v_path = tmp_path / "movie.m2v"
    m2v_path.write_bytes(b"video-stream")

    monkeypatch.setattr(
        bundle,
        "_demux_usm_to_m2v",
        lambda *_args, **_kwargs: pytest.fail("m2v jobs must not be demuxed again"),
    )

    async def fake_run_ffmpeg(input_path, output_path, _config):
        staged = tmp_path / ".staged.mp4"
        staged.write_bytes(b"mp4:" + Path(input_path.as_posix()).read_bytes())
        process = SimpleNamespace(
            returncode=0,
            _bundle_output_path=staged,
            _bundle_staging_dir=None,
        )
        return process, None

    async def fake_wait(process, _timeout):
        return process.returncode

    monkeypatch.setattr(bundle, "_run_ffmpeg_video_to_mp4", fake_run_ffmpeg)
    monkeypatch.setattr(bundle, "_wait_for_process", fake_wait)

    exported, discarded = asyncio.run(
        bundle._process_video_job(m2v_path.as_posix(), SimpleNamespace(), asyncio.Semaphore(1))
    )

    mp4_path = tmp_path / "movie.mp4"
    assert exported == [bundle.Path(mp4_path.as_posix())]
    assert mp4_path.read_bytes() == b"mp4:video-stream"
    assert not m2v_path.exists()
