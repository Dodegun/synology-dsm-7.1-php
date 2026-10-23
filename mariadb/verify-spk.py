#!/usr/bin/env python3
"""Validate SPK structure and run its lifecycle as an unprivileged package account."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tarfile
import tempfile
import time


def verify(spk, output):
    account = pwd.getpwnam('mariadb')
    report = {'physical_nas_tested': False, 'package': 'MariaDB'}
    with tempfile.TemporaryDirectory(prefix='mariadb-spk-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        outer, target, var = root/'spk', root/'target', root/'var'
        outer.mkdir(); target.mkdir(); var.mkdir()
        with tarfile.open(spk) as archive:
            names = archive.getnames()
            required = {'INFO', 'package.tgz', 'conf/privilege'} | {
                'scripts/'+name for name in ('preinst', 'postinst', 'preupgrade', 'postupgrade',
                                            'preuninst', 'postuninst', 'start-stop-status', 'service.sh')}
            if len(names) != len(set(names)) or not required <= set(names):
                raise ValueError('Missing or duplicate SPK members')
            archive.extractall(outer, filter='data')
        info = (outer/'INFO').read_text()
        for line in ('package="MariaDB"', 'arch="cedarview"', 'os_min_ver="7.1-42661"', 'startable="yes"'):
            if line not in info.splitlines():
                raise ValueError('Incorrect INFO metadata: ' + line)
        with (outer/'package.tgz').open('rb') as stream:
            md5 = hashlib.file_digest(stream, 'md5').hexdigest()
        if f'checksum="{md5}"' not in info.splitlines():
            raise ValueError('SPK payload MD5 mismatch')
        privilege = json.loads((outer/'conf/privilege').read_text())
        if privilege['defaults']['run-as'] != 'package' or privilege['username'] != 'mariadb':
            raise ValueError('Incorrect package privilege')
        with tarfile.open(outer/'package.tgz', 'r:gz') as archive:
            archive.extractall(target, filter='data')
        for path in [target, var, *target.rglob('*')]:
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        env = dict(os.environ, SYNOPKG_PKGDEST=str(target), SYNOPKG_PKGVAR=str(var))
        def run(script, *arguments, check=True):
            return subprocess.run(['sh', str(outer/'scripts'/script), *arguments],
                env=env, user=account.pw_uid, group=account.pw_gid, extra_groups=[],
                text=True, capture_output=True, timeout=100, check=check)
        client = [str(target/'bin/mariadb'), '--no-defaults', f'--socket={var}/run/mariadb.sock',
                  '--user='+account.pw_name, '--default-character-set=utf8mb4', '--batch', '--skip-column-names']
        def sql(statement):
            return subprocess.check_output(client + ['--execute='+statement], env=env,
                user=account.pw_uid, group=account.pw_gid, extra_groups=[], text=True, encoding='utf-8', timeout=30)
        try:
            run('preinst'); run('postinst'); run('start-stop-status', 'start'); run('start-stop-status', 'status')
            value = sql("CREATE DATABASE spk_ci CHARACTER SET utf8mb4; USE spk_ci; CREATE TABLE t(v VARCHAR(40)) ENGINE=InnoDB; INSERT INTO t VALUES('한글😀'); SELECT v FROM t;")
            if value.strip() != '한글😀':
                raise RuntimeError('SPK SQL result mismatch')
            sql("USE spk_ci; START TRANSACTION; UPDATE t SET v='rollback'; ROLLBACK;")
            if sql('SELECT v FROM spk_ci.t').strip() != '한글😀':
                raise RuntimeError('SPK rollback failed')
            config_before = (var/'my.cnf').read_bytes()
            run('start-stop-status', 'stop')
            if run('start-stop-status', 'status', check=False).returncode != 3:
                raise RuntimeError('Stopped service is still reported as running')
            run('preupgrade'); run('preinst'); run('postinst'); run('postupgrade')
            run('start-stop-status', 'start')
            if (var/'my.cnf').read_bytes() != config_before or sql('SELECT v FROM spk_ci.t').strip() != '한글😀':
                raise RuntimeError('Upgrade did not preserve settings/data')
            run('start-stop-status', 'stop'); run('preuninst'); run('postuninst')
            if not (var/'data/spk_ci').is_dir():
                raise RuntimeError('Uninstall hook removed persistent data')
            marker = (var/'engine-series').read_text()
            (var/'engine-series').write_text('0.0\n')
            if run('preinst', check=False).returncode == 0:
                raise RuntimeError('Incompatible existing data was accepted')
            (var/'engine-series').write_text(marker)
            report.update(ok=True, unprivileged_service=True, start_stop=True, innodb_crud=True,
                          transaction_rollback=True, korean_emoji=True, upgrade_preserves_data=True,
                          uninstall_preserves_data=True, rejects_incompatible_data=True)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f'{error.cmd}: {error.stdout}\n{error.stderr}') from error
        finally:
            run('start-stop-status', 'stop', check=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spk', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    verify(args.spk, args.output)
