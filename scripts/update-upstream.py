#!/usr/bin/env python3
"""Resolve stable upstream releases and pin their published SHA256 in this build."""
import argparse
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MARIADB_API = 'https://downloads.mariadb.org/rest-api/mariadb/'


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'synology-cedarview-builder'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def version_key(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError(f'Invalid stable version: {version}')
    return tuple(map(int, version.split('.')))


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('Missing or invalid published SHA256')
    return value


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def refresh_php(series):
    versions_path = ROOT / 'versions.json'
    lock_path = ROOT / 'sources.lock.json'
    versions = json.loads(versions_path.read_text())
    lock = json.loads(lock_path.read_text())
    for branch in ([series] if series else sorted(versions)):
        old = versions[branch]
        release = get_json(f'https://www.php.net/releases/?json&version={branch}')
        version = release['version']
        if not re.fullmatch(re.escape(branch) + r'\.\d+', version):
            raise ValueError('PHP API returned another branch or a prerelease')
        if version_key(version) < version_key(old['version']):
            raise ValueError('Refusing an upstream PHP version downgrade')
        filename = f'php-{version}.tar.xz'
        source = next(item for item in release['source'] if item['filename'] == filename)
        row = next(item for item in lock['sources'] if item['filename'] == old['source'])
        row.update(version=version, filename=filename,
                   url=f'https://www.php.net/distributions/{filename}',
                   sha256=checksum(source['sha256']))
        print(f'PHP {branch}: {old["version"]} -> {version}')
        old.update(version=version, source=filename)
    save(versions_path, versions)
    save(lock_path, lock)


def refresh_mariadb():
    lock_path = ROOT / 'mariadb/sources.lock.json'
    lock = json.loads(lock_path.read_text())
    row = next(item for item in lock['sources'] if item['name'] == 'mariadb')
    branches = get_json(MARIADB_API)['major_releases']
    stable = [item for item in branches if item['release_status'] == 'Stable']
    branch = max(stable, key=lambda item: tuple(map(int, item['release_id'].split('.'))))['release_id']
    releases = get_json(MARIADB_API + branch + '/')['releases']
    candidates = [release for version, release in releases.items()
                  if re.fullmatch(re.escape(branch) + r'\.\d+', version)
                  and not re.search(r'\b(alpha|beta|rc|preview)\b', release['release_name'], re.I)]
    release = max(candidates, key=lambda item: version_key(item['release_id']))
    version = release['release_id']
    if version_key(version) < version_key(row['version']):
        raise ValueError('Refusing an upstream MariaDB version downgrade')
    filename = f'mariadb-{version}.tar.gz'
    source = next(item for item in release['files']
                  if item['file_name'] == filename and item['os'] == 'Source')
    print(f'MariaDB: {row["version"]} -> {version} (latest stable; excludes RC/preview)')
    row.update(version=version, filename=filename,
               url=f'https://archive.mariadb.org/mariadb-{version}/source/{filename}',
               sha256=checksum(source['checksum']['sha256sum']))
    save(lock_path, lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=['php', 'mariadb'])
    parser.add_argument('--php-series', choices=['8.2', '8.3', '8.4'])
    args = parser.parse_args()
    if args.component == 'php':
        refresh_php(args.php_series)
    else:
        refresh_mariadb()
