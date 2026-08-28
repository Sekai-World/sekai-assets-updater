import asyncio
import json
import unittest
from pathlib import Path
from pathlib import Path as StdPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from anyio import Path as AsyncPath

import main
from updater.cli import configuration, entry, lifecycle, pending, runner
from updater.modes import is_live2d_bundle
from updater.modes import filter_bundles_for_mode, get_mode_bundle_prefixes
from updater.net.plan import select_bundles_for_download
from updater.modes import (
    get_enabled_specialized_modes,
    get_required_bundle_prefixes,
    mode_uses_bundle_pipeline,
    needs_live2d_bundle_cache,
    needs_shared_workspace,
    retains_live2d_extracted_outputs,
)
from updater.postprocess import charts, dispatch, incremental_state, live2d_models
from updater.postprocess.charts import (
    collect_score_files,
    get_json_url,
    has_local_chart_sources,
    music_id_from_score_path,
    needs_temporary_chart_source,
)
from updater.postprocess.config import (
    get_chart_data_server,
    get_normal_storage_candidates,
    get_specialized_storage,
)
from updater.postprocess.dispatch import run_specialized_postprocess
from updater.postprocess.incremental_state import (
    chart_fingerprint,
    chart_state_path,
    compute_motion_bundle_hashes,
    compute_score_hashes,
    hash_score_file,
    live2d_state_path,
    load_chart_state,
    load_live2d_state,
    pending_motion_bundles,
    pending_score_paths,
    validate_chart_state,
    validate_live2d_state,
)
from updater.postprocess.live2d_models import recover_live2d_model_outputs
from updater.workspace import get_bundle_cache_path, get_bundle_cache_root


class SpecializedHelpersTest(unittest.TestCase):
    def config(self):
        return SimpleNamespace(
            ENABLE_LIVE2D_POSTPROCESS=False,
            ENABLE_CHARTS_POSTPROCESS=False,
            ASSET_REMOTE_STORAGE=[
                {
                    "type": "live2d",
                    "base": "live",
                    "program": "rclone",
                    "args": ["copy", "src", "dst"],
                },
                {
                    "type": "charts",
                    "base": "charts",
                    "program": "rclone",
                    "args": ["copy", "src", "dst"],
                },
            ],
            ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
            ASSET_LOCAL_EXTRACTED_DIR=None,
            LIVE2D_BUNDLE_CACHE_DIR=None,
        )

    def test_assets_mode_uses_independent_enable_flags(self):
        config = self.config()
        self.assertEqual(get_enabled_specialized_modes("assets", config), ())
        config.ENABLE_LIVE2D_POSTPROCESS = True
        self.assertEqual(get_enabled_specialized_modes("assets", config), ("live2d",))
        config.ENABLE_CHARTS_POSTPROCESS = True
        self.assertEqual(get_enabled_specialized_modes("assets", config), ("live2d", "charts"))

    def test_specialized_mode_forces_its_postprocessor(self):
        config = self.config()
        self.assertEqual(get_enabled_specialized_modes("live2d", config), ("live2d",))
        self.assertEqual(get_enabled_specialized_modes("charts", config), ("charts",))

    def test_charts_bypass_asset_bundle_pipeline(self):
        self.assertTrue(mode_uses_bundle_pipeline("assets"))
        self.assertTrue(mode_uses_bundle_pipeline("live2d"))
        self.assertFalse(mode_uses_bundle_pipeline("charts"))

    def test_required_prefixes_include_enabled_assets_processors(self):
        config = self.config()
        config.ENABLE_LIVE2D_POSTPROCESS = True
        config.ENABLE_CHARTS_POSTPROCESS = True
        self.assertEqual(
            get_required_bundle_prefixes("assets", config),
            ("live2d/",),
        )
        self.assertEqual(get_required_bundle_prefixes("live2d", config), ("live2d/",))

    def test_automatic_bundles_bypass_user_filters_and_dedupe(self):
        bundles = {
            "normal": {"bundleName": "music/a"},
            "live": {"bundleName": "live2d/model/a"},
            "live-duplicate": {"bundleName": "live2d/model/a"},
            "excluded-chart": {"bundleName": "music/music_score/a"},
        }
        selected = select_bundles_for_download(
            bundles,
            include_list=[r"^music/a$"],
            exclude_list=[r"^music/music_score/"],
            automatic_prefixes=("live2d/", "music/music_score/"),
        )
        self.assertEqual(list(selected), ["normal", "live", "excluded-chart"])

    def test_specialized_storage_is_independent_from_normal_storage(self):
        config = self.config()
        config.ASSET_REMOTE_STORAGE = [
            {"type": "normal", "base": "normal"},
            {"type": "live2d", "base": "live"},
            {"type": "charts", "base": "charts"},
        ]
        self.assertEqual(
            get_specialized_storage(config, "live2d"), [config.ASSET_REMOTE_STORAGE[1]]
        )
        self.assertEqual(
            get_specialized_storage(config, "charts"), [config.ASSET_REMOTE_STORAGE[2]]
        )

    def test_specialized_mode_prefixes_are_mandatory(self):
        bundles = {
            "live": {"bundleName": "live2d/model/a"},
            "score": {"bundleName": "music/music_score/a"},
            "other": {"bundleName": "music/a"},
        }
        self.assertEqual(get_mode_bundle_prefixes("live2d"), ("live2d/",))
        self.assertEqual(
            filter_bundles_for_mode(bundles, "live2d"),
            {"live": bundles["live"]},
        )
        self.assertEqual(list(filter_bundles_for_mode(bundles, "charts")), list(bundles))
        self.assertEqual(list(filter_bundles_for_mode(bundles, "assets")), list(bundles))

    def test_score_parser_and_collection(self):
        with self.subTest("score parser"):
            import tempfile

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                score = root / "music" / "music_score" / "001_song" / "master.txt"
                score.parent.mkdir(parents=True)
                score.write_text("# SUS", encoding="utf-8")
                self.assertEqual(music_id_from_score_path(score), 1)
                self.assertEqual(collect_score_files(root), [score])

    def test_chart_source_detection_honors_score_directory_include_patterns(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            score = root / "music" / "music_score" / "001_song" / "master.txt"
            score.parent.mkdir(parents=True)
            score.write_text("# SUS", encoding="utf-8")
            include_list = [r"^music/music_score/002_song$"]
            self.assertFalse(has_local_chart_sources(root, include_list))

    def test_live2d_extraction_is_decided_by_bundle_name(self):
        self.assertTrue(is_live2d_bundle({"bundleName": "live2d/motion/base"}))
        self.assertTrue(is_live2d_bundle({"bundleName": "live2d/model/base"}))
        self.assertFalse(is_live2d_bundle({"bundleName": "character/motion/base"}))
        self.assertFalse(is_live2d_bundle({"bundleName": "music/music_score/base"}))

    def test_specialized_prefixes_ignore_user_filters(self):
        config = self.config()
        config.ENABLE_LIVE2D_POSTPROCESS = True
        self.assertEqual(get_required_bundle_prefixes("assets", config), ("live2d/",))

    def test_bundle_cache_root_is_namespace_specific(self):
        config = self.config()
        config.ASSET_LOCAL_BUNDLE_CACHE_DIR = Path("normal")
        config.LIVE2D_BUNDLE_CACHE_DIR = Path("live2d-cache")
        self.assertEqual(
            get_bundle_cache_root(config, {"bundleName": "live2d/model/a"}),
            Path("live2d-cache"),
        )
        self.assertEqual(
            get_bundle_cache_root(config, {"bundleName": "music/music_score/a"}),
            Path("normal"),
        )
        self.assertEqual(
            get_bundle_cache_path(config, {"bundleName": "live2d/model/a"}),
            Path("live2d-cache/live2d/model/a"),
        )

    def test_specialized_workspace_and_cache_are_independent(self):
        config = self.config()
        self.assertTrue(needs_shared_workspace("charts", config))
        self.assertTrue(needs_live2d_bundle_cache("live2d", config))
        config.ASSET_LOCAL_EXTRACTED_DIR = Path("extracted")
        config.LIVE2D_BUNDLE_CACHE_DIR = Path("live2d-cache")
        self.assertFalse(needs_shared_workspace("charts", config))
        self.assertFalse(needs_live2d_bundle_cache("live2d", config))

    def test_live2d_outputs_are_retained_for_forced_or_enabled_postprocess(self):
        config = self.config()
        self.assertFalse(retains_live2d_extracted_outputs(config))
        config.ENABLE_LIVE2D_POSTPROCESS = True
        self.assertTrue(retains_live2d_extracted_outputs(config))
        config.ENABLE_LIVE2D_POSTPROCESS = False
        config.UPDATER_MODE = "live2d"
        self.assertTrue(retains_live2d_extracted_outputs(config))

    def test_charts_have_no_automatic_bundle_prefix(self):
        config = self.config()
        config.ENABLE_CHARTS_POSTPROCESS = True
        self.assertEqual(get_required_bundle_prefixes("assets", config), ())
        self.assertEqual(get_required_bundle_prefixes("charts", config), ())

    def test_chart_data_server_can_differ_from_asset_region(self):
        config = SimpleNamespace(
            REGION=SimpleNamespace(name="TW"), CHART_DATA_SERVER="tc"
        )
        self.assertEqual(get_chart_data_server(config), "tc")
        self.assertEqual(
            get_json_url(get_chart_data_server(config), "musics"),
            "https://sekai-world.github.io/sekai-master-db-tc-diff/musics.json",
        )
        self.assertEqual(get_chart_data_server(SimpleNamespace(REGION=config.REGION)), "tw")

    def test_chart_normal_storage_candidates_preserve_order(self):
        config = self.config()
        config.ASSET_REMOTE_STORAGE = [
            {"type": "special", "base": "skip"},
            {"type": "normal", "base": "first"},
            {"type": "normal", "base": "second"},
        ]
        self.assertEqual(
            get_normal_storage_candidates(config),
            config.ASSET_REMOTE_STORAGE[1:],
        )

    def test_chart_source_workspace_does_not_pollute_persistent_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertTrue(needs_temporary_chart_source(root, root))
            score = root / "music" / "music_score" / "001_song" / "master.txt"
            score.parent.mkdir(parents=True)
            score.write_text("# SUS", encoding="utf-8")
            self.assertTrue(has_local_chart_sources(root))
            self.assertFalse(needs_temporary_chart_source(root, root))
            self.assertFalse(needs_temporary_chart_source(root, None))

    def test_remote_model_paths_reject_unsafe_entries(self):
        with self.assertRaises(ValueError):
            live2d_models._models_from_remote_entries(
                [{"Path": "../old.model3.json", "IsDir": False}]
            )
        with self.assertRaises(ValueError):
            live2d_models._models_from_remote_entries(
                [{"Path": "/absolute.model3.json", "IsDir": False}]
            )

    def test_listing_args_preserve_flags_and_use_exact_target(self):
        storage = {
            "base": "sekai-ts:",
            "program": "rclone",
            "args": ["copyto", "src", "dst", "--s3-no-check-bucket", "--config", "opaque.conf"],
        }
        self.assertEqual(
            live2d_models._listing_args(storage, "sekai-ts:/live2d/model"),
            [
                "lsjson",
                "sekai-ts:/live2d/model",
                "--recursive",
                "--s3-no-check-bucket",
                "--config",
                "opaque.conf",
            ],
        )


class SpecializedPostprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_assets_charts_passes_download_include_list_to_postprocess(self):
        config = SimpleNamespace(
            ENABLE_LIVE2D_POSTPROCESS=False,
            ENABLE_CHARTS_POSTPROCESS=True,
            DL_INCLUDE_LIST=[r"^music/music_score/001_song$"],
        )
        with patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
            await lifecycle._run_enabled_specialized_postprocess("assets", config, False)
        postprocess.assert_awaited_once_with(
            "charts",
            config,
            extracted_dir_is_temporary=False,
            skip_missing_sources=True,
            score_include_list=config.DL_INCLUDE_LIST,
        )

    async def test_forced_charts_does_not_pass_download_include_list(self):
        config = SimpleNamespace(DL_INCLUDE_LIST=[r"^music/music_score/001_song$"])
        with patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
            await lifecycle._run_enabled_specialized_postprocess("charts", config, False)
        postprocess.assert_awaited_once_with(
            "charts",
            config,
            extracted_dir_is_temporary=False,
            skip_missing_sources=False,
            score_include_list=None,
        )

    async def test_chart_rendering_honors_score_directory_include_patterns(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            extracted_dir = Path(temp_dir)
            for directory_name in ("001_song", "002_song"):
                score = extracted_dir / "music" / "music_score" / directory_name / "master.txt"
                score.parent.mkdir(parents=True)
                score.write_text("# SUS", encoding="utf-8")
            config = SimpleNamespace(REGION=SimpleNamespace(name="JP"))

            with patch.object(charts, "get_list", new=AsyncMock(return_value=[{"id": 1}, {"id": 2}])
            ):
                with patch.object(charts, "render_chart", new=AsyncMock()) as render:
                    await charts._render_charts(
                        config, extracted_dir, [r"^music/music_score/001_song$"]
                    )

            render.assert_awaited_once()
            self.assertEqual(
                render.await_args.args[1],
                str(extracted_dir / "charts" / "jp" / "001" / "master.svg"),
            )

    async def test_chart_rendering_preserves_source_music_id_padding_in_output_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            extracted_dir = Path(temp_dir)
            for directory_name in ("0001_song", "12345_song"):
                score = extracted_dir / "music" / "music_score" / directory_name / "master.txt"
                score.parent.mkdir(parents=True)
                score.write_text("# SUS", encoding="utf-8")
            config = SimpleNamespace(REGION=SimpleNamespace(name="JP"))

            with patch.object(charts, "get_list",
                new=AsyncMock(return_value=[{"id": 1}, {"id": 12345}]),
            ):
                with patch.object(charts, "render_chart", new=AsyncMock()) as render:
                    await charts._render_charts(config, extracted_dir)

            self.assertEqual(render.await_count, 2)
            rendered_paths = {call.args[1] for call in render.await_args_list}
            self.assertEqual(
                rendered_paths,
                {
                    str(extracted_dir / "charts" / "jp" / "0001" / "master.svg"),
                    str(extracted_dir / "charts" / "jp" / "12345" / "master.svg"),
                },
            )

    async def test_forced_live2d_recovers_missing_model_output_from_raw_cache(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            extracted = root / "extracted"
            motion = cache / "live2d" / "motion"
            motion.mkdir(parents=True)
            cached_model = cache / "live2d" / "model" / "unit"
            cached_model.parent.mkdir(parents=True)
            cached_model.write_bytes(b"raw bundle")
            config = SimpleNamespace(
                LIVE2D_BUNDLE_CACHE_DIR=AsyncPath(str(cache)),
                ASSET_LOCAL_EXTRACTED_DIR=AsyncPath(str(extracted)),
                UNITY_VERSION="2022.3",
            )
            bundles = {"unit": {"bundleName": "live2d/model/unit"}}

            async def extract(_raw, _bundle, target, **_kwargs):
                output = Path(str(target)) / "live2d" / "model" / "unit"
                output.mkdir(parents=True)
                (output / "unit.model3.json").write_text("{}", encoding="utf-8")
                return [AsyncPath(str(output / "unit.model3.json"))]

            extract_bundle = AsyncMock(side_effect=extract)
            with patch("updater.postprocess.live2d_models.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("updater.postprocess.live2d_models.extract_asset_bundle", new=extract_bundle):
                    await recover_live2d_model_outputs(config, bundles)

            self.assertTrue((extracted / "live2d" / "model").is_dir())
            self.assertTrue(motion.is_dir())
            extract_bundle.assert_awaited_once()

    async def test_failed_recovery_keeps_existing_aggregate_model_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            (cache / "live2d" / "motion").mkdir(parents=True)
            for name in ("one", "two"):
                path = cache / "live2d" / "model" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"raw")
            existing = root / "extracted" / "live2d" / "model" / "good.model3.json"
            existing.parent.mkdir(parents=True)
            existing.write_text("old", encoding="utf-8")
            config = SimpleNamespace(
                LIVE2D_BUNDLE_CACHE_DIR=AsyncPath(str(cache)),
                ASSET_LOCAL_EXTRACTED_DIR=AsyncPath(str(root / "extracted")),
                UNITY_VERSION="2022.3",
            )
            calls = 0

            async def extract(_raw, _bundle, target, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError("bad cache")
                output = Path(str(target)) / "live2d" / "model" / "one.model3.json"
                output.parent.mkdir(parents=True)
                output.write_text("new", encoding="utf-8")
                return [AsyncPath(str(output))]

            with patch("updater.postprocess.live2d_models.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("updater.postprocess.live2d_models.extract_asset_bundle", new=AsyncMock(side_effect=extract)):
                    with self.assertRaisesRegex(RuntimeError, "extracting cached model bundle"):
                        await recover_live2d_model_outputs(
                            config,
                            {
                                "one": {"bundleName": "live2d/model/one"},
                                "two": {"bundleName": "live2d/model/two"},
                            },
                        )
            self.assertEqual(existing.read_text(encoding="utf-8"), "old")

    async def test_recovery_prevalidates_all_model_cache_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            (cache / "live2d" / "motion").mkdir(parents=True)
            first = cache / "live2d" / "model" / "one"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"raw")
            config = SimpleNamespace(
                LIVE2D_BUNDLE_CACHE_DIR=AsyncPath(str(cache)),
                ASSET_LOCAL_EXTRACTED_DIR=AsyncPath(str(root / "extracted")),
                UNITY_VERSION="2022.3",
            )
            extract = AsyncMock()
            with patch("updater.postprocess.live2d_models.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("updater.postprocess.live2d_models.extract_asset_bundle", new=extract):
                    with self.assertRaisesRegex(RuntimeError, "bundle file is missing"):
                        await recover_live2d_model_outputs(
                            config,
                            {
                                "one": {"bundleName": "live2d/model/one"},
                                "two": {"bundleName": "live2d/model/two"},
                            },
                        )
            extract.assert_not_awaited()

    async def test_forced_postprocess_recovers_even_with_partial_model_directory(self):
        config = SimpleNamespace(
            ASSET_LOCAL_EXTRACTED_DIR=AsyncPath("extracted"),
            LIVE2D_BUNDLE_CACHE_DIR=AsyncPath("cache"),
        )
        with patch.object(lifecycle, "recover_live2d_model_outputs", new=AsyncMock()) as recover:
            with patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
                await lifecycle._run_enabled_specialized_postprocess(
                    "live2d", config, False, {"unit": {"bundleName": "live2d/model/unit"}}
                )
        recover.assert_awaited_once()
        postprocess.assert_awaited_once()

    async def test_forced_live2d_recovery_requires_cached_motion_source(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            cached_model = cache / "live2d" / "model" / "unit"
            cached_model.parent.mkdir(parents=True)
            cached_model.write_bytes(b"raw bundle")
            config = SimpleNamespace(
                LIVE2D_BUNDLE_CACHE_DIR=AsyncPath(str(cache)),
                ASSET_LOCAL_EXTRACTED_DIR=AsyncPath(str(root / "extracted")),
                UNITY_VERSION="2022.3",
            )
            with patch("updater.postprocess.live2d_models.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with self.assertRaisesRegex(RuntimeError, "cached motion source is missing"):
                    await recover_live2d_model_outputs(
                        config, {"unit": {"bundleName": "live2d/model/unit"}}
                    )

    async def test_assets_noop_runs_enabled_specialized_postprocessing(self):
        config = SimpleNamespace(
            DL_LIST_CACHE_PATH=AsyncPath("/tmp/unused-dl-list.json"),
            ENABLE_LIVE2D_POSTPROCESS=True,
            ENABLE_CHARTS_POSTPROCESS=False,
        )

        with patch.object(lifecycle, "_run_enabled_specialized_postprocess", new=AsyncMock()
        ) as postprocess:
            await lifecycle._complete_with_empty_download_list(
                config,
                "assets",
                [],
                True,
                0,
            )

        postprocess.assert_awaited_once_with("assets", config, True)

    async def test_assets_noop_live2d_recovers_models_from_cache_before_postprocessing(self):
        config = SimpleNamespace(
            DL_LIST_CACHE_PATH=AsyncPath("/tmp/unused-dl-list.json"),
            ENABLE_LIVE2D_POSTPROCESS=True,
            ENABLE_CHARTS_POSTPROCESS=False,
        )
        bundles = {"unit": {"bundleName": "live2d/model/unit"}}

        with patch.object(lifecycle, "recover_live2d_model_outputs", new=AsyncMock()) as recover:
            with patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as process:
                await lifecycle._complete_with_empty_download_list(
                    config,
                    "assets",
                    [],
                    True,
                    0,
                    live2d_bundles=bundles,
                )

        recover.assert_awaited_once_with(config, bundles)
        process.assert_awaited_once_with(
            "live2d", config, extracted_dir_is_temporary=True, skip_missing_sources=True
        )

    async def test_forced_specialized_noop_retains_postprocessing(self):
        config = SimpleNamespace(DL_LIST_CACHE_PATH=AsyncPath("/tmp/unused-dl-list.json"))

        with patch.object(lifecycle, "_run_enabled_specialized_postprocess", new=AsyncMock()
        ) as postprocess:
            await lifecycle._complete_with_empty_download_list(
                config,
                "live2d",
                [],
                True,
                0,
            )

        postprocess.assert_awaited_once_with("live2d", config, True)

    async def test_live2d_postprocess_uses_live2d_sources_and_storage(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            extracted_dir = root / "extracted"
            model_dir = extracted_dir / "live2d" / "model" / "unit"
            model_dir.mkdir(parents=True)
            (model_dir / "unit.model3.json").write_text("{}", encoding="utf-8")
            bundle_cache = root / "bundle-cache"
            (bundle_cache / "live2d" / "motion").mkdir(parents=True)
            config = SimpleNamespace(
                ASSET_LOCAL_EXTRACTED_DIR=extracted_dir,
                DL_LIST_CACHE_PATH=root / "cache" / "dl_list.json",
                LIVE2D_BUNDLE_CACHE_DIR=bundle_cache,
                UNITY_VERSION="2022.3",
                REGION=SimpleNamespace(name="JP"),
                ASSET_REMOTE_STORAGE=[
                    {
                        "type": "live2d",
                        "base": "live-target",
                        "program": "rclone",
                        "args": ["copy", "src", "dst"],
                    },
                    {"type": "charts", "base": "chart-target", "program": "rclone", "args": []},
                ],
            )

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(live2d_models, "upload_directory", new=AsyncMock()) as upload:
                    process = MagicMock(returncode=0)
                    process.communicate = AsyncMock(
                        return_value=(
                            b'[{"Path":"unit/unit.model3.json","IsDir":false}]',
                            b"",
                        )
                    )
                    process.wait = AsyncMock()
                    with patch.object(
                        asyncio,
                        "create_subprocess_exec",
                        new=AsyncMock(return_value=process),
                    ):
                        await run_specialized_postprocess("live2d", config)

            restore.assert_awaited_once_with(
                AsyncPath(str(bundle_cache / "live2d" / "motion")),
                AsyncPath(str(extracted_dir / "live2d" / "motion")),
                AsyncPath(str(extracted_dir / "live2d" / "model")),
                "2022.3",
                config=config,
                param_id_map={},
                bundle_paths=[],
            )
            self.assertEqual(upload.await_count, 2)
            self.assertEqual(
                upload.await_args_list[0].args[1], AsyncPath("live-target/live2d")
            )
            self.assertEqual(
                upload.await_args_list[1].args[1], AsyncPath("live-target/live2d")
            )

    async def test_live2d_listing_failure_does_not_publish_index(self):
        with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()) as assets:
            with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()
            ) as publish:
                process = MagicMock(returncode=1)
                process.communicate = AsyncMock(return_value=(b"[]", b"failed"))
                with patch.object(
                    asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ):
                    with self.assertRaises(RuntimeError):
                        await live2d_models._remote_model_list(
                            {
                                "base": "sekai-ts:",
                                "program": "rclone",
                                "args": ["copy", "src", "dst"],
                            },
                            SimpleNamespace(EXTERNAL_PROCESS_TIMEOUT=5),
                        )
                assets.assert_not_awaited()
                publish.assert_not_awaited()

    async def test_live2d_rejects_destructive_storage_before_processing(self):
        config = SimpleNamespace(
            ASSET_LOCAL_EXTRACTED_DIR=Path("extracted"),
            LIVE2D_BUNDLE_CACHE_DIR=Path("cache"),
            UNITY_VERSION="2022.3",
            ASSET_REMOTE_STORAGE=[
                {
                    "type": "live2d",
                    "base": "live-target",
                    "program": "rclone",
                    "args": ["sync", "src", "dst"],
                }
            ],
        )
        with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
            with patch.object(live2d_models, "upload_directory", new=AsyncMock()) as upload:
                with patch.object(
                    asyncio, "create_subprocess_exec", new=AsyncMock()
                ) as execute:
                    with self.assertRaisesRegex(ValueError, "copy or copyto"):
                        await run_specialized_postprocess("live2d", config)
        restore.assert_not_awaited()
        upload.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_optional_live2d_skips_when_sources_are_missing(self):
        config = SimpleNamespace(
            ASSET_LOCAL_EXTRACTED_DIR=Path("extracted"),
            LIVE2D_BUNDLE_CACHE_DIR=Path("cache"),
            UNITY_VERSION="2022.3",
            ASSET_REMOTE_STORAGE=[
                {
                    "type": "live2d",
                    "base": "live",
                    "program": "rclone",
                    "args": ["copy", "src", "dst"],
                }
            ],
        )
        with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
            await run_specialized_postprocess("live2d", config, skip_missing_sources=True)
        restore.assert_not_awaited()

    async def test_forced_live2d_raises_when_sources_are_missing(self):
        config = SimpleNamespace(
            ASSET_LOCAL_EXTRACTED_DIR=Path("extracted"),
            LIVE2D_BUNDLE_CACHE_DIR=Path("cache"),
            UNITY_VERSION="2022.3",
            ASSET_REMOTE_STORAGE=[],
        )
        with self.assertRaisesRegex(RuntimeError, "sources are missing"):
            await run_specialized_postprocess("live2d", config)

    async def test_optional_charts_skips_when_sources_and_fallback_are_missing(self):
        with __import__("tempfile").TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                ASSET_LOCAL_EXTRACTED_DIR=Path(temp_dir),
                REGION=SimpleNamespace(name="JP"),
                ASSET_REMOTE_STORAGE=[],
            )
            with patch.object(dispatch, "fetch_chart_sources_from_storage",
                new=AsyncMock(side_effect=RuntimeError("no source")),
            ) as fetch:
                with patch.object(dispatch, "_render_charts", new=AsyncMock()) as render:
                    await run_specialized_postprocess("charts", config, skip_missing_sources=True)
            fetch.assert_awaited_once()
            render.assert_not_awaited()

    async def test_forced_charts_raises_when_sources_and_fallback_are_missing(self):
        with __import__("tempfile").TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                ASSET_LOCAL_EXTRACTED_DIR=Path(temp_dir),
                REGION=SimpleNamespace(name="JP"),
                ASSET_REMOTE_STORAGE=[],
            )
            with self.assertRaisesRegex(RuntimeError, "normal ASSET_REMOTE_STORAGE"):
                await run_specialized_postprocess("charts", config)

    async def test_empty_listing_does_not_publish_index_and_uses_process_timeout(self):
        process = MagicMock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"[]", b""))
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as execute:
            with patch.object(live2d_models, "_get_external_process_timeout", return_value=7
            ) as timeout:
                with self.assertRaises(RuntimeError):
                    await live2d_models._remote_model_list(
                        {"base": "sekai-ts:", "program": "rclone", "args": ["copy", "src", "dst"]},
                        SimpleNamespace(),
                    )
        timeout.assert_called_once()
        execute.assert_awaited_once_with(
            "rclone",
            "lsjson",
            "sekai-ts:/live2d/model",
            "--recursive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def test_charts_postprocess_uses_local_scores_and_chart_storage(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            extracted_dir = Path(temp_dir)
            score = extracted_dir / "music" / "music_score" / "001_song" / "master.txt"
            score.parent.mkdir(parents=True)
            score.write_text("# SUS", encoding="utf-8")
            config = SimpleNamespace(
                ASSET_LOCAL_EXTRACTED_DIR=extracted_dir,
                DL_LIST_CACHE_PATH=Path(temp_dir) / "cache" / "dl_list.json",
                REGION=SimpleNamespace(name="JP"),
                ASSET_REMOTE_STORAGE=[
                    {"type": "live2d", "base": "live-target", "program": "rclone", "args": []},
                    {"type": "charts", "base": "chart-target", "program": "rclone", "args": []},
                ],
            )

            rendered_dirs = []

            async def render_charts(_config, source_dir, _include_list=None, score_files=None):
                rendered_dirs.append(source_dir)
                (source_dir / "charts" / "jp").mkdir(parents=True)
                return set()

            with patch.object(dispatch, "fetch_chart_sources_from_storage", new=AsyncMock()
            ) as fetch:
                with patch.object(dispatch, "_render_charts", new=render_charts):
                    with patch.object(dispatch, "upload_directory", new=AsyncMock()) as upload:
                        await run_specialized_postprocess("charts", config)

            fetch.assert_not_awaited()
            self.assertEqual(rendered_dirs, [extracted_dir])
            upload.assert_awaited_once_with(
                AsyncPath(str(extracted_dir / "charts" / "jp")),
                AsyncPath("chart-target/jp"),
                "rclone",
                [],
                config=config,
            )


class ChartIncrementalStateTest(unittest.IsolatedAsyncioTestCase):
    """Tests for incremental chart rendering with persisted state."""

    def _make_score(self, workspace: Path, directory: str, content: str = "# SUS"):
        """Create a fake score file and return its path."""
        score = workspace / "music" / "music_score" / directory / "master.txt"
        score.parent.mkdir(parents=True, exist_ok=True)
        score.write_text(content, encoding="utf-8")
        return score

    def _make_config(self, temp_dir: str, **overrides) -> SimpleNamespace:
        defaults = dict(
            ASSET_LOCAL_EXTRACTED_DIR=Path(temp_dir),
            DL_LIST_CACHE_PATH=Path(temp_dir) / "cache" / "dl_list.json",
            REGION=SimpleNamespace(name="JP"),
            CHART_DATA_SERVER=None,
            CHART_JACKET_BASE_URL=None,
            ASSET_REMOTE_STORAGE=[
                {
                    "type": "charts",
                    "base": "chart-target",
                    "program": "rclone",
                    "args": ["copy", "src", "dst"],
                },
            ],
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _state_file(self, config) -> Path:
        return Path(config.DL_LIST_CACHE_PATH).parent / "chart_state.json"

    async def _run_charts(self, config, workspace, include_list=None):
        """Run _process_charts with mocked render and upload."""
        rendered_files: list[str] = []

        async def render(
            _config, _extracted_dir, _include_list=None, score_files=None
        ):
            rendered_files.extend(score_files or [])
            (workspace / "charts" / "jp").mkdir(parents=True, exist_ok=True)
            return set(score_files or [])

        upload_calls: list = []

        async def upload_dir(mode, source_dir, config):
            upload_calls.append((mode, str(source_dir)))

        with patch.object(dispatch, "_render_charts", new=render):
            with patch.object(dispatch, "_upload_specialized_directory", new=upload_dir
            ):
                await dispatch._process_charts(config, workspace, include_list)
        return rendered_files, upload_calls

    async def test_initial_build_no_state_renders_all_and_persists(self):
        """No state file → all scores rendered, upload invoked, state written after upload."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            self._make_score(workspace, "002_song", "score-b")
            config = self._make_config(temp_dir)
            state_file = self._state_file(config)
            self.assertFalse(state_file.exists())

            rendered, uploads = await self._run_charts(config, workspace)

            self.assertEqual(sorted(rendered), ["001_song/master.txt", "002_song/master.txt"])
            self.assertEqual(len(uploads), 1)
            self.assertTrue(state_file.exists())
            state = load_chart_state(state_file)
            self.assertIsNotNone(state)
            self.assertIn("001_song/master.txt", state["scores"])
            self.assertIn("002_song/master.txt", state["scores"])

    async def test_incremental_addition_renders_only_new(self):
        """Run twice with one new file → only the new file rendered."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            config = self._make_config(temp_dir)

            # First run: full build
            rendered1, uploads1 = await self._run_charts(config, workspace)
            self.assertEqual(len(rendered1), 1)
            self.assertEqual(len(uploads1), 1)

            # Add a new score
            self._make_score(workspace, "002_song", "score-b")
            rendered2, uploads2 = await self._run_charts(config, workspace)
            self.assertEqual(rendered2, ["002_song/master.txt"])
            self.assertEqual(len(uploads2), 1)

            state = load_chart_state(self._state_file(config))
            self.assertEqual(len(state["scores"]), 2)

    async def test_content_change_renders_only_changed(self):
        """Rewrite one score's bytes → only it re-renders."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            self._make_score(workspace, "002_song", "score-b")
            config = self._make_config(temp_dir)

            # First run
            rendered1, _ = await self._run_charts(config, workspace)
            self.assertEqual(len(rendered1), 2)

            # Change content of one score
            self._make_score(workspace, "002_song", "score-b-changed")
            rendered2, _ = await self._run_charts(config, workspace)
            self.assertEqual(rendered2, ["002_song/master.txt"])

    async def test_noop_skips_render_and_upload(self):
        """Third identical run → render and upload not called."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            config = self._make_config(temp_dir)

            # Run twice to establish state
            await self._run_charts(config, workspace)
            await self._run_charts(config, workspace)

            # Third run: should be a no-op
            render_called = False

            async def render(
                _config, _extracted_dir, _include_list=None, score_files=None
            ):
                nonlocal render_called
                render_called = True
                return set()

            upload_called = False

            async def upload_dir(mode, source_dir, config):
                nonlocal upload_called
                upload_called = True

            with patch.object(dispatch, "_render_charts", new=render):
                with patch.object(dispatch, "_upload_specialized_directory", new=upload_dir
                ):
                    await dispatch._process_charts(config, workspace)

            self.assertFalse(render_called)
            self.assertFalse(upload_called)

    async def test_upload_failure_preserves_old_state(self):
        """Upload raises → state file still holds old hashes; rerun retries."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            config = self._make_config(temp_dir)

            # First successful run
            await self._run_charts(config, workspace)
            state_after_first = load_chart_state(self._state_file(config))
            self.assertIn("001_song/master.txt", state_after_first["scores"])

            # Add a new score and fail the upload
            self._make_score(workspace, "002_song", "score-b")

            async def render(
                _config, _extracted_dir, _include_list=None, score_files=None
            ):
                (workspace / "charts" / "jp").mkdir(parents=True, exist_ok=True)
                return set(score_files or [])

            async def failing_upload(mode, source_dir, config):
                raise RuntimeError("upload failed")

            with patch.object(dispatch, "_render_charts", new=render):
                with patch.object(dispatch, "_upload_specialized_directory", new=failing_upload
                ):
                    with self.assertRaisesRegex(RuntimeError, "upload failed"):
                        await dispatch._process_charts(config, workspace)

            # State should still be the old state (no 002)
            state_after_fail = load_chart_state(self._state_file(config))
            self.assertIn("001_song/master.txt", state_after_fail["scores"])
            self.assertNotIn("002_song/master.txt", state_after_fail["scores"])

            # Rerun succeeds
            rendered, _ = await self._run_charts(config, workspace)
            self.assertEqual(rendered, ["002_song/master.txt"])

    async def test_corrupt_state_triggers_full_rebuild(self):
        """Corrupt state file → treated as full rebuild, no crash."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            config = self._make_config(temp_dir)
            state_file = self._state_file(config)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("NOT VALID JSON {{{", encoding="utf-8")

            rendered, _ = await self._run_charts(config, workspace)
            # Full rebuild: all scores rendered
            self.assertEqual(sorted(rendered), ["001_song/master.txt"])
            state = load_chart_state(state_file)
            self.assertIsNotNone(state)
            self.assertIn("001_song/master.txt", state["scores"])

    async def test_fingerprint_change_triggers_full_rebuild(self):
        """Mutate CHART_DATA_SERVER → full rebuild even with existing state."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            config = self._make_config(temp_dir)

            # First run
            await self._run_charts(config, workspace)

            # Mutate fingerprint by changing CHART_DATA_SERVER
            config.CHART_DATA_SERVER = "tc"
            rendered, _ = await self._run_charts(config, workspace)
            # Full rebuild means all scores rendered again
            self.assertEqual(sorted(rendered), ["001_song/master.txt"])

            state = load_chart_state(self._state_file(config))
            self.assertEqual(state["fingerprint"]["data_server"], "tc")

    async def test_merge_preserves_previously_published_scores(self):
        """Narrowing include list does not drop already-published scores from state."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            self._make_score(workspace, "001_song", "score-a")
            self._make_score(workspace, "002_song", "score-b")
            config = self._make_config(temp_dir)

            # Full build
            await self._run_charts(config, workspace)
            state = load_chart_state(self._state_file(config))
            self.assertEqual(len(state["scores"]), 2)

            # Narrow include list to only 001 — 002 is not rendered but
            # its hash should remain in state (rclone never deletes remotely).
            # Since 001's hash hasn't changed, nothing is re-rendered.
            rendered, _ = await self._run_charts(
                config, workspace, include_list=[r"^music/music_score/001_song$"]
            )
            self.assertEqual(rendered, [])
            state2 = load_chart_state(self._state_file(config))
            self.assertEqual(len(state2["scores"]), 2)
            self.assertIn("002_song/master.txt", state2["scores"])

    async def test_validate_chart_state_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            validate_chart_state(
                {
                    "schema_version": 1,
                    "fingerprint": {
                        "region": "jp",
                        "data_server": "jp",
                        "jacket_base_url": "...",
                    },
                    "scores": {},
                    "extra": True,
                }
            )

    async def test_validate_chart_state_rejects_bad_hash(self):
        with self.assertRaisesRegex(ValueError, "64-char lowercase hex"):
            validate_chart_state(
                {
                    "schema_version": 1,
                    "fingerprint": {
                        "region": "jp",
                        "data_server": "jp",
                        "jacket_base_url": "...",
                    },
                    "scores": {"a.txt": "NOT-A-HASH"},
                }
            )

    async def test_pending_score_paths_detects_new_and_changed(self):
        current = {"a.txt": "aaa", "b.txt": "bbb", "c.txt": "ccc"}
        stored = {"a.txt": "aaa", "b.txt": "OLD"}
        self.assertEqual(pending_score_paths(current, stored), ["b.txt", "c.txt"])

    async def test_pending_score_paths_empty_when_identical(self):
        data = {"a.txt": "aaa", "b.txt": "bbb"}
        self.assertEqual(pending_score_paths(data, data), [])

    async def test_hash_score_file_deterministic(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.txt"
            path.write_bytes(b"hello world")
            h1 = hash_score_file(path)
            h2 = hash_score_file(path)
            self.assertEqual(h1, h2)
            self.assertEqual(len(h1), 64)

    async def test_compute_score_hashes_uses_posix_relpaths(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._make_score(root, "001_song")
            hashes = compute_score_hashes(root)
            self.assertIn("001_song/master.txt", hashes)
            self.assertEqual(len(hashes), 1)

    async def test_chart_state_path_siblings_dl_list(self):
        config = SimpleNamespace(
            DL_LIST_CACHE_PATH=Path("cache", "jp", "json", "dl_list.json")
        )
        self.assertEqual(
            chart_state_path(config), Path("cache", "jp", "json", "chart_state.json")
        )

    async def test_chart_fingerprint_includes_all_fields(self):
        config = SimpleNamespace(
            REGION=SimpleNamespace(name="JP"),
            CHART_DATA_SERVER="tc",
            CHART_JACKET_BASE_URL=None,
        )
        fp = chart_fingerprint(config)
        self.assertEqual(fp["region"], "jp")
        self.assertEqual(fp["data_server"], "tc")
        self.assertIn("jacket_base_url", fp)
        # Default jacket base URL uses the region name, not data_server
        self.assertIn("jp", fp["jacket_base_url"])


class Live2DIncrementalStateTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the Live2D incremental motion state helpers."""

    def _make_motion(self, root: Path, name: str, data: bytes = b"motion-data") -> Path:
        path = root / name
        path.write_bytes(data)
        return path

    def _make_model_dir(self, root: Path, moc3_names: list[str] | None = None) -> Path:
        """Create a minimal live2d model directory tree.

        Moc3 files are filled with 128 zero bytes so the header addresses
        are both zero and ``extract_params_ids_from_moc3`` returns an empty
        map without crashing.
        """
        model_dir = root / "live2d" / "model" / "unit"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "unit.model3.json").write_text("{}", encoding="utf-8")
        for name in (moc3_names or ["unit.moc3"]):
            (model_dir / name).write_bytes(b"\x00" * 272)
        return root / "live2d" / "model"

    def _make_config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            DL_LIST_CACHE_PATH=root / "cache" / "dl_list.json",
            UNITY_VERSION="2022.3",
        )

    def _live2d_state_file(self, root: Path) -> Path:
        return root / "cache" / "live2d_motion_state.json"

    # --- Initial build ---

    async def test_initial_build_restores_all_bundles_and_persists_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            self._make_motion(motion_source, "a.bundle")
            self._make_motion(motion_source, "b.bundle")
            self._make_model_dir(root)
            config = self._make_config(root)

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            restore.assert_awaited_once()
            _, kwargs = restore.call_args
            restored_names = sorted(p.name for p in kwargs["bundle_paths"])
            self.assertEqual(restored_names, ["a.bundle", "b.bundle"])

            state_file = self._live2d_state_file(root)
            state = validate_live2d_state(json.loads(state_file.read_bytes()))
            self.assertEqual(state["schema_version"], 1)
            self.assertIn("unity_version", state["fingerprint"])
            self.assertIn("model_hash", state["fingerprint"])
            self.assertIn("a.bundle", state["motions"])
            self.assertIn("b.bundle", state["motions"])
            self.assertEqual(len(state["motions"]), 2)

    # --- Incremental add ---

    async def test_incremental_add_skips_unchanged_and_restores_new(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            self._make_motion(motion_source, "a.bundle", b"v1")
            self._make_motion(motion_source, "b.bundle", b"v1")
            self._make_model_dir(root)
            config = self._make_config(root)

            # First build to populate state
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()):
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            # Add c.bundle
            self._make_motion(motion_source, "c.bundle", b"v1")

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            restore.assert_awaited_once()
            _, kwargs = restore.call_args
            restored_names = sorted(p.name for p in kwargs["bundle_paths"])
            self.assertEqual(restored_names, ["c.bundle"])

            state = validate_live2d_state(json.loads(self._live2d_state_file(root).read_bytes()))
            self.assertIn("a.bundle", state["motions"])
            self.assertIn("b.bundle", state["motions"])
            self.assertIn("c.bundle", state["motions"])
            self.assertEqual(len(state["motions"]), 3)

    # --- Content change ---

    async def test_content_change_restores_only_changed_bundles(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            a = self._make_motion(motion_source, "a.bundle", b"v1")
            b = self._make_motion(motion_source, "b.bundle", b"v1")
            self._make_model_dir(root)
            config = self._make_config(root)

            # First build
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()):
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            # Change a.bundle
            a.write_bytes(b"v2")

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            _, kwargs = restore.call_args
            restored_names = [p.name for p in kwargs["bundle_paths"]]
            self.assertEqual(restored_names, ["a.bundle"])

            state = validate_live2d_state(json.loads(self._live2d_state_file(root).read_bytes()))
            self.assertEqual(state["motions"]["a.bundle"], hash_score_file(a))
            self.assertEqual(state["motions"]["b.bundle"], hash_score_file(b))

    # --- No-op ---

    async def test_no_op_skips_restore_and_upload(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            self._make_motion(motion_source, "a.bundle", b"v1")
            self._make_model_dir(root)
            config = self._make_config(root)

            # First build
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()):
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            # Nothing changed — second run should be a no-op
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()) as upload:
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            restore.assert_not_awaited()
            upload.assert_not_awaited()

    # --- Fingerprint change ---

    async def test_moc3_change_invalidates_fingerprint_and_restores_all(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            self._make_motion(motion_source, "a.bundle", b"v1")
            self._make_model_dir(root)
            config = self._make_config(root)

            # First build
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()):
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            # Change moc3 file → fingerprint invalidation → full restore
            (root / "live2d" / "model" / "unit" / "unit.moc3").write_bytes(b"\x01" * 272)

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            _, kwargs = restore.call_args
            restored_names = [p.name for p in kwargs["bundle_paths"]]
            self.assertEqual(restored_names, ["a.bundle"])

            state = validate_live2d_state(json.loads(self._live2d_state_file(root).read_bytes()))
            # New fingerprint should differ from the initial one
            self.assertIn("unity_version", state["fingerprint"])
            self.assertIn("model_hash", state["fingerprint"])

    # --- State persistence merge ---

    async def test_merge_preserves_previous_motions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            motion_source = root / "bundle-cache" / "live2d" / "motion"
            motion_source.mkdir(parents=True)
            a = self._make_motion(motion_source, "a.bundle", b"v1")
            b = self._make_motion(motion_source, "b.bundle", b"v1")
            self._make_model_dir(root)
            config = self._make_config(root)

            # First build: restore a only (mock to only claim we restored a)
            async def restore_first(config_arg, motion_extracted, model_extracted, unity_version, **kwargs):
                pass
            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock(side_effect=restore_first)):
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            state1 = validate_live2d_state(json.loads(self._live2d_state_file(root).read_bytes()))
            self.assertIn("a.bundle", state1["motions"])
            self.assertIn("b.bundle", state1["motions"])

            # Add c.bundle, change a.bundle
            c = self._make_motion(motion_source, "c.bundle", b"v1")
            a.write_bytes(b"v2")

            with patch.object(dispatch, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(dispatch, "_upload_live2d_assets", new=AsyncMock()):
                    with patch.object(dispatch, "_remote_model_list", new=AsyncMock(return_value={})):
                        with patch.object(dispatch, "_publish_live2d_model_list", new=AsyncMock()):
                            await dispatch._process_live2d(
                                config, StdPath(str(motion_source)), StdPath(str(root))
                            )

            _, kwargs = restore.call_args
            restored_names = sorted(p.name for p in kwargs["bundle_paths"])
            self.assertEqual(restored_names, ["a.bundle", "c.bundle"])

            state2 = validate_live2d_state(json.loads(self._live2d_state_file(root).read_bytes()))
            self.assertEqual(len(state2["motions"]), 3)
            self.assertEqual(state2["motions"]["a.bundle"], hash_score_file(a))
            self.assertEqual(state2["motions"]["b.bundle"], hash_score_file(b))
            self.assertEqual(state2["motions"]["c.bundle"], hash_score_file(c))

    # --- Validation helpers ---

    async def test_validate_live2d_state_rejects_missing_schema_version(self):
        with self.assertRaises(ValueError):
            validate_live2d_state({"fingerprint": {}, "motions": {}})

    async def test_validate_live2d_state_rejects_bad_fingerprint(self):
        value = {
            "schema_version": 1,
            "fingerprint": {"unity_version": 123, "model_hash": "abc"},
            "motions": {},
        }
        with self.assertRaises(ValueError):
            validate_live2d_state(value)

    async def test_validate_live2d_state_rejects_bad_motion_hash(self):
        value = {
            "schema_version": 1,
            "fingerprint": {"unity_version": "2022.3", "model_hash": "abc"},
            "motions": {"a.bundle": "short"},
        }
        with self.assertRaises(ValueError):
            validate_live2d_state(value)

    async def test_validate_live2d_state_rejects_unknown_fields(self):
        value = {
            "schema_version": 1,
            "fingerprint": {"unity_version": "2022.3", "model_hash": "abc"},
            "motions": {},
            "unknown_field": True,
        }
        with self.assertRaises(ValueError):
            validate_live2d_state(value)

    async def test_load_live2d_state_returns_none_for_missing_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            result = load_live2d_state(StdPath(str(Path(temp_dir) / "nonexistent.json")))
            self.assertIsNone(result)

    async def test_load_live2d_state_returns_none_for_corrupt_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "corrupt.json"
            path.write_bytes(b"not json!!!")
            result = load_live2d_state(StdPath(str(path)))
            self.assertIsNone(result)

    # --- Hash helpers ---

    async def test_compute_motion_bundle_hashes_deterministic(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._make_motion(root, "a.bundle", b"data")
            h1 = compute_motion_bundle_hashes(StdPath(str(root)))
            h2 = compute_motion_bundle_hashes(StdPath(str(root)))
            self.assertEqual(h1, h2)
            self.assertEqual(h1["a.bundle"], hash_score_file(root / "a.bundle"))

    async def test_pending_motion_bundles_detects_addition(self):
        current = {"a": "h1", "b": "h2"}
        stored = {"a": "h1"}
        self.assertEqual(pending_motion_bundles(current, stored), ["b"])

    async def test_pending_motion_bundles_detects_content_change(self):
        current = {"a": "h_new"}
        stored = {"a": "h_old"}
        self.assertEqual(pending_motion_bundles(current, stored), ["a"])

    async def test_pending_motion_bundles_returns_empty_when_unchanged(self):
        current = {"a": "h1", "b": "h2"}
        stored = {"a": "h1", "b": "h2"}
        self.assertEqual(pending_motion_bundles(current, stored), [])

    # --- Path helpers ---

    async def test_live2d_state_path_siblings_dl_list(self):
        config = SimpleNamespace(
            DL_LIST_CACHE_PATH=Path("cache", "jp", "json", "dl_list.json")
        )
        self.assertEqual(
            live2d_state_path(config), Path("cache", "jp", "json", "live2d_motion_state.json")
        )
