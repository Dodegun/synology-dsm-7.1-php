"""Copy upstream license texts and locked source provenance into the runtime stage."""
from pathlib import Path
import shutil
import sys


stage = Path(sys.argv[1]).resolve(strict=True)
sources = Path('/build/src')
destination = stage / 'share/licenses'
for component in ('php', 'apcu', 'openssl', 'curl', 'libxml2', 'libzip', 'oniguruma', 'sqlite3', 'zlib'):
    root = sources / component
    names = ('LICENSE', 'LICENSE.txt', 'COPYING', 'Copyright', 'NOTICE')
    for source in root.rglob('*') if component == 'php' else root.iterdir():
        if source.is_file() and source.name in names:
            target = destination / component / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if component == 'php':
        shutil.copyfile(root / 'README.REDIST.BINS', destination / component / 'README.REDIST.BINS')
    elif component == 'sqlite3':
        # The public header's opening comment contains SQLite's copyright disclaimer.
        text = (root / 'sqlite3.h').read_text().split('*/', 1)[0] + '*/\n'
        target = destination / component / 'PUBLIC-DOMAIN.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
