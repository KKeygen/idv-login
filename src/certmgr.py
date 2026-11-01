# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <https://www.gnu.org/licenses/>.
 """

import subprocess
import sys
import datetime
import os  # Added import
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from typing import List
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    NoEncryption,
)
from logutil import setup_logger


class certmgr:
    def __init__(self) -> None:
        self.logger = setup_logger()
        pass

    def is_certificate_expired(self, cert_path: str) -> bool:
        if not os.path.exists(cert_path):
            self.logger.info(f"证书文件 {cert_path} 不存在，将进行创建。")
            return False  # Not expired because it will be (re)created
        try:
            with open(cert_path, "rb") as f:
                cert_data = f.read()
            cert = x509.load_pem_x509_certificate(cert_data)
            # Use timezone-aware datetime for comparison
            if datetime.datetime.now(datetime.timezone.utc) > cert.not_valid_after_utc:
                self.logger.warning(f"证书 {cert_path} 已于 {cert.not_valid_after_utc} 过期。")
                return True
            else:
                self.logger.debug(f"证书 {cert_path} 有效期至 {cert.not_valid_after_utc}。")
                return False
        except Exception as e:
            self.logger.error(f"检查证书 {cert_path} 有效期失败: {e}", exc_info=True)
            return True  # Treat as expired if an error occurs during check

    def generate_private_key(self, bits: int):
        return rsa.generate_private_key(public_exponent=65537, key_size=bits)

    def generate_ca(self, privatekey):
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
                x509.NameAttribute(NameOID.LOCALITY_NAME, "Los Angles"),
                x509.NameAttribute(
                    NameOID.ORGANIZATION_NAME, "Netease Login Helper CA"
                ),
                x509.NameAttribute(
                    NameOID.ORGANIZATIONAL_UNIT_NAME, "Netease Login Helper CA"
                ),
                x509.NameAttribute(NameOID.COMMON_NAME, "Netease Login Helper CA"),
            ]
        )
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(privatekey.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3))
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=360)
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .sign(privatekey, hashes.SHA256())
        )

    def generate_cert(self, hostnames: List[str], privatekey, ca_cert, ca_key):
        # generate the CSR for multiple domains
        tmp_names = [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Los Angeles"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Netease Login Helper Web"),
            x509.NameAttribute(
                NameOID.ORGANIZATIONAL_UNIT_NAME, "Netease Login Helper Web"
            ),
        ]
        tmp_names += [x509.NameAttribute(NameOID.COMMON_NAME, i) for i in hostnames]

        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name(tmp_names))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(i) for i in hostnames]),
                critical=False,
            )
            .sign(privatekey, hashes.SHA256())
        )

        # send the csr to CA
        return (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=3))#avoid using UTC
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=198)#for chrome
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(i) for i in hostnames]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

    def is_root_installed(self, cert_path):
        if sys.platform == "win32":
            with open(cert_path, "rb") as source:
                cert = x509.load_pem_x509_certificate(source.read())
            command = ['certutil', '-store', 'Root', cert.fingerprint(hashes.SHA1()).hex()]
        elif sys.platform == "darwin":
            command = ['security', 'verify-cert', '-c', cert_path, '-p', 'basic', '-l', '-L']
        elif sys.platform.startswith("linux"):
            command = ['openssl', 'verify', '-purpose', 'sslserver', cert_path]
        else:
            raise NotImplementedError("当前系统不支持CA证书安装检查")
        env = os.environ.copy()
        env.pop('SSL_CERT_FILE', None)
        env.pop('SSL_CERT_DIR', None)
        result = subprocess.run(command, capture_output=True, timeout=15, env=env)
        if result.returncode == 0:
            return True
        if sys.platform == "win32" and result.returncode & 0xffffffff == 0x80092004:
            return False
        if sys.platform == "darwin" and result.returncode == 1 and b'CSSMERR_TP_NOT_TRUSTED' in result.stderr:
            return False
        if sys.platform.startswith("linux") and result.returncode == 2 and b'error 18 at 0 depth lookup:' in result.stderr:
            return False
        raise subprocess.CalledProcessError(result.returncode, command)

    def ensure_root_installed(self, cert_path):
        try:
            if self.is_root_installed(cert_path):
                return True
            return self.import_to_root(cert_path) and self.is_root_installed(cert_path)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.logger.error("检查CA证书安装状态失败。", exc_info=True)
            return False

    def import_to_root(self, cert_path):
        try:
            if sys.platform == "win32":
                subprocess.check_call(
                    ['certutil', '-addstore', 'Root', cert_path]
                )
            elif sys.platform == "darwin":
                # macOS
                subprocess.check_call(
                    ['sudo', 'security', 'add-trusted-cert', '-d', '-r', 'trustRoot', '-k', '/Library/Keychains/System.keychain', cert_path]
                )
            elif sys.platform.startswith("linux"):
                # Linux
                if os.path.exists("/etc/arch-release"):
                    # Arch Linux
                    self.logger.info("Detected OS Arch Linux")
                    print(f"Copying certificate {cert_path} to /usr/local/share/ca-certificates/")
                    subprocess.check_call(
                        ['sudo', 'cp', cert_path, '/etc//ca-certificates/trust-source/anchors/']
                    )
                    subprocess.check_call(
                        ['sudo', 'trust', 'extract-compat']
                    )
                else:
                    destination = os.path.join('/usr/local/share/ca-certificates/',
                                               os.path.splitext(os.path.basename(cert_path))[0] + '.crt')
                    subprocess.check_call(
                        ['sudo', 'cp', cert_path, destination]
                    )
                    subprocess.check_call(
                        ['sudo', 'update-ca-certificates']
                    )
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error("导入CA证书失败。请关闭杀毒软件后重试。报错信息：", exc_info=True)
            return False

    def export_key(self, fn, key):
        try:
            from secure_write import write_file_restricted
            write_file_restricted(fn, key.private_bytes(
                Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
            ))
        except Exception as e:
            self.logger.error(str(e))
            self.logger.error("导出私钥失败！")
            sys.exit()

    def export_cert(self, fn, cert):
        try:
            with open(fn, "wb") as f:
                f.write(cert.public_bytes(Encoding.PEM))
        except Exception as e:
            self.logger.error(str(e))
            self.logger.error("导出证书失败！")
            sys.exit()
