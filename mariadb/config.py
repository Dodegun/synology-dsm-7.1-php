"""Read MariaDB build settings exclusively from the source lock."""
import json
from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / 'sources.lock.json').read_text())
SOURCES = {item['name']: item for item in LOCK['sources']}
VERSION = SOURCES['mariadb']['version']
if not re.fullmatch(r'\d+\.\d+\.\d+', VERSION):
    raise ValueError('Invalid MariaDB version')
PREFIX = f'/opt/mariadb-{VERSION}'
ARCHIVE = f'mariadb-{VERSION}-cedarview-0001.tar.gz'
TAG = f'mariadb-{VERSION}-0001'

if __name__ == '__main__':
    for name, value in {'MARIADB_VERSION': VERSION, 'PREFIX': PREFIX,
                        'ARCHIVE': ARCHIVE, 'RELEASE_TAG': TAG}.items():
        print(f'export {name}={shlex.quote(value)}')
