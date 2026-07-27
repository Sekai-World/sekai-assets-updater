from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import helpers
from helpers import _derive_storage_remote_path, get_download_max_retries, upload_to_storage
from security import SecurityError


def test_upload_validates_sources_and_derives_posix_remote_keys(
    tmp_path: Path, fake_subprocess
) -> None:
    exported_path = tmp_path / "nested" / "song.mp3"
    exported_path.parent.mkdir()
    exported_path.write_bytes(b"audio")
    upload_args = ["copyto", "src", "dst"]
    fake_process = fake_subprocess()

    asyncio.run(
        upload_to_storage(
            [exported_path],
            tmp_path,
            "remote:bucket/prefix/",
            "rclone",
            upload_args,
        )
    )

    assert fake_process.calls == [
        (
            (
                "rclone",
                "copyto",
                str(exported_path),
                "remote:bucket/prefix/nested/song.mp3",
            ),
            {},
        )
    ]
    assert upload_args == ["copyto", "src", "dst"]


@pytest.mark.parametrize(
    ("remote_base", "expected"),
    [
        ("remote:bucket/prefix", "remote:bucket/prefix/nested/song.mp3"),
        ("sftp:/absolute/path", "sftp:/absolute/path/nested/song.mp3"),
        ("local:/absolute/path", "local:/absolute/path/nested/song.mp3"),
        (
            ":s3,provider=example:bucket/prefix",
            ":s3,provider=example:bucket/prefix/nested/song.mp3",
        ),
    ],
)
def test_storage_remote_path_preserves_generic_rclone_target(
    remote_base: str, expected: str
) -> None:
    assert _derive_storage_remote_path(remote_base, "nested/song.mp3") == expected


def test_storage_remote_path_preserves_trailing_separator() -> None:
    assert _derive_storage_remote_path("remote:bucket/prefix/", "song.mp3") == (
        "remote:bucket/prefix/song.mp3"
    )


def test_upload_rejects_outside_source_before_subprocess(tmp_path: Path, fake_subprocess) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.mp3"
    outside_path.write_bytes(b"outside")
    fake_process = fake_subprocess()

    with pytest.raises(SecurityError):
        asyncio.run(
            upload_to_storage(
                [outside_path],
                tmp_path,
                "remote:bucket/prefix",
                "rclone",
                ["copyto", "src", "dst"],
            )
        )

    assert fake_process.calls == []


def test_upload_rejects_symlink_source_before_subprocess(tmp_path: Path, fake_subprocess) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-target.mp3"
    outside_path.write_bytes(b"outside")
    symlink_path = tmp_path / "song.mp3"
    symlink_path.symlink_to(outside_path)
    fake_process = fake_subprocess()

    with pytest.raises(SecurityError):
        asyncio.run(
            upload_to_storage(
                [symlink_path],
                tmp_path,
                "remote:bucket/prefix",
                "rclone",
                ["copyto", "src", "dst"],
            )
        )

    assert fake_process.calls == []


def test_upload_rejects_filename_with_non_posix_separator(tmp_path: Path, fake_subprocess) -> None:
    exported_path = tmp_path / "bad\\name.mp3"
    exported_path.write_bytes(b"audio")
    fake_process = fake_subprocess()

    with pytest.raises(SecurityError):
        asyncio.run(
            upload_to_storage(
                [exported_path],
                tmp_path,
                "remote:bucket/prefix",
                "rclone",
                ["copyto", "src", "dst"],
            )
        )

    assert fake_process.calls == []


@pytest.mark.parametrize(
    "relative_key",
    ["../outside.mp3", "nested/../outside.mp3", "/absolute.mp3", "nested\\song.mp3"],
)
def test_storage_remote_path_rejects_malicious_local_relative_key(
    relative_key: str,
) -> None:
    with pytest.raises(SecurityError):
        _derive_storage_remote_path("sftp:/absolute/path", relative_key)


def test_upload_aggregates_failures_from_all_jobs(tmp_path: Path, fake_subprocess) -> None:
    first_path = tmp_path / "first.mp3"
    second_path = tmp_path / "second.mp3"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    fake_process = fake_subprocess(returncode=1)

    with pytest.raises(RuntimeError, match=r"2 upload\(s\) failed"):
        asyncio.run(
            upload_to_storage(
                [first_path, second_path],
                tmp_path,
                "remote:bucket/prefix",
                "rclone",
                ["copyto", "src", "dst"],
            )
        )

    assert len(fake_process.calls) == 2


def test_upload_reraises_cancelled_error_from_gather(
    tmp_path: Path, fake_subprocess, monkeypatch
) -> None:
    exported_path = tmp_path / "asset.bin"
    exported_path.write_bytes(b"asset")
    fake_subprocess()

    async def cancel_wait(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(helpers, "_wait_for_process", cancel_wait)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            upload_to_storage(
                [exported_path],
                tmp_path,
                "remote:bucket/prefix",
                "rclone",
                ["copyto", "src", "dst"],
            )
        )


@pytest.mark.parametrize("configured_retries", [0, -1])
def test_download_retry_helper_clamps_to_one_attempt(configured_retries: int) -> None:
    config = type("Config", (), {"DOWNLOAD_MAX_RETRIES": configured_retries})()

    assert get_download_max_retries(config) == 1
