import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# The formatters are pure; keep their tests independent from runtime downloads.
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("zmq", types.ModuleType("zmq"))

from download_binary import (
    MSG_DISK_FULL,
    MSG_SETUP_ERROR,
    ProgressReporter,
    allocate_ipc_ports,
    _progress_view,
    _progress_event,
    _render_core_error,
    _render_progress_line,
)


class DownloadProgressTests(unittest.TestCase):
    def test_each_download_can_request_a_distinct_port_pair(self):
        heartbeat_port, progress_port = allocate_ipc_ports()
        self.assertNotEqual(heartbeat_port, progress_port)
        self.assertGreater(heartbeat_port, 0)
        self.assertGreater(progress_port, 0)

    def test_download_phase_uses_download_metrics(self):
        data = {
            "StateFlags": 4,
            "ShowDownloadPercent": 0.25,
            "ShowDownloadRateStr": "8 MB/s",
            "ShowDownloadSize": 1024,
            "ShowBuildPercent": 0.9,
        }
        phase, percent, rate, total = _progress_view(data)
        self.assertEqual((phase, percent, rate, total), ("下载文件", 0.25, "8 MB/s", 1024))
        line = _render_progress_line(data)
        self.assertIn("25.00%", line)
        self.assertIn("256.00 B / 1.00 KB", line)

    def test_verify_phase_does_not_reuse_build_percent(self):
        line = _render_progress_line({
            "StateFlags": 11,
            "ShowVerifyPercent": 0.6,
            "ShowBuildPercent": 1,
        })
        self.assertIn("校验文件", line)
        self.assertIn("60.00%", line)

    def test_progress_snapshots_are_throttled_to_one_percent(self):
        reporter = ProgressReporter(step_percent=1)
        base = {"StateFlags": 4, "ShowDownloadRateStr": "1 MB/s"}
        self.assertTrue(reporter.render_if_due({**base, "ShowDownloadPercent": 0.01}))
        self.assertEqual(
            reporter.render_if_due({**base, "ShowDownloadPercent": 0.015}), ""
        )
        self.assertTrue(reporter.render_if_due({**base, "ShowDownloadPercent": 0.02}))

    def test_narrow_terminal_uses_compact_non_wrapping_layout(self):
        line = _render_progress_line({
            "StateFlags": 4,
            "ShowDownloadPercent": 0.25,
            "ShowDownloadRateStr": "8 MB/s",
            "ShowDownloadSize": 1024,
        }, columns=60)
        self.assertNotIn("#", line)
        self.assertLessEqual(len(line), 60)

    def test_disk_full_message_reports_required_size(self):
        message = _render_core_error(MSG_DISK_FULL, b'{"NeedSpaceSize": 1073741824}')
        self.assertEqual(message, "磁盘空间不足，至少需要可用空间：1.00 GB")

    def test_setup_error_is_not_discarded(self):
        message = _render_core_error(MSG_SETUP_ERROR, b"invalid path")
        self.assertEqual(message, "下载初始化失败：invalid path")

    def test_finished_core_state_remains_pending_until_metadata_is_saved(self):
        event = _progress_event({"StateFlags": 8})
        self.assertEqual(event["status"], "pending")
        self.assertEqual(event["progress_percent"], 100)


if __name__ == "__main__":
    unittest.main()
