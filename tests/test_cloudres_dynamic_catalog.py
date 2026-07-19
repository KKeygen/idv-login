import os
import sys
import tempfile
import types
import unittest


SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

channel_utils = types.ModuleType("channelHandler.channelUtils")
channel_utils.cmp_game_id = lambda left, right: (
    str(left or "").split("-")[-1] == str(right or "").split("-")[-1]
)
sys.modules["channelHandler.channelUtils"] = channel_utils

from cloudRes import CloudRes


class FakeDynamicCatalog:
    def get_cloud_config(self, game_id):
        return {"game_id": "h55", "log_key": "dynamic"}

    def get_feature(self, game_id):
        return {
            "game_id": "h55",
            "download_distributions": [73],
            "downloadable": True,
        }


class CloudResDynamicCatalogTests(unittest.TestCase):
    def tearDown(self):
        CloudRes._instance = None
        CloudRes._initialized = False

    def test_static_cloud_entries_take_priority_over_dynamic_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            CloudRes._instance = None
            CloudRes._initialized = False
            cloud = CloudRes([], temp_dir)
            cloud.local_data = {
                "data": [{"game_id": "h55", "log_key": "static"}],
                "feature_game_short_ids": [{
                    "game_id": "h55",
                    "download_distributions": [73, 134],
                    "downloadable": True,
                }],
            }
            cloud.dynamic_game_catalog = FakeDynamicCatalog()

            self.assertEqual(cloud.get_by_game_id("h55")["log_key"], "static")
            self.assertEqual(cloud.get_by_game_id_and_key("h55", "log_key"), "static")
            self.assertEqual(
                cloud.get_feature_by_game_id("h55")["download_distributions"],
                [73, 134],
            )


if __name__ == "__main__":
    unittest.main()
