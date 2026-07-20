import json
import os
import sys
import tempfile
import unittest


SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from nativebridge import NativeTaskRegistry


class NativeTaskRegistryTests(unittest.TestCase):
    def test_download_supervisor_status_is_merged_into_http_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = os.path.join(temp_dir, "status.json")
            task_id = NativeTaskRegistry.create("launcher-install")
            NativeTaskRegistry.update(
                task_id,
                installation_id="installation-1",
                status_file=status_file,
            )
            self.assertTrue(
                NativeTaskRegistry.has_pending_installation("installation-1")
            )
            with open(status_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "status": "done",
                    "success": True,
                    "phase": "finished",
                    "progress_percent": 100,
                }, handle)
            task = NativeTaskRegistry.get(task_id)
            self.assertEqual(task["phase"], "finished")
            self.assertEqual(task["progress_percent"], 100)
            self.assertFalse(
                NativeTaskRegistry.has_pending_installation("installation-1")
            )


if __name__ == "__main__":
    unittest.main()
