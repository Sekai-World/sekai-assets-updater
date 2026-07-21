import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import filter_bundles_for_mode, get_mode_bundle_prefixes
from helpers import select_bundles_for_download
from specialized import (
    collect_score_files,
    get_enabled_specialized_modes,
    get_required_bundle_prefixes,
    get_specialized_storage,
    get_normal_storage_candidates,
    has_local_chart_sources,
    needs_temporary_chart_source,
    music_id_from_score_path,
    mode_uses_bundle_pipeline,
    needs_live2d_bundle_cache,
    needs_shared_workspace,
)
from bundle import is_live2d_bundle
from worker import get_bundle_cache_path, get_bundle_cache_root


class SpecializedHelpersTest(unittest.TestCase):
    def config(self):
        return SimpleNamespace(
            ENABLE_LIVE2D_POSTPROCESS=False,
            ENABLE_CHARTS_POSTPROCESS=False,
            LIVE2D_REMOTE_STORAGE=[
                {"base": "live", "program": "rclone", "args": ["copy", "src", "dst"]}
            ],
            CHARTS_REMOTE_STORAGE=[
                {"base": "charts", "program": "rclone", "args": ["copy", "src", "dst"]}
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
        config.ASSET_REMOTE_STORAGE = [{"type": "live2d"}]
        self.assertEqual(get_specialized_storage(config, "live2d"), config.LIVE2D_REMOTE_STORAGE)
        self.assertEqual(get_specialized_storage(config, "charts"), config.CHARTS_REMOTE_STORAGE)

    def test_specialized_mode_prefixes_are_mandatory(self):
        bundles = {
            "live": {"bundleName": "live2d/model/a"},
            "score": {"bundleName": "music/music_score/a"},
            "other": {"bundleName": "music/a"},
        }
        self.assertEqual(get_mode_bundle_prefixes("live2d"), ("live2d/",))
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

    def test_charts_have_no_automatic_bundle_prefix(self):
        config = self.config()
        config.ENABLE_CHARTS_POSTPROCESS = True
        self.assertEqual(get_required_bundle_prefixes("assets", config), ())
        self.assertEqual(get_required_bundle_prefixes("charts", config), ())

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
