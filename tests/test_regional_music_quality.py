import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from anyio import Path as AnyioPath

from updater.postprocess import dispatch


def test_scoped_chart_upload_stages_only_selected_regular_outputs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    chart_root = workspace / "charts" / "en"
    selected_dir = chart_root / "001"
    shared_dir = chart_root / "002"
    selected_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)
    (selected_dir / "master.svg").write_text("selected svg")
    (shared_dir / "master.svg").write_text("shared svg")
    (selected_dir / "master.png").symlink_to(selected_dir / "master.svg")

    staged_files = []

    async def capture_staging(_mode, source_dir, _config):
        staged_files.extend(
            sorted(path.relative_to(source_dir).as_posix() for path in source_dir.rglob("*"))
        )

    monkeypatch.setattr(dispatch, "_upload_specialized_directory", capture_staging)
    config = SimpleNamespace(
        REGION=SimpleNamespace(name="EN"),
        ASSET_REMOTE_STORAGE=[{"type": "charts"}],
    )

    asyncio.run(
        dispatch._upload_scoped_chart_outputs(config, workspace, {"001_song/master.txt": "hash"})
    )

    assert staged_files == ["001", "001/master.svg"]


def test_specialized_upload_converts_stdlib_path_at_io_boundary(tmp_path, monkeypatch):
    source_dir = tmp_path / "charts"
    source_dir.mkdir()
    upload = AsyncMock()
    monkeypatch.setattr(dispatch, "upload_directory", upload)
    config = SimpleNamespace(
        REGION=SimpleNamespace(name="EN"),
        ASSET_REMOTE_STORAGE=[
            {
                "type": "charts",
                "base": "chart-target",
                "program": "rclone",
                "args": ["copy", "src", "dst"],
            }
        ],
    )

    asyncio.run(dispatch._upload_specialized_directory("charts", source_dir, config))

    upload.assert_awaited_once()
    assert upload.await_args.args[0] == AnyioPath(source_dir)
    assert isinstance(upload.await_args.args[0], AnyioPath)
