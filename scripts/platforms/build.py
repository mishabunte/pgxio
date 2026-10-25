#!/usr/bin/env python3
"""Build a matched native Nginx/module pair in an explicit staging prefix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
NGINX = '1.30.5'
RUST = '1.99.1'


def run(argv, **kw):
    print('+', ' '.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kw)


def fetch(url, destination):
    if not destination.exists():
        urllib.request.urlretrieve(url, destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prefix', type=Path, required=True)
    p.add_argument('--libpq', choices=('system', 'borrowed'), default='system')
    p.add_argument('--jobs', type=int, default=2)
    p.add_argument('--features', default='')
    p.add_argument('--toolchain-home', type=Path)
    p.add_argument('--diagnostic-allocations', action='store_true')
    p.add_argument('--client-only', action='store_true')
    args = p.parse_args()
    if platform.system() not in ('Linux','FreeBSD'):
        p.error('native builds require Linux or FreeBSD; use Docker on other hosts')
    if args.jobs < 1 or (args.diagnostic_allocations and args.libpq != 'borrowed'):
        p.error('positive jobs and borrowed libpq required for allocation diagnostics')
    prefix = args.prefix.resolve(); prefix.mkdir(parents=True, exist_ok=True)
    work = prefix / 'build'; work.mkdir(exist_ok=True)
    env = dict(os.environ)
    toolchain=args.toolchain_home
    if toolchain is None and Path('/opt/pgxio-toolchain/cargo/bin/rustup').exists(): toolchain=Path('/opt/pgxio-toolchain')
    if toolchain is not None:
        env.update(CARGO_HOME=str(toolchain/'cargo'), RUSTUP_HOME=str(toolchain/'rustup'))
    else:
        env.setdefault('CARGO_HOME',str(Path.home()/'.cargo'))
        env.setdefault('RUSTUP_HOME',str(Path.home()/'.rustup'))
    env['PATH'] = env['CARGO_HOME']+'/bin:'+env['PATH']
    env['CARGO_BUILD_JOBS'] = str(args.jobs)
    make = 'gmake' if platform.system() == 'FreeBSD' else 'make'
    if platform.system() == 'FreeBSD':
        env['CPPFLAGS'] = '-I/usr/local/include'
        env['LDFLAGS'] = '-L/usr/local/lib'
        clangs = sorted(Path('/usr/local').glob('llvm*/lib/libclang.so'))
        if clangs: env['LIBCLANG_PATH'] = str(clangs[-1].parent)
    metadata = dict(os=platform.platform(), arch=platform.machine(), libpq=args.libpq,
                    diagnostic_allocations=args.diagnostic_allocations, sources={})
    if args.libpq == 'borrowed':
        source = json.loads((ROOT/'patches/libpq/source.json').read_text())
        archive = work/'postgres.tar.gz'
        metadata['sources']['postgres'] = fetch(f"{source['repository'].replace('https://github.com/', 'https://codeload.github.com/')}/tar.gz/{source['ref']}", archive)
        postgres = work/('postgres-'+source['ref'])
        if not postgres.exists():
            with tarfile.open(archive) as tar: tar.extractall(work, filter='data')
        pq = prefix/'libpq'
        pe = dict(env)
        if args.diagnostic_allocations:
            # Only the diagnostic client has allocation seams, never the release library.
            for name in ('fe-exec.c', 'fe-misc.c'):
                f = postgres/'src/interfaces/libpq'/name
                text = f.read_text()
                marker = '#include "pgxio_test_alloc.h"'
                if marker not in text:
                    at = text.index('\n', text.index('#include "postgres_fe.h"'))+1
                    text = text[:at]+marker+'\n'+text[at:]
                    f.write_text(text)
            shutil.copy(ROOT/'patches/libpq/test_alloc.h', postgres/'src/interfaces/libpq/pgxio_test_alloc.h')
            f = postgres/'src/interfaces/libpq/fe-exec.c'
            text = f.read_text()
            if 'static long pgxio_test_allocations' not in text:
                f.write_text(text+'\n'+(ROOT/'patches/libpq/test_alloc.c').read_text())
            f = postgres/'src/interfaces/libpq/exports.txt'
            if 'libpq_test_fail_alloc' not in f.read_text():
                number=max(int(line.split()[1]) for line in f.read_text().splitlines() if line and not line.startswith('#'))+1
                with f.open('a') as stream: stream.write(f'libpq_test_fail_alloc {number}\n')
        run(['./configure', f'--prefix={pq}', '--with-ssl=openssl', '--without-icu', '--without-readline', '--without-zlib', '--without-gssapi', '--without-ldap', '--without-libcurl', '--without-lz4', '--without-zstd'], cwd=postgres, env=pe)
        run([make, '-C', 'src/backend', 'generated-headers'], cwd=postgres, env=pe)
        run([make, f'-j{args.jobs}', '-C', 'src/interfaces/libpq'], cwd=postgres, env=pe)
        run([make, '-C', 'src/interfaces/libpq', 'install'], cwd=postgres, env=pe)
        run([make, '-C', 'src/include', 'install'], cwd=postgres, env=pe)
        env.update(PQ_LIB_DIR=str(pq/'lib'), LD_LIBRARY_PATH=str(pq/'lib'),
                   RUSTFLAGS='-C link-arg=-Wl,-z,origin -C link-arg=-Wl,-rpath,$ORIGIN/../libpq/lib')
    else:
        pg = subprocess.check_output(['pg_config', '--version'], text=True).strip()
        if int(pg.split()[1].split('.')[0]) < 17: raise RuntimeError('libpq 17+ required')
    if args.client_only:
        (prefix/'build.json').write_text(json.dumps(metadata,indent=2))
        return
    archive = work/'nginx.tar.gz'
    metadata['sources']['nginx'] = fetch(f'https://nginx.org/download/nginx-{NGINX}.tar.gz', archive)
    nginx = work/('nginx-'+NGINX)
    if not nginx.exists():
        with tarfile.open(archive) as tar: tar.extractall(work, filter='data')
    options = [f'--prefix={prefix}', '--with-compat', '--with-http_ssl_module', '--with-http_v2_module', '--with-http_stub_status_module', '--error-log-path=stderr', '--http-log-path=/dev/stdout', '--pid-path=/tmp/nginx.pid', '--http-client-body-temp-path=/tmp/client_body', '--http-proxy-temp-path=/tmp/proxy']
    if platform.system() == 'FreeBSD': options += ['--with-cc-opt=-I/usr/local/include', '--with-ld-opt=-L/usr/local/lib']
    run(['./configure', *options], cwd=nginx, env=env)
    run([make, f'-j{args.jobs}'], cwd=nginx, env=env)
    env.update(NGINX_SOURCE_DIR=str(nginx), NGINX_BUILD_DIR=str(nginx/'objs'), CARGO_TARGET_DIR=str(work/'target'))
    features = args.features.split(',') if args.features else []
    if args.libpq == 'borrowed': features.append('borrowed-libpq')
    for cmd in (['test', '--test', 'core', '--test', 'allocations'], ['build']):
        run(['cargo', '+'+RUST, *cmd, '--release', '--locked', '--features', ','.join(sorted(set(features)))], cwd=ROOT, env=env)
    for name in ('bin', 'modules', 'logs', 'libpq'): (prefix/name).mkdir(exist_ok=True)
    shutil.copy(nginx/'objs/nginx', prefix/'bin/nginx')
    shutil.copy(work/'target/release/libngx_http_pgxio_module.so', prefix/'modules/libngx_http_pgxio_module.so')
    expected=183 if platform.machine() in ('arm64','aarch64') else 62
    for binary in (prefix/'bin/nginx',prefix/'modules/libngx_http_pgxio_module.so'):
        with binary.open('rb') as stream: header=stream.read(20)
        if header[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(header[18:20],'little')!=expected:
            raise RuntimeError('compiled ELF does not match the requested 64-bit CPU: '+str(binary))
    metadata.update(features=features, nginx=NGINX, rust=RUST,
                    elf_machine=expected,
                    pg_config=subprocess.check_output(['pg_config', '--version'], text=True).strip())
    if args.libpq=='borrowed': metadata.update(borrowed_base=source['base'], borrowed_ref=source['ref'])
    (prefix/'build.json').write_text(json.dumps(metadata, indent=2)+'\n')


if __name__ == '__main__': main()
