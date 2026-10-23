#!/usr/bin/env python3
"""Skip published versions; API/authentication failures must fail the build."""
import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('tag')
args = parser.parse_args()
if not re.fullmatch(r'(?:php|mariadb)-\d+\.\d+\.\d+-\d{4}', args.tag):
    raise ValueError('Invalid release tag')
repo = os.environ['GITHUB_REPOSITORY']
token = os.environ['GH_TOKEN']
request = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/tags/{args.tag}',
                                 headers={'Authorization': f'Bearer {token}',
                                          'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'synology-cedarview-builder'})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release['draft']:
        raise RuntimeError(f'Unfinished draft {args.tag}; inspect it before retrying')
    if not release['assets'] or any(item['state'] != 'uploaded' for item in release['assets']):
        raise RuntimeError('Published release has missing or incomplete assets')
    build = False
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
    build = True
print(f'{args.tag}: ' + ('build required' if build else 'already published; skipped'))
with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
    output.write(f'build={str(build).lower()}\n')
