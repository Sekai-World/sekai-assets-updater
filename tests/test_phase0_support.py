"""Focused tests proving the Phase 0 test support exercises real boundaries."""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

from aiohttp import web
from anyio import Path as AnyioPath

from updater.bundle import pipeline as bundle
from updater.helpers import upload_to_storage
from updater.net import download as net_download


def test_download_fixture_serves_and_deobfuscates_a_bundle(
    temp_dir: Path,
    local_aiohttp_server,
) -> None:
    fields = b"2024.1\0rev\0"
    plain_bundle = (
        b"UnityFS\0"
        + struct.pack(">I", 6)
        + fields
        + struct.pack(">QIII", 8 + 4 + len(fields) + 20 + 1, 1, 1, 0)
        + b"I"
    )
    payload = b"\x20\x00\x00\x00" + plain_bundle

    def bundle_handler(_request: web.Request) -> web.Response:
        return web.Response(body=payload)

    server = local_aiohttp_server({"/bundle": bundle_handler})
    output_path = AnyioPath(temp_dir / "bundle")

    asyncio.run(
        net_download.download_deobfuscate_bundle(
            f"{server.url}/bundle",
            temp_dir,
            "bundle",
            headers={},
            config=type("Config", (), {"DOWNLOAD_MAX_RETRIES": 1})(),
        )
    )

    assert Path(output_path).read_bytes() == payload[4:]
    assert server.requests == [("GET", "/bundle")]


def test_fake_subprocess_records_upload_command(
    temp_dir: Path,
    fake_subprocess,
) -> None:
    exported_path = temp_dir / "music" / "song.mp3"
    exported_path.parent.mkdir()
    exported_path.write_bytes(b"audio")
    fake_process = fake_subprocess()

    asyncio.run(
        upload_to_storage(
            [exported_path],
            temp_dir,
            "remote/assets",
            "rclone",
            ["copyto", "src", "dst"],
        )
    )

    assert fake_process.calls == [
        (
            ("rclone", "copyto", str(exported_path), "remote/assets/music/song.mp3"),
            {},
        )
    ]


def test_synthetic_unityfs_paths_map_to_the_extraction_root(
    temp_dir: Path,
    synthetic_unityfs_paths: dict[str, str],
    unityfs_path,
) -> None:
    expected = {
        "container": temp_dir / "unit.prefab",
        "builtin": temp_dir / "shared.mat",
        "builtin_alt": temp_dir / "common.asset",
    }

    for name, path in synthetic_unityfs_paths.items():
        assert bundle._build_unityfs_save_path(path, temp_dir) == expected[name]

    assert (
        unityfs_path("characters", "unit.prefab")
        .as_posix()
        .endswith("resources/characters/unit.prefab")
    )


def test_local_aiohttp_fixture_can_host_multiple_endpoints(local_aiohttp_server) -> None:
    server = local_aiohttp_server(
        {
            "/one": lambda _request: web.Response(text="one"),
            "/two": lambda _request: web.Response(text="two"),
        }
    )

    async def fetch_both() -> list[str]:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            values = []
            for path in ("/one", "/two"):
                async with session.get(server.url + path) as response:
                    values.append(await response.text())
            return values

    assert asyncio.run(fetch_both()) == ["one", "two"]
    assert server.requests == [("GET", "/one"), ("GET", "/two")]
