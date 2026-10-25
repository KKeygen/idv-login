"""Exercise hotfix downloads over real TLS without importing the application."""

import contextlib
import importlib.util
import io
import py_compile
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from test_ssl_verification import local_tls_server


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
REAL_SESSION = requests.Session
OLD_SOURCE = b"VALUE = 1\n"
NEW_SOURCE = b"VALUE = 2\n"
ITEM = {"target_module": "tls_hotfix_subject", "target_commit": "123456789abcdef"}


class FakeGenv:
    """Keep hotfix persistence entirely inside this test's memory."""

    def __init__(self, root):
        self.values = {"SCRIPT_DIR": str(root / "src"), "FP_WORKDIR": str(root)}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, cached=False):
        self.values[key] = value


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HotfixTLSTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        (self.root / "src").mkdir()
        self.genv = FakeGenv(self.root)
        envmgr = types.ModuleType("envmgr")
        envmgr.genv = self.genv
        ssl_utils = load_module("_hotfix_test_ssl_utils", "ssl_utils.py")
        self.enterContext(mock.patch.dict(sys.modules, {"envmgr": envmgr, "ssl_utils": ssl_utils}))
        self.hotfix = load_module("_hotfix_test_module", "hotfixmgr.py")
        self.sessions = []
        self.enterContext(mock.patch.object(requests, "Session", self.new_session))
        self.addCleanup(self.close_sessions)

    def new_session(self):
        session = REAL_SESSION()
        session.trust_env = False
        self.sessions.append(session)
        return session

    def close_sessions(self):
        for session in self.sessions:
            session.close()

    @contextlib.contextmanager
    def independent_workdir(self):
        original_root, original_genv = self.root, self.genv
        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary)
            (self.root / "src").mkdir()
            self.genv = FakeGenv(self.root)
            try:
                with mock.patch.object(self.hotfix, "genv", self.genv):
                    yield
            finally:
                self.root, self.genv = original_root, original_genv

    def prepare_target(self, target_kind):
        source = self.root / "src" / "tls_hotfix_subject.py"
        source.write_bytes(OLD_SOURCE)
        if target_kind == "pyc":
            target = source.with_suffix(".pyc")
            py_compile.compile(str(source), cfile=str(target), doraise=True)
            source.unlink()
            return target
        return source

    @contextlib.contextmanager
    def darwin_mode(self, target_kind):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            sys, "frozen", target_kind == "overlay_py", create=True
        ):
            yield

    def tree_snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
            for path in self.root.rglob("*")
        }

    def assert_rejected_without_install(self, url, target_kind):
        target = self.prepare_target(target_kind)
        old_bytes = target.read_bytes()
        before = self.tree_snapshot()
        with self.darwin_mode(target_kind), mock.patch.object(
            self.hotfix, "_build_remote_source_info", return_value=(url, [])
        ):
            ok, message = self.hotfix._apply_one(ITEM)
        self.assertFalse(ok, message)
        self.assertIn("下载失败", message)
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertEqual(self.tree_snapshot(), before)
        self.assertEqual(self.genv.get("hotfix_records", {}), {})
        self.assertEqual(self.genv.get("hotfix_pending_validate", []), [])
        self.assertIsNone(self.genv.get("last_run_state"))

        # The caller must also leave the failed batch out of restart validation.
        cloud = types.SimpleNamespace(get_hotfixes=lambda: [ITEM])
        with self.darwin_mode(target_kind), mock.patch.object(
            self.hotfix, "_build_remote_source_info", return_value=(url, [])
        ), mock.patch.object(self.hotfix.time, "sleep"), mock.patch.object(
            self.hotfix, "restart_self"
        ) as restart, contextlib.redirect_stdout(io.StringIO()):
            self.hotfix.handle_if_needed(cloud)
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertEqual(self.tree_snapshot(), before)
        self.assertEqual(self.genv.get("hotfix_pending_validate", []), [])
        record = self.genv.get("hotfix_records")["tls_hotfix_subject@123456789abcdef"]
        self.assertEqual(record["status"], "skipped")
        restart.assert_not_called()
        self.assertIsNone(self.genv.get("last_run_state"))

    def test_invalid_certificates_cannot_replace_source_pyc_or_frozen_overlay(self):
        # Returning verify=False on Darwin would install the untrusted response.
        for certificate_kind in ("untrusted", "wrong_host", "expired"):
            for target_kind in ("py", "pyc", "overlay_py"):
                with self.subTest(certificate=certificate_kind, target=target_kind), self.independent_workdir():
                    with local_tls_server(certificate_kind) as (url, ca_path):
                        with mock.patch.object(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", ca_path):
                            self.assert_rejected_without_install(url, target_kind)

    def test_network_failure_does_not_create_hotfix_artifacts(self):
        # A bound, non-listening socket gives a local refusal without external I/O.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            url = f"https://127.0.0.1:{reserved.getsockname()[1]}/patch.py"
            for target_kind in ("py", "pyc", "overlay_py"):
                with self.subTest(target=target_kind), self.independent_workdir():
                    self.assert_rejected_without_install(url, target_kind)

    def test_download_tries_trusted_mirror_after_certificate_failure(self):
        # If verification is disabled, the first server's different body wins.
        with local_tls_server("untrusted", body=b"VALUE = 999\n") as (bad_url, _):
            with local_tls_server("trusted", body=NEW_SOURCE) as (good_url, ca_path):
                with mock.patch.object(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", ca_path):
                    with self.darwin_mode("py"):
                        ok, content, error = self.hotfix._download_text(bad_url, [good_url])
        self.assertTrue(ok, error)
        self.assertEqual(content, NEW_SOURCE)

    def test_trusted_tls_can_install_source_pyc_and_frozen_overlay(self):
        with local_tls_server("trusted", body=NEW_SOURCE) as (url, ca_path):
            with mock.patch.object(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", ca_path):
                for target_kind in ("py", "pyc", "overlay_py"):
                    with self.subTest(target=target_kind), self.independent_workdir():
                        target = self.prepare_target(target_kind)
                        old_bytes = target.read_bytes()
                        self.genv.values.pop("hotfix_records", None)
                        with self.darwin_mode(target_kind), mock.patch.object(
                            self.hotfix, "_build_remote_source_info", return_value=(url, [])
                        ):
                            ok, message = self.hotfix._apply_one(ITEM)
                        self.assertTrue(ok, message)
                        record = self.genv.get("hotfix_records")["tls_hotfix_subject@123456789abcdef"]
                        self.assertEqual(record["status"], "pending_validate")
                        self.assertEqual(record["target_kind"], target_kind)
                        installed = Path(record["target_path"])
                        namespace = {}
                        if target_kind == "pyc":
                            spec = importlib.util.spec_from_file_location("_installed_hotfix", installed)
                            namespace["VALUE"] = None
                            module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(module)
                            namespace = vars(module)
                        else:
                            self.assertEqual(installed.read_bytes(), NEW_SOURCE)
                            exec(compile(installed.read_bytes(), str(installed), "exec"), namespace)
                        self.assertEqual(namespace["VALUE"], 2)
                        if target_kind == "overlay_py":
                            self.assertEqual(target.read_bytes(), old_bytes)
                            self.assertEqual(record["backup_path"], "")
                        else:
                            self.assertEqual(Path(record["backup_path"]).read_bytes(), old_bytes)

if __name__ == "__main__":
    unittest.main()
