"""Download and hash-check the locked native MariaDB build inputs."""
import hashlib
from pathlib import Path
import urllib.request
from config import SOURCES, ROOT

destination = ROOT / 'downloads'
destination.mkdir(exist_ok=True)
for row in SOURCES.values():
    if Path(row['filename']).name != row['filename'] or not row['url'].startswith('https://'):
        raise ValueError('Invalid input filename or URL')
    archive = destination / row['filename']
    if not archive.exists():
        partial = archive.with_suffix(archive.suffix + '.partial')
        print('Downloading', row['filename'], flush=True)
        with urllib.request.urlopen(row['url'], timeout=120) as response, partial.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        with partial.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != row['sha256']:
            raise RuntimeError('Downloaded SHA256 mismatch: ' + row['filename'])
        partial.replace(archive)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
            raise RuntimeError('SHA256 mismatch: ' + row['filename'])
    print('Verified', row['filename'], flush=True)
