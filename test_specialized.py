import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import filter_bundles_for_mode, get_mode_bundle_prefixes
from specialized import (
    collect_score_files,
    get_enabled_specialized_modes,
    get_specialized_storage,
    music_id_from_score_path,
)
from bundle import is_live2d_bundle


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
        self.assertEqual(list(filter_bundles_for_mode(bundles, "charts")), ["score"])
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
