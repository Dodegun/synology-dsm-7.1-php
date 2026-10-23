#!/usr/bin/env bash
set -Eeuo pipefail
eval "$(python3 /work/config.py)"
STAGE=/build/stage$PREFIX
mkdir -p /work/dist "$STAGE/share/build-info"
cp /work/sources.lock.json "$STAGE/share/build-info/sources.lock.json"
mkdir -p "$STAGE/share/licenses/openssl" "$STAGE/share/licenses/ncurses"
cp /build/openssl/LICENSE.txt "$STAGE/share/licenses/openssl/"
cp /build/ncurses/COPYING "$STAGE/share/licenses/ncurses/"
items=()
for item in bin lib lib64 share scripts support-files COPYING; do
    if [[ -e "$STAGE/$item" ]]; then items+=("$item"); fi
done
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --exclude='*.a' --exclude='share/man' --exclude='share/doc' \
    -czf "/work/dist/$ARCHIVE" -C "$STAGE" "${items[@]}"
(cd /work/dist && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")
mkdir -p /build/final-runtime
tar -xf "/work/dist/$ARCHIVE" -C /build/final-runtime
python3 /work/verify-build.py --stage /build/final-runtime --output /work/build/final-validation.json
python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
from config import ARCHIVE, VERSION, SOURCES, TAG
archive = Path('/work/dist') / ARCHIVE
with archive.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
report = {
    'version': VERSION, 'release_tag': TAG, 'architecture': 'cedarview', 'dsm': '7.1',
    'glibc': '2.26', 'compiler': 'Synology GCC 8.5.0', 'cpu': '-march=bonnell -mtune=bonnell',
    'generated_parser_optimization': '-O1', 'other_release_optimization': '-O3',
    'format': 'standalone native runtime tar.gz', 'physical_nas_tested': False,
    'build_commit': os.environ.get('BUILD_COMMIT'), 'archive': ARCHIVE, 'sha256': digest,
    'sources': list(SOURCES.values()),
    'validation': json.loads(Path('/work/build/final-validation.json').read_text())['smoke']
}
Path(str(archive) + '.manifest.json').write_text(json.dumps(report, indent=2) + '\n')
print(ARCHIVE, digest)
PY
