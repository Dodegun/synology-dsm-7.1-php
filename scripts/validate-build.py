"""Audit staged ELF ABI and run CLI/FPM with the DSM7.1 SDK glibc loader.

This is a Linux-container runtime check, not a run on the physical NAS.
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import threading
import time

from elftools.elf.elffile import ELFFile
from variant_config import load_variant

VARIANT = load_variant()
PREFIX = Path(VARIANT['target'])
SDK = Path('/build/toolchain/x86_64-pc-linux-gnu/x86_64-pc-linux-gnu/sys-root')
TOOL_LIB = SDK.parent / 'lib64'


@contextlib.contextmanager
def local_https(root):
    cert, key = root / 'tls-cert.pem', root / 'tls-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
                    '-days', '1', '-keyout', str(key), '-out', str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'php84-local-tls-ok'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield {'PHP_SMOKE_TLS_URL': f'https://127.0.0.1:{server.server_port}/',
               'PHP_SMOKE_TLS_CA': str(cert)}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def record(kind, request, content=b''):
    pad = (-len(content)) % 8
    return struct.pack('!BBHHBB', 1, kind, request, len(content), pad, 0) + content + b'\0' * pad


def param(name, value):
    def length(n):
        return bytes([n]) if n < 128 else struct.pack('!I', n | 0x80000000)
    k, v = name.encode(), value.encode()
    return length(len(k)) + length(len(v)) + k + v


def fastcgi(port, script):
    values = {'SCRIPT_FILENAME': str(script), 'SCRIPT_NAME': '/runtime-smoke.php',
              'REQUEST_METHOD': 'GET', 'SERVER_PROTOCOL': 'HTTP/1.1',
              'SERVER_SOFTWARE': 'build-validation', 'REMOTE_ADDR': '127.0.0.1',
              'SERVER_ADDR': '127.0.0.1', 'SERVER_PORT': str(port), 'CONTENT_LENGTH': '0'}
    data = record(1, 1, struct.pack('!HB5s', 1, 0, b'\0'*5))
    data += record(4, 1, b''.join(param(k, v) for k, v in values.items()))
    data += record(4, 1) + record(5, 1)
    stdout, stderr = bytearray(), bytearray()
    with socket.create_connection(('127.0.0.1', port), timeout=20) as conn:
        conn.sendall(data)
        def read_exact(n):
            out = bytearray()
            while len(out) < n:
                chunk = conn.recv(n - len(out))
                if not chunk:
                    raise RuntimeError('Truncated FastCGI response')
                out.extend(chunk)
            return bytes(out)
        while True:
            _, kind, _, size, padding, _ = struct.unpack('!BBHHBB', read_exact(8))
            body = read_exact(size)
            if padding:
                read_exact(padding)
            if kind == 6:
                stdout.extend(body)
            elif kind == 7:
                stderr.extend(body)
            elif kind == 3:
                if struct.unpack('!I', body[:4])[0] != 0:
                    raise RuntimeError(f'FastCGI app error: {bytes(stdout)!r}, {bytes(stderr)!r}')
                break
    if stderr:
        raise RuntimeError(f'FastCGI stderr: {stderr.decode(errors="replace")}')
    return json.loads(bytes(stdout).split(b'\r\n\r\n', 1)[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path, default=Path('/build/stage') / str(PREFIX).lstrip('/'))
    parser.add_argument('--output', type=Path, default=Path('/work/build') / VARIANT['series'] / 'validation.json')
    args = parser.parse_args()
    stage = args.stage.resolve()
    results = {'target': 'cedarview DSM7.1 glibc2.26', 'php_series': VARIANT['series'],
               'physical_nas_tested': False, 'elf': []}
    allowed = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'libresolv.so.2',
               'libutil.so.1', 'librt.so.1', 'libcrypt.so.1', 'libgcc_s.so.1', 'libstdc++.so.6'}
    for path in sorted(stage.rglob('*')):
        if not path.is_file() or path.read_bytes()[:4] != b'\x7fELF':
            continue
        with path.open('rb') as stream:
            elf = ELFFile(stream)
            if elf.header.e_machine != 'EM_X86_64':
                raise RuntimeError(f'Wrong architecture: {path}')
            versions = []
            version_table = elf.get_section_by_name('.gnu.version_r')
            if version_table:
                versions = [a.name for _, aux in version_table.iter_versions() for a in aux]
            for version in versions:
                if version.startswith('GLIBC_') and version != 'GLIBC_PRIVATE':
                    if tuple(int(x) for x in version[6:].split('.')) > (2, 26):
                        raise RuntimeError(f'Unsupported {version}: {path}')
                elif version == 'GLIBC_PRIVATE':
                    raise RuntimeError(f'Private glibc ABI: {path}')
            dynamic = elf.get_section_by_name('.dynamic')
            needed = [tag.needed for tag in dynamic.iter_tags() if tag.entry.d_tag == 'DT_NEEDED'] if dynamic else []
            paths = [str(getattr(tag, 'rpath', getattr(tag, 'runpath', '')))
                     for tag in dynamic.iter_tags() if tag.entry.d_tag in {'DT_RPATH', 'DT_RUNPATH'}] if dynamic else []
            if any('/build' in value or '/work' in value for value in paths):
                raise RuntimeError(f'Leaked build library path {paths}: {path}')
            unexpected = set(needed) - allowed
            if unexpected:
                raise RuntimeError(f'Unbundled dependency {unexpected}: {path}')
            results['elf'].append({'path': str(path.relative_to(stage)), 'needed': needed,
                                   'glibc': sorted(v for v in versions if v.startswith('GLIBC_')), 'rpath': paths})
    PREFIX.parent.mkdir(parents=True, exist_ok=True)
    if PREFIX.is_symlink():
        PREFIX.unlink()
    elif PREFIX.exists():
        raise RuntimeError('Validation prefix already exists as a real directory')
    PREFIX.symlink_to(stage, target_is_directory=True)
    testroot = Path('/build/verify') / VARIANT['series']
    testroot.mkdir(parents=True, exist_ok=True)
    loader = [str(SDK / 'lib/ld-linux-x86-64.so.2'), '--library-path',
              ':'.join(map(str, [SDK/'lib', SDK/'usr/lib', TOOL_LIB]))]
    env = dict(os.environ, PHP_INI_SCAN_DIR=str(stage / 'etc/conf.d'),
               GCONV_PATH=str(SDK / 'usr/lib/gconv'), PHP_EXPECTED_VERSION=VARIANT['version'])
    # These paths exist on DSM; create container-only counterparts for final ini validation.
    Path('/var/services/tmp').mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        env.update(stack.enter_context(local_https(testroot)))
        run_runtime(stage, loader, env, results, testroot)
    results['ok'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2))
    print(json.dumps({k:v for k,v in results.items() if k != 'elf'}, indent=2))


def run_runtime(stage, loader, env, results, testroot):
    cli = loader + [str(stage/'usr/local/bin'/VARIANT['tag'])]
    smoke = Path('/work/scripts/runtime-smoke.php')
    proc = subprocess.run(cli + ['-d', 'apc.enable_cli=1', str(smoke)], env=env, capture_output=True, text=True)
    if proc.returncode or proc.stderr.strip():
        raise RuntimeError(f'CLI smoke failed ({proc.returncode}): {proc.stdout}\n{proc.stderr}')
    results['cli'] = json.loads(proc.stdout)
    results['cgi_version'] = subprocess.check_output(loader + [str(stage/'usr/local/bin'/(VARIANT['tag'] + '-cgi')), '-v'], env=env, text=True).splitlines()[0]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = testroot/'fpm.conf'
    config.write_text(f'''[global]
daemonize=no
error_log={testroot}/fpm-error.log
[smoke]
listen=127.0.0.1:{port}
user=root
group=root
pm=static
pm.max_children=1
catch_workers_output=yes
clear_env=no
''')
    command = loader + [str(stage/'usr/local/bin'/(VARIANT['tag'] + '-fpm')), '-F', '-R', '-y', str(config)]
    if (stage / 'misc/php-fpm.ini').is_file():
        command += ['-c', str(stage / 'misc/php-fpm.ini')]
    with (testroot/'fpm-stdout.log').open('w') as out:
        fpm = subprocess.Popen(command, env=env, stdout=out, stderr=out)
        try:
            for _ in range(100):
                if fpm.poll() is not None:
                    raise RuntimeError((testroot/'fpm-stdout.log').read_text())
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('FPM did not open its loopback listener')
            results['fpm'] = fastcgi(port, smoke)
            results['fpm_second_request'] = fastcgi(port, smoke)
        finally:
            fpm.terminate()
            try:
                fpm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                fpm.kill()
                fpm.wait()


if __name__ == '__main__':
    main()
