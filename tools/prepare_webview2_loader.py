"Prepare the WebView2 loader."
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile

SDK_VERSION='1.0.3240.44'
SDK_SHA256='8a8841b6d78c40010d7340287fd55e1355c717568aca7abcc3a14a86280babf7'
SDK_URL=f'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{SDK_VERSION}/microsoft.web.webview2.{SDK_VERSION}.nupkg'
ROOT=Path(__file__).resolve().parents[1]


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_signature(path):
    import os
    env=dict(os.environ,IDV_LOADER_SIGNATURE_PATH=str(path))
    command="$s=Get-AuthenticodeSignature -LiteralPath $env:IDV_LOADER_SIGNATURE_PATH; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {exit 1}"
    subprocess.run(['powershell.exe','-NoProfile','-Command',command],env=env,check=True,capture_output=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--arch',choices=('x64','arm64'),default='x64')
    p.add_argument('--sdk-package',type=Path)
    p.add_argument('--output-dir',type=Path,default=ROOT/'src/native/webview2')
    a=p.parse_args()
    build=ROOT/'build/webview2';build.mkdir(parents=True,exist_ok=True)
    package=a.sdk_package or build/f'Microsoft.Web.WebView2.{SDK_VERSION}.nupkg'
    if not package.exists():
        download=package.with_suffix('.download')
        try:
            with urllib.request.urlopen(SDK_URL,timeout=60) as source,download.open('wb') as out:shutil.copyfileobj(source,out)
            if digest(download)!=SDK_SHA256:raise ValueError('WebView2 SDK SHA-256 mismatch')
            download.replace(package)
        finally:download.unlink(missing_ok=True)
    if digest(package)!=SDK_SHA256:raise ValueError('WebView2 SDK SHA-256 mismatch')
    loader=build/'signed-loader'/a.arch/'WebView2Loader.dll';loader.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        loader.write_bytes(archive.read(f'build/native/{a.arch}/WebView2Loader.dll'))
        data=loader.read_bytes();offset=struct.unpack_from('<I',data,0x3c)[0]
        if data[:2]!=b'MZ' or data[offset:offset+4]!=b'PE\0\0' or struct.unpack_from('<H',data,offset+4)[0]!={'x64':0x8664,'arm64':0xaa64}[a.arch]:
            raise ValueError('Loader architecture mismatch')
        if sys.platform=='win32':verify_signature(loader)
        output=a.output_dir.resolve();output.mkdir(parents=True,exist_ok=True)
        shutil.copy2(loader,output/loader.name)
        for name in ('LICENSE.txt','NOTICE.txt'):(output/('WebView2-'+name)).write_bytes(archive.read(name))
        for name in ('idv_webview2.dll','build-manifest.json','LLVM-LICENSE.txt','mingw-w64-LICENSE.txt','MinGW-w64-COPYING.txt','MinGW-w64-runtime-NOTICES.txt'):
            (output/name).unlink(missing_ok=True)
    manifest={'sdk_version':SDK_VERSION,'sdk_sha256':SDK_SHA256,'architecture':a.arch,
              'runtime_bundled':False,'custom_dll':False,'authenticode_verified':sys.platform=='win32',
              'loader_sha256':digest(output/loader.name)}
    (output/'loader-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    print(json.dumps(manifest,indent=2))

if __name__=='__main__':main()
