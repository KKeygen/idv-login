"""Exercise HTTPS verification with disposable certificates and loopback servers."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import warnings

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import requests
from urllib3.exceptions import InsecureRequestWarning


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import ssl_utils  # noqa: E402


PLATFORMS = ("darwin", "win32", "linux")


def _certificate_authority(common_name, now):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=None, decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


@contextmanager
def local_tls_server(certificate_kind="trusted", body=b"VALUE = 2\n"):
    """Yield (HTTPS URL, trusted CA file) without changing the machine trust store."""
    if certificate_kind not in {"trusted", "untrusted", "wrong_host", "expired"}:
        raise ValueError(f"Unknown test certificate kind: {certificate_kind}")

    now = datetime.now(timezone.utc)
    trusted_key, trusted_ca = _certificate_authority("Local TLS test CA", now)
    issuer_key, issuer_ca = trusted_key, trusted_ca
    if certificate_kind == "untrusted":
        issuer_key, issuer_ca = _certificate_authority("Untrusted TLS test CA", now)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Loopback TLS test")])
    alternative_name = (
        x509.DNSName("wrong-host.invalid") if certificate_kind == "wrong_host"
        else x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    )
    expired = certificate_kind == "expired"
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2) if expired else now - timedelta(hours=1))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.SubjectAlternativeName([alternative_name]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False)
        .sign(issuer_key, hashes.SHA256())
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with tempfile.TemporaryDirectory(prefix="idv-tls-test-") as directory:
        directory = Path(directory)
        ca_path = directory / "ca.pem"
        cert_path = directory / "server.pem"
        key_path = directory / "server-key.pem"
        ca_path.write_bytes(trusted_ca.public_bytes(serialization.Encoding.PEM))
        cert_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = None
        try:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            yield f"https://127.0.0.1:{server.server_port}/", str(ca_path)
        finally:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("Local TLS test server did not stop")


@contextmanager
def _local_session(ca_path):
    with requests.Session() as session:
        session.trust_env = False
        with mock.patch.object(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", ca_path):
            yield session


def _load_channel_utils():
    """Avoid importing application state or another test's channelUtils stub."""
    spec = importlib.util.spec_from_file_location(
        "_tls_test_channel_utils", SRC_DIR / "channelHandler" / "channelUtils.py",
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"app_state": types.ModuleType("app_state")}):
        spec.loader.exec_module(module)
    return module


class SslVerificationTests(unittest.TestCase):
    def test_verification_is_literal_true_on_all_supported_platforms(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                self.assertIs(ssl_utils.should_verify_ssl(), True)

    def test_trusted_ca_and_matching_ip_succeed_on_all_platforms(self):
        with local_tls_server() as (url, ca_path), _local_session(ca_path) as session:
            for platform in PLATFORMS:
                with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                    response = session.get(url, verify=ssl_utils.should_verify_ssl(), timeout=3)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"VALUE = 2\n")

    def _assert_certificate_rejected(self, certificate_kind):
        with local_tls_server(certificate_kind) as (url, ca_path), _local_session(ca_path) as session:
            for platform in PLATFORMS:
                with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                    with self.assertRaises(requests.exceptions.SSLError):
                        session.get(url, verify=ssl_utils.should_verify_ssl(), timeout=3)

    def test_untrusted_ca_is_rejected_on_all_platforms(self):
        self._assert_certificate_rejected("untrusted")

    def test_wrong_hostname_is_rejected_on_all_platforms(self):
        self._assert_certificate_rejected("wrong_host")

    def test_expired_certificate_is_rejected_on_all_platforms(self):
        self._assert_certificate_rejected("expired")

    def test_closed_port_raises_connection_error_on_all_platforms(self):
        # Reserve the port without listening so it cannot be reused during the test.
        with socket.socket() as reserved, requests.Session() as session:
            reserved.bind(("127.0.0.1", 0))
            session.trust_env = False
            url = f"https://127.0.0.1:{reserved.getsockname()[1]}/"
            for platform in PLATFORMS:
                with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                    with self.assertRaises(requests.exceptions.ConnectionError) as error:
                        session.get(url, verify=ssl_utils.should_verify_ssl(), timeout=1)
                    self.assertNotIsInstance(error.exception, requests.exceptions.SSLError)


class PublicIpTlsTests(unittest.TestCase):
    def _request_public_ip(self, certificate_kind, expected_ip):
        channel_utils = _load_channel_utils()
        with local_tls_server(certificate_kind, body=b'{"ip":"203.0.113.7"}') as (url, ca_path):
            with _local_session(ca_path) as session:
                def get_from_loopback(request_url, **kwargs):
                    self.assertEqual(request_url, "https://who.nie.netease.com/")
                    kwargs.setdefault("timeout", 3)
                    return session.get(url, **kwargs)

                for platform in PLATFORMS:
                    with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                        with mock.patch.object(channel_utils.requests, "get", side_effect=get_from_loopback) as get:
                            with warnings.catch_warnings():
                                warnings.simplefilter("ignore", InsecureRequestWarning)
                                self.assertEqual(channel_utils._get_my_ip(), expected_ip)
                            get.assert_called_once()
                            self.assertIs(get.call_args.kwargs.get("verify"), True)

    def test_public_ip_uses_verified_https_on_all_platforms(self):
        self._request_public_ip("trusted", "203.0.113.7")

    def test_public_ip_untrusted_ca_returns_loopback_without_retry(self):
        self._request_public_ip("untrusted", "127.0.0.1")

    def test_public_ip_wrong_hostname_returns_loopback_without_retry(self):
        self._request_public_ip("wrong_host", "127.0.0.1")

    def test_public_ip_expired_certificate_returns_loopback_without_retry(self):
        self._request_public_ip("expired", "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
