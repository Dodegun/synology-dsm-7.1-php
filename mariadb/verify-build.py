"""Audit ELF ABI and exercise an isolated InnoDB server with the DSM SDK loader."""
import argparse
import json
from pathlib import Path
import pwd
import os
import subprocess
import tempfile
import time
from elftools.elf.elffile import ELFFile
from config import VERSION, PREFIX, SOURCES

SDK = Path('/build/toolchain/x86_64-pc-linux-gnu/x86_64-pc-linux-gnu/sys-root')
LOADER = [str(SDK / 'lib/ld-linux-x86-64.so.2'), '--library-path',
          ':'.join(map(str, [SDK/'lib', SDK/'usr/lib', SDK.parent/'lib64']))]
ALLOWED = {'ld-linux-x86-64.so.2', 'libc.so.6', 'libm.so.6', 'libpthread.so.0',
           'libdl.so.2', 'librt.so.1', 'libcrypt.so.1', 'libresolv.so.2', 'libutil.so.1'}


def audit(stage):
    report = []
    for path in sorted(stage.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
            stream.seek(0)
            elf = ELFFile(stream)
            if elf.elfclass != 64 or elf['e_machine'] != 'EM_X86_64':
                raise RuntimeError(f'Unexpected ELF architecture: {path}')
            versions = []
            section = elf.get_section_by_name('.gnu.version_r')
            if section:
                for _, auxiliary in section.iter_versions():
                    versions.extend(item.name for item in auxiliary)
            for version in versions:
                if version.startswith('GLIBC_'):
                    if not version[6:].replace('.', '').isdigit() or tuple(map(int, version[6:].split('.'))) > (2, 26):
                        raise RuntimeError(f'Unsupported glibc requirement: {path}: {version}')
            dynamic = elf.get_section_by_name('.dynamic')
            needed = [tag.needed for tag in dynamic.iter_tags() if tag.entry.d_tag == 'DT_NEEDED'] if dynamic else []
            if set(needed) - ALLOWED:
                raise RuntimeError(f'Unbundled dependency: {path}: {set(needed) - ALLOWED}')
            report.append({'path': str(path.relative_to(stage)), 'needed': needed, 'versions': sorted(set(versions))})
    for required in ('bin/mariadbd', 'bin/mariadb'):
        if not any(item['path'] == required for item in report):
            raise RuntimeError('Missing required ELF: ' + required)
    return report


def smoke(stage):
    env = dict(os.environ, GCONV_PATH=str(SDK / 'usr/lib/gconv'))
    server = LOADER + [str(stage/'bin/mariadbd')]
    version = subprocess.check_output(server + ['--no-defaults', '--version'], text=True, env=env)
    if VERSION not in version:
        raise RuntimeError('Server version does not match the source lock')
    with tempfile.TemporaryDirectory(prefix='mariadb-smoke-', dir='/build') as directory:
        root = Path(directory)
        data, socket, errorlog = root/'data', root/'db.sock', root/'server.log'
        user = pwd.getpwuid(os.getuid()).pw_name
        init = subprocess.run(['bash', str(stage/'scripts/mariadb-install-db'), '--no-defaults',
                               f'--basedir={stage}', f'--datadir={data}', f'--user={user}',
                               '--auth-root-authentication-method=normal', '--skip-test-db'],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if init.returncode:
            raise RuntimeError('Database initialization failed: ' + init.stdout)
        process = subprocess.Popen(server + ['--no-defaults', f'--basedir={stage}', f'--datadir={data}',
                                   f'--socket={socket}', f'--pid-file={root}/db.pid', f'--log-error={errorlog}',
                                   f'--user={user}', '--skip-networking', '--innodb-buffer-pool-size=64M',
                                   '--max-connections=5', '--performance-schema=OFF'],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        client = LOADER + [str(stage/'bin/mariadb'), '--no-defaults', f'--socket={socket}',
                           '--user=root', '--default-character-set=utf8mb4', '--batch', '--skip-column-names']
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(errorlog.read_text(errors='replace'))
                if socket.exists():
                    ready = subprocess.run(client + ['--execute=SELECT 1'], capture_output=True, env=env)
                    if ready.returncode == 0:
                        break
                time.sleep(.2)
            else:
                raise RuntimeError('MariaDB smoke startup timed out')
            sql = '''CREATE DATABASE ci_test CHARACTER SET utf8mb4;
USE ci_test;
CREATE TABLE t (id INT PRIMARY KEY, value VARCHAR(80)) ENGINE=InnoDB;
INSERT INTO t VALUES (1,'한글😀');
START TRANSACTION; UPDATE t SET value='rollback' WHERE id=1; ROLLBACK;
SELECT value FROM t WHERE id=1;
PREPARE stmt FROM 'SELECT COUNT(*) FROM t WHERE id=?';
SET @id=1; EXECUTE stmt USING @id; DEALLOCATE PREPARE stmt;
DELETE FROM t WHERE id=1; SELECT COUNT(*) FROM t;
DROP DATABASE ci_test;'''
            result = subprocess.run(client, input=sql, capture_output=True, text=True, encoding='utf-8', env=env, check=True)
            if result.stdout.splitlines() != ['한글😀', '1', '0']:
                raise RuntimeError('SQL smoke result mismatch: ' + result.stdout)
            return {'server_version': version.strip(), 'innodb_crud': True, 'transaction_rollback': True,
                    'prepared_statement': True, 'korean_emoji': True, 'networking': 'disabled'}
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path, default=Path('/build/stage') / PREFIX.lstrip('/'))
    parser.add_argument('--output', type=Path, default=Path('/work/build/validation.json'))
    args = parser.parse_args()
    report = {'version': VERSION, 'target': 'DSM 7.1 cedarview', 'glibc_maximum_allowed': '2.26',
              'physical_nas_tested': False, 'sources': list(SOURCES.values()),
              'elf_files': audit(args.stage), 'smoke': smoke(args.stage), 'ok': True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key not in ('elf_files', 'sources')}, indent=2))
