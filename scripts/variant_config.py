"""Read the selected, version-locked PHP branch for build and validation tools."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex


def load_variant():
    series = os.environ.get('PHP_SERIES', '8.4')
    variants = json.loads((Path(__file__).resolve().parent.parent / 'versions.json').read_text())
    if series not in variants or not re.fullmatch(r'8\.[2-4]', series):
        raise ValueError(f'Unsupported PHP_SERIES: {series}')
    selected = dict(variants[series])
    version = selected['version']
    if not re.fullmatch(re.escape(series) + r'\.\d+', version):
        raise ValueError(f'Invalid PHP version: {version}')
    if selected['source'] != f'php-{version}.tar.xz':
        raise ValueError('Invalid locked PHP source archive')
    source_lock = json.loads((Path(__file__).resolve().parent.parent / 'sources.lock.json').read_text())
    selected['source_name'] = next(row['name'] for row in source_lock['sources'] if row['filename'] == selected['source'])
    if not re.fullmatch(r'php\d*', selected['source_name']):
        raise ValueError('Invalid locked PHP source name')
    suffix = series.replace('.', '')
    selected.update(series=series, suffix=suffix, tag='php' + suffix,
                    package='PHP' + series, target='/var/packages/PHP' + series + '/target')
    return selected


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--shell', action='store_true')
    args = parser.parse_args()
    selected = load_variant()
    if args.shell:
        values = {'PHP_SERIES': selected['series'], 'PHP_VERSION': selected['version'],
                  'PHP_SOURCE': selected['source_name'], 'PHP_TAG': selected['tag'],
                  'PHP_SUFFIX': selected['suffix'], 'PACKAGE': selected['package'],
                  'PREFIX': selected['target']}
        for name, value in values.items():
            print(f'export {name}={shlex.quote(value)}')
    else:
        print(json.dumps(selected, indent=2))
