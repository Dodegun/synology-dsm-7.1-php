"""Verify the produced SPK, then extract its runtime into a fresh container directory."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

from package_spk import Entry, MODULE_DIR, TARGET, manifest_entries, safe_name, validate_entries
from variant_config import load_variant

VARIANT = load_variant()


def read_archive(blob):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:*') as archive:
        for member in archive:
            name = safe_name(member.name.rstrip('/'))
            if name in result:
                raise ValueError(f'Duplicate archive member: {name}')
            if member.isfile():
                if member.size > 512 * 1024 * 1024:
                    raise ValueError(f'Oversized member: {name}')
                result[name] = Entry(name, 'file', member.mode, data=archive.extractfile(member).read())
            elif member.isdir():
                result[name] = Entry(name, 'directory', member.mode)
            elif member.issym():
                result[name] = Entry(name, 'symlink', member.mode, target=member.linkname)
            else:
                raise ValueError(f'Unsupported archive member: {name}')
    validate_entries(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spk', type=Path)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    raw = args.spk.read_bytes()
    report = json.loads(Path(str(args.spk) + '.manifest.json').read_text())
    sha = hashlib.sha256(raw).hexdigest()
    assert sha == report['spk_sha256']
    assert Path(str(args.spk) + '.sha256').read_text().split()[0] == sha
    outer = read_archive(raw)
    info = outer['INFO'].data.decode()
    for required in (f'package="{VARIANT["package"]}"', 'arch="cedarview"', 'os_min_ver="7.1-42661"', 'startable="no"'):
        assert required in info, required
    compressed = outer['package.tgz'].data
    assert hashlib.sha256(compressed).hexdigest() == report['package_tgz_sha256']
    assert hashlib.md5(compressed).hexdigest() == re.search(r'^checksum="([0-9a-f]+)"$', info, re.M)[1]
    assert manifest_entries(outer) == report['spk_members']
    payload = read_archive(compressed)
    assert manifest_entries(payload) == report['payload']
    for name, entry in outer.items():
        if name.startswith('scripts/') and entry.kind == 'file':
            assert entry.data.startswith(b'#!/bin/sh\n') and b'\r' not in entry.data, name
            assert entry.mode & 0o111, name
            subprocess.run(['sh', '-n'], input=entry.data, check=True)
    meta = json.loads(payload['misc/meta.json'].data)
    assert meta['id'] == VARIANT['metadata_id'] and meta['prefix'] == VARIANT['tag']
    for key in ('extension_list_path', 'default_settings', 'default_ini', 'cgi_path', 'fpm_path', 'cli_path'):
        assert meta[key].startswith(TARGET + '/'), key
        assert meta[key][len(TARGET) + 1:] in payload, (key, meta[key])
    modules = sorted(Path(name).stem for name in payload if name.startswith(MODULE_DIR + '/') and name.endswith('.so'))
    loads = []
    for name, entry in payload.items():
        if name.startswith('etc/conf.d/') and name.endswith('.ini'):
            loads += re.findall(r'^\s*(?:zend_extension|extension)\s*=\s*[\"\']?([\w]+)(?:\.so)?', entry.data.decode(), re.M)
    assert sorted(loads) == modules, (loads, modules)
    extension_list = json.loads(payload['misc/extension_list.json'].data)
    assert set(extension_list) == set(modules) - {'apcu', 'opcache', 'xdebug'}
    defaults = json.loads(payload['misc/default_settings.json'].data)
    assert defaults['extensions']['session.upload_progress.enabled'] in ('0', 0, False)
    assert defaults['core']['extension_dir'] == f'{TARGET}/{MODULE_DIR}'
    for ini in ('etc/php.ini', 'misc/php-fpm.ini'):
        text = payload[ini].data.decode()
        for key in ('curl.cainfo', 'openssl.cafile'):
            assert re.search(rf'^\s*{re.escape(key)}\s*=\s*{re.escape(TARGET)}/etc/cacert.pem\s*$', text, re.M), (ini, key)
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name, entry in sorted(payload.items(), key=lambda row: (row[0].count('/'), row[0])):
        path = destination / name
        if entry.kind == 'directory':
            path.mkdir(exist_ok=True)
            path.chmod(entry.mode)
        elif entry.kind == 'file':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(entry.data)
            path.chmod(entry.mode)
        else:
            path.symlink_to(entry.target)
    result = {'ok': True, 'sha256': sha, 'payload_entries': len(payload), 'modules': len(modules),
              'default_ini_entries': {k: len(v) for k, v in defaults.items()},
              'extracted_stage': str(destination), 'physical_nas_tested': False}
    output = Path('/work/build') / VARIANT['series'] / 'package-validation.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
