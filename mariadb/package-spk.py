#!/usr/bin/env python3
"""Wrap a verified native MariaDB runtime in a DSM 7.1 service package."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parent


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def add_bytes(archive, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    info.uid = info.gid = 0
    archive.addfile(info, io.BytesIO(data))


def build(runtime, output):
    manifest = json.loads(Path(str(runtime) + '.manifest.json').read_text())
    version = manifest['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid runtime version')
    if digest(runtime) != manifest['sha256'] or manifest['architecture'] != 'cedarview':
        raise ValueError('Runtime digest or architecture mismatch')
    series = '.'.join(version.split('.')[:2])
    output.mkdir(parents=True, exist_ok=True)
    spk = output / f'MariaDB-cedarview-{version}-0001.spk'
    with tarfile.open(runtime, 'r:gz') as archive:
        names = set(archive.getnames())
        required = {'bin/mariadbd', 'bin/mariadb', 'bin/mariadb-admin', 'scripts/mariadb-install-db'}
        if not required <= names:
            raise ValueError('Missing native runtime executables')
        extractsize = sum(item.size for item in archive.getmembers()) // 1024 + 1
    info = f'''package="MariaDB"
version="{version}-0001"
os_min_ver="7.1-42661"
arch="cedarview"
maintainer="Dodegun"
displayname="MariaDB {series}"
description="MariaDB {version} / DSM 7.1 cedarview"
thirdparty="yes"
startable="yes"
install_type="volume"
install_on_cold_storage="yes"
support_center="no"
checksum="{digest(runtime, 'md5')}"
extractsize={extractsize}
'''
    helper = (ROOT/'spk/service.sh').read_text().replace('@VERSION@', version).replace('@SERIES@', series)
    include = '#!/bin/sh\nset -eu\n. "$(dirname "$0")/service.sh"\n'
    scripts = {
        'preinst': include + 'check_data\n',
        'postinst': include + 'prepare\n',
        'preupgrade': include + 'check_data\n',
        'postupgrade': include + 'prepare\n',
        'preuninst': '#!/bin/sh\nexit 0\n',
        'postuninst': '#!/bin/sh\nexit 0\n',
        'start-stop-status': include + '''case "${1:-}" in
start) start_service ;;
stop) stop_service ;;
status) if running; then exit 0; else exit 3; fi ;;
prestart) check_data ;;
prestop) exit 0 ;;
restart) stop_service; start_service ;;
*) exit 64 ;;
esac
''',
        'service.sh': helper,
    }
    with tarfile.open(spk, 'w', format=tarfile.USTAR_FORMAT) as archive:
        add_bytes(archive, 'INFO', info.encode())
        for name in ('conf', 'scripts'):
            directory = tarfile.TarInfo(name)
            directory.type, directory.mode, directory.mtime = tarfile.DIRTYPE, 0o755, 0
            archive.addfile(directory)
        payload = tarfile.TarInfo('package.tgz')
        payload.size, payload.mode, payload.mtime = runtime.stat().st_size, 0o644, 0
        with runtime.open('rb') as stream:
            archive.addfile(payload, stream)
        privilege = {'defaults': {'run-as': 'package'}, 'username': 'mariadb', 'groupname': 'mariadb'}
        add_bytes(archive, 'conf/privilege', (json.dumps(privilege, indent=2)+'\n').encode())
        for name, script in scripts.items():
            add_bytes(archive, 'scripts/' + name, script.encode(), 0o755)
    report = {'package': 'MariaDB', 'version': version+'-0001', 'architecture': 'cedarview',
              'minimum_dsm': '7.1-42661', 'format': 'unsigned-synology-spk',
              'spk_sha256': digest(spk), 'runtime_sha256': manifest['sha256'],
              'runtime_build_commit': manifest['build_commit'], 'build_commit': os.environ.get('GITHUB_SHA'),
              'physical_nas_tested': False, 'port': 3308, 'bind_address': '127.0.0.1'}
    Path(str(spk)+'.sha256').write_text(f'{report["spk_sha256"]}  {spk.name}\n')
    Path(str(spk)+'.manifest.json').write_text(json.dumps(report, indent=2)+'\n')
    print(spk)
    return spk


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build(args.runtime, args.output)
