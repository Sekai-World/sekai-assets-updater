import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import specialized
import main
from anyio import Path as AsyncPath
from helpers import filter_bundles_for_mode, get_mode_bundle_prefixes
from helpers import select_bundles_for_download
from utils.chart import get_json_url
from specialized import (
    collect_score_files,
    get_enabled_specialized_modes,
    get_chart_data_server,
    get_required_bundle_prefixes,
    get_specialized_storage,
    get_normal_storage_candidates,
    has_local_chart_sources,
    needs_temporary_chart_source,
    music_id_from_score_path,
    mode_uses_bundle_pipeline,
    needs_live2d_bundle_cache,
    needs_shared_workspace,
    retains_live2d_extracted_outputs,
    run_specialized_postprocess,
)
from bundle import is_live2d_bundle
from worker import get_bundle_cache_path, get_bundle_cache_root, recover_live2d_model_outputs


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
            specialized._models_from_remote_entries(
                [{"Path": "../old.model3.json", "IsDir": False}]
            )
        with self.assertRaises(ValueError):
            specialized._models_from_remote_entries(
                [{"Path": "/absolute.model3.json", "IsDir": False}]
            )

    def test_listing_args_preserve_flags_and_use_exact_target(self):
        storage = {
            "base": "sekai-ts:",
            "program": "rclone",
            "args": ["copyto", "src", "dst", "--s3-no-check-bucket", "--config", "opaque.conf"],
        }
        self.assertEqual(
            specialized._listing_args(storage, "sekai-ts:/live2d/model"),
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
        with patch.object(main, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
            await main._run_enabled_specialized_postprocess("assets", config, False)
        postprocess.assert_awaited_once_with(
            "charts",
            config,
            extracted_dir_is_temporary=False,
            skip_missing_sources=True,
            score_include_list=config.DL_INCLUDE_LIST,
        )

    async def test_forced_charts_does_not_pass_download_include_list(self):
        config = SimpleNamespace(DL_INCLUDE_LIST=[r"^music/music_score/001_song$"])
        with patch.object(main, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
            await main._run_enabled_specialized_postprocess("charts", config, False)
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

            with patch.object(
                specialized, "get_list", new=AsyncMock(return_value=[{"id": 1}, {"id": 2}])
            ):
                with patch.object(specialized, "render_chart", new=AsyncMock()) as render:
                    await specialized._render_charts(
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

            with patch.object(
                specialized,
                "get_list",
                new=AsyncMock(return_value=[{"id": 1}, {"id": 12345}]),
            ):
                with patch.object(specialized, "render_chart", new=AsyncMock()) as render:
                    await specialized._render_charts(config, extracted_dir)

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
            with patch("worker.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("worker.extract_asset_bundle", new=extract_bundle):
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

            with patch("worker.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("worker.extract_asset_bundle", new=AsyncMock(side_effect=extract)):
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
            with patch("worker.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
                with patch("worker.extract_asset_bundle", new=extract):
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
        with patch.object(main, "recover_live2d_model_outputs", new=AsyncMock()) as recover:
            with patch.object(main, "run_specialized_postprocess", new=AsyncMock()) as postprocess:
                await main._run_enabled_specialized_postprocess(
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
            with patch("worker.prepare_secure_directory", side_effect=lambda path: Path(str(path))):
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

        with patch.object(
            main, "_run_enabled_specialized_postprocess", new=AsyncMock()
        ) as postprocess:
            await main._complete_with_empty_download_list(
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

        with patch.object(main, "recover_live2d_model_outputs", new=AsyncMock()) as recover:
            with patch.object(main, "run_specialized_postprocess", new=AsyncMock()) as process:
                await main._complete_with_empty_download_list(
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

        with patch.object(
            main, "_run_enabled_specialized_postprocess", new=AsyncMock()
        ) as postprocess:
            await main._complete_with_empty_download_list(
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

            with patch.object(specialized, "restore_live2d_motions", new=AsyncMock()) as restore:
                with patch.object(specialized, "upload_directory", new=AsyncMock()) as upload:
                    process = MagicMock(returncode=0)
                    process.communicate = AsyncMock(
                        return_value=(
                            b'[{"Path":"unit/unit.model3.json","IsDir":false}]',
                            b"",
                        )
                    )
                    process.wait = AsyncMock()
                    with patch.object(
                        specialized.asyncio,
                        "create_subprocess_exec",
                        new=AsyncMock(return_value=process),
                    ):
                        await run_specialized_postprocess("live2d", config)

            restore.assert_awaited_once_with(
                specialized.Path(str(bundle_cache / "live2d" / "motion")),
                specialized.Path(str(extracted_dir / "live2d" / "motion")),
                specialized.Path(str(extracted_dir / "live2d" / "model")),
                "2022.3",
                config=config,
            )
            self.assertEqual(upload.await_count, 2)
            self.assertEqual(
                upload.await_args_list[0].args[1], specialized.Path("live-target/live2d")
            )
            self.assertEqual(
                upload.await_args_list[1].args[1], specialized.Path("live-target/live2d")
            )

    async def test_live2d_listing_failure_does_not_publish_index(self):
        with patch.object(specialized, "_upload_live2d_assets", new=AsyncMock()) as assets:
            with patch.object(
                specialized, "_publish_live2d_model_list", new=AsyncMock()
            ) as publish:
                process = MagicMock(returncode=1)
                process.communicate = AsyncMock(return_value=(b"[]", b"failed"))
                with patch.object(
                    specialized.asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ):
                    with self.assertRaises(RuntimeError):
                        await specialized._remote_model_list(
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
        with patch.object(specialized, "restore_live2d_motions", new=AsyncMock()) as restore:
            with patch.object(specialized, "upload_directory", new=AsyncMock()) as upload:
                with patch.object(
                    specialized.asyncio, "create_subprocess_exec", new=AsyncMock()
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
        with patch.object(specialized, "restore_live2d_motions", new=AsyncMock()) as restore:
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
            with patch.object(
                specialized,
                "fetch_chart_sources_from_storage",
                new=AsyncMock(side_effect=RuntimeError("no source")),
            ) as fetch:
                with patch.object(specialized, "_render_charts", new=AsyncMock()) as render:
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
            specialized.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as execute:
            with patch.object(
                specialized, "_get_external_process_timeout", return_value=7
            ) as timeout:
                with self.assertRaises(RuntimeError):
                    await specialized._remote_model_list(
                        {"base": "sekai-ts:", "program": "rclone", "args": ["copy", "src", "dst"]},
                        SimpleNamespace(),
                    )
        timeout.assert_called_once()
        execute.assert_awaited_once_with(
            "rclone",
            "lsjson",
            "sekai-ts:/live2d/model",
            "--recursive",
            stdout=specialized.asyncio.subprocess.PIPE,
            stderr=specialized.asyncio.subprocess.PIPE,
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
                REGION=SimpleNamespace(name="JP"),
                ASSET_REMOTE_STORAGE=[
                    {"type": "live2d", "base": "live-target", "program": "rclone", "args": []},
                    {"type": "charts", "base": "chart-target", "program": "rclone", "args": []},
                ],
            )

            rendered_dirs = []

            async def render_charts(_config, source_dir, _include_list=None):
                rendered_dirs.append(source_dir)
                (source_dir / "charts" / "jp").mkdir(parents=True)

            with patch.object(
                specialized, "fetch_chart_sources_from_storage", new=AsyncMock()
            ) as fetch:
                with patch.object(specialized, "_render_charts", new=render_charts) as render:
                    with patch.object(specialized, "upload_directory", new=AsyncMock()) as upload:
                        await run_specialized_postprocess("charts", config)

            fetch.assert_not_awaited()
            self.assertEqual(rendered_dirs, [extracted_dir])
            upload.assert_awaited_once_with(
                specialized.Path(str(extracted_dir / "charts" / "jp")),
                specialized.Path("chart-target/jp"),
                "rclone",
                [],
                config=config,
            )
