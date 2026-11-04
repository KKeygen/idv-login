"""Prepare and locally sign a frozen cloud configuration; never executes staging code."""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = 'assets/cloudRes.json'
DOMAIN = b'IDV-CLOUD-CONFIG-V1\x00'
HEX = re.compile(r'[0-9a-f]{40}\Z')


def git(repo, *args):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0')
    return subprocess.run(['git', '-C', str(repo), *args], env=env, check=True,
                          stdout=subprocess.PIPE).stdout


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def reject_number(value):
    raise ValueError(f'non-finite JSON number is not permitted: {value}')


def read_config(data):
    result = json.loads(data.decode('utf-8'), object_pairs_hook=unique_keys,
                        parse_constant=reject_number)
    if not isinstance(result, dict) or not isinstance(result.get('hotfix'), list):
        raise ValueError('cloud config must be an object containing a hotfix list')
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('non-finite JSON number is not permitted')
        if isinstance(value, dict):
            for child in value.values(): finite(child)
        elif isinstance(value, list):
            for child in value: finite(child)
    finite(result)
    return result


def pretty(config):
    return json.dumps(config, ensure_ascii=False, indent=2) + '\n'


def signing_bytes(config):
    unsigned = dict(config)
    unsigned.pop('_signature', None)
    return DOMAIN + json.dumps(unsigned, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False).encode('utf-8')


def path_for(module):
    if not isinstance(module, str) or not module:
        raise ValueError('empty hotfix module')
    parts = module.replace('/', '.').split('.')
    if not all(p.isidentifier() for p in parts):
        raise ValueError(f'invalid hotfix module: {module!r}')
    return 'src/' + '/'.join(parts) + '.py'


def safe_executable(value):
    p = Path(value)
    if not p.is_absolute() or not p.is_file():
        raise ValueError('trusted signer path must be an absolute existing file')
    return str(p.resolve())


def prepare(args):
    repo = Path(args.repo).resolve(strict=True)
    # The source of truth is resolved once; no staging script is imported or run.
    if args.fetch:
        git(repo, 'fetch', '--no-tags', 'origin', '+refs/heads/cloud-config-staging:refs/local-cloud-signer/staging')
        ref = 'refs/local-cloud-signer/staging'
    else:
        ref = args.commit
    if not ref:
        raise ValueError('supply --fetch or --commit')
    oid = git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip().lower()
    if not HEX.fullmatch(oid):
        raise ValueError('unexpected commit identifier')
    before = git(repo, 'show', oid + ':' + CONFIG)
    candidate = read_config(before)
    candidate.pop('_signature', None)
    base_data = Path(args.baseline).read_bytes() if args.baseline else None
    baseline = read_config(base_data) if base_data is not None else None
    hashes, indices, seen, sources = [], [], {}, {}
    for item in candidate['hotfix']:
        if not isinstance(item, dict):
            raise ValueError('hotfix entry must be an object')
        path = path_for(item.get('target_module'))
        commit = item.get('target_commit')
        if not isinstance(commit, str) or not HEX.fullmatch(commit.lower()):
            raise ValueError('hotfix target_commit must be a full 40-digit SHA')
        commit = commit.lower()
        key = (commit, path)
        if key not in seen:
            source = git(repo, 'show', commit + ':' + path)
            seen[key] = len(hashes)
            hashes.append(hashlib.sha256(source).hexdigest())
            sources[key] = source
        indices.append(seen[key])
    for item, i in zip(candidate['hotfix'], indices):
        item['source_sha256'] = hashes[i]
    revision = candidate.get('lastModified')
    if type(revision) is not int or revision <= 0:
        raise ValueError('candidate lastModified must be a positive integer')
    if baseline is not None:
        old_revision = baseline.get('lastModified')
        if type(old_revision) is not int or old_revision <= 0:
            raise ValueError('baseline lastModified must be a positive integer')
        unsigned_baseline = dict(baseline)
        unsigned_baseline.pop('_signature', None)
        if candidate != unsigned_baseline and revision <= old_revision:
            raise ValueError('changed candidate lastModified must exceed baseline')
    output = pretty(candidate).encode('utf-8')
    # Treat embedded HTML as inert data; never render it in the signing tool.
    pages = {}
    for key in ('login_base64_page', 'login_base64_page_fever'):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            try:
                pages[key] = base64.b64decode(value, validate=True).decode('utf-8')
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f'invalid embedded page: {key}') from exc
    out = Path(args.output).resolve()
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(out, 0o700)
    (out / 'cloudRes.unsigned.json').write_bytes(output)
    state = {'staging_commit': oid, 'unsigned_file_sha256': hashlib.sha256(output).hexdigest(),
             'signing_sha256': hashlib.sha256(signing_bytes(candidate)).hexdigest(),
             'lastModified': revision,
             'hotfix_entries': len(indices), 'unique_files': len(hashes),
             'baseline_sha256': hashlib.sha256(base_data).hexdigest() if base_data else None}
    (out / 'candidate.json').write_text(pretty(state), encoding='utf-8')
    source_dir = out / 'hotfix-sources'
    source_dir.mkdir(mode=0o700)
    report = ['All source files below are inert text for manual review.\n',
              'The baseline is not authenticated by this tool.\n\n']
    for index, ((commit, path), source) in enumerate(sources.items(), 1):
        filename = f'{index:03d}-{commit[:12]}.txt'
        (source_dir / filename).write_bytes(source)
        report.append(f'{filename}  {path}  commit={commit}  '
                      f'sha256={hashlib.sha256(source).hexdigest()}\n')
    (out / 'hotfix-sources.txt').write_text(''.join(report), encoding='utf-8')
    # The supplied baseline is comparison data, not a trusted attestation.
    if baseline is not None:
        diff = difflib.unified_diff(pretty(baseline).splitlines(True), pretty(candidate).splitlines(True),
                                    fromfile='baseline (not automatically authenticated)', tofile='candidate')
        (out / 'review.diff').write_text(''.join(diff), encoding='utf-8')
    else:
        (out / 'review.diff').write_text('No baseline supplied. Review the complete cloudRes.unsigned.json.\n', encoding='utf-8')
    # Export encoded pages as inert source text for review. Never execute/render them here.
    for key, page in pages.items():
        (out / (key + '.txt')).write_text(page, encoding='utf-8')
        old = baseline.get(key) if baseline is not None else None
        if isinstance(old, str) and old:
            try:
                decoded = base64.b64decode(old, validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                decoded = '[baseline page could not be decoded]'
            (out / ('baseline_' + key + '.txt')).write_text(decoded, encoding='utf-8')
    print(f'Candidate commit: {oid}\nSigning SHA-256: {state["signing_sha256"]}\n'
          f'lastModified: {revision}\n'
          f'Hotfix entries: {len(indices)}; distinct module/commit pairs: {len(hashes)}\n'
          f'Review files: {out}\nBaseline is for comparison only; verify it independently.')


def verify(openssl, pub, data, signature):
    with tempfile.TemporaryDirectory(prefix='cloud-sign-check-') as temp:
        folder = Path(temp)
        (folder / 'payload').write_bytes(data)
        try:
            raw = base64.b64decode(signature.strip(), validate=True)
        except ValueError as error:
            raise ValueError('signature is not valid base64') from error
        (folder / 'signature').write_bytes(raw)
        subprocess.run([openssl, 'dgst', '-sha256', '-verify', str(pub), '-signature',
                        str(folder / 'signature'), str(folder / 'payload')], check=True,
                       stdout=subprocess.DEVNULL)


def sign(args):
    folder = Path(args.candidate).resolve(strict=True)
    config_path = folder / 'cloudRes.unsigned.json'
    output_path = folder / 'cloudRes.json'
    if output_path.exists():
        raise ValueError('signed configuration already exists; refusing to overwrite it')
    data = config_path.read_bytes()
    config = read_config(data)
    if '_signature' in config:
        raise ValueError('unsigned candidate unexpectedly contains a signature')
    state = json.loads((folder / 'candidate.json').read_text(encoding='utf-8'), object_pairs_hook=unique_keys)
    digest = hashlib.sha256(signing_bytes(config)).hexdigest()
    if state.get('unsigned_file_sha256') != hashlib.sha256(data).hexdigest() or state.get('signing_sha256') != digest:
        raise ValueError('candidate changed since preparation; prepare and review again')
    if not sys.stdin.isatty():
        raise ValueError('interactive review confirmation is required')
    print(f'Review {folder / "review.diff"}, hotfix-sources.txt and embedded page text files before signing.\n'
          f'Candidate SHA-256: {digest}\n')
    if input('Type the full SHA-256 to confirm: ').strip().lower() != digest:
        raise ValueError('confirmation does not match candidate')
    signer = safe_executable(args.signer)
    pub = Path(args.public_key).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='cloud-sign-') as temp:
        path = Path(temp) / 'cloudRes.json'
        path.write_bytes(signing_bytes(config))
        sig = Path(temp) / 'cloudRes.json.sig'
        subprocess.run([signer, 'sign', str(path), str(sig)], check=True)
        signature = sig.read_bytes()
        verify('/usr/bin/openssl', pub, signing_bytes(config), signature)
        if config_path.read_bytes() != data:
            raise ValueError('candidate changed during signing; signature not saved')
        config['_signature'] = {'alg': 'ECDSA-P256-SHA256-DER', 'value': signature.decode('ascii').strip()}
        signed = pretty(config).encode('utf-8')
        parsed = read_config(signed)
        verify('/usr/bin/openssl', pub, signing_bytes(parsed),
               parsed['_signature']['value'].encode('ascii'))
        # Exclusive creation prevents accidentally replacing an older approval.
        with output_path.open('xb') as stream:
            stream.write(signed)
    check(argparse.Namespace(candidate=str(folder), public_key=str(pub)))
    print('Signed configuration is ready for manual publication.')


def check(args):
    folder = Path(args.candidate).resolve(strict=True)
    data = (folder / 'cloudRes.json').read_bytes()
    config = read_config(data)
    signature = config.get('_signature')
    if (not isinstance(signature, dict) or set(signature) != {'alg', 'value'}
            or signature.get('alg') != 'ECDSA-P256-SHA256-DER' or not isinstance(signature.get('value'), str)):
        raise ValueError('invalid signature field')
    state = json.loads((folder / 'candidate.json').read_text(encoding='utf-8'), object_pairs_hook=unique_keys)
    if hashlib.sha256(signing_bytes(config)).hexdigest() != state.get('signing_sha256'):
        raise ValueError('candidate bytes changed')
    verify('/usr/bin/openssl', Path(args.public_key).resolve(strict=True), signing_bytes(config),
           signature['value'].encode('ascii'))
    print('Signed configuration verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--repo', default=str(ROOT))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--fetch', action='store_true', help='fetch origin cloud-config-staging')
    source.add_argument('--commit', help='resolve an already-fetched candidate commit')
    p.add_argument('--baseline', help='local previously approved config for comparison')
    p.add_argument('--output', required=True, help='new output directory')
    p.set_defaults(func=prepare)
    p = commands.add_parser('sign')
    p.add_argument('candidate')
    p.add_argument('--signer', required=True, help='trusted compiled Secure Enclave signer')
    p.add_argument('--public-key', required=True, help='pinned public key PEM')
    p.set_defaults(func=sign)
    p = commands.add_parser('check')
    p.add_argument('candidate')
    p.add_argument('--public-key', required=True)
    p.set_defaults(func=check)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        args.func(args)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError) as exc:
        print(f'cloud signer: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
