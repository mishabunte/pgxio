"""Explicit build/runtime dependency evidence for native installations."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess


def capture(argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def generate(root, prefix, metadata, env):
    root, prefix = Path(root), Path(prefix)
    output = prefix/'share/pgxio'
    output.mkdir(parents=True, exist_ok=True)
    binaries = [prefix/'bin/nginx', prefix/'modules/libngx_http_pgxio_module.so']
    hashes = {str(p.relative_to(prefix)): digest(p) for p in binaries}
    graph = subprocess.check_output(['cargo','metadata','--locked','--format-version','1'],cwd=root,env=env,text=True)
    graph = json.loads(graph)
    build = dict(version=1, kind='build', metadata=metadata, sources=json.loads((root/'scripts/sources.json').read_text()),
                 cargo_lock_sha256=digest(root/'Cargo.lock'),
                 dependencies=[dict(name=p['name'],version=p['version'],source=p['source']) for p in graph['packages']],
                 binaries=hashes, compiler=capture(['cc','--version']))
    libraries = {}
    for binary in binaries:
        linked = capture(['ldd',str(binary)])
        for line in linked.splitlines():
            if 'not found' in line: raise RuntimeError('unresolved library: '+line)
            for part in line.split():
                if part.startswith('/') and Path(part).is_file(): libraries[part]=digest(part)
    packages = capture(['pkg','query','%n %v'] if platform.system()=='FreeBSD' else ['dpkg-query','-W','-f=${Package} ${Version}\n'])
    runtime = dict(version=1,kind='runtime',packages=packages.splitlines(),libraries=libraries,binaries=hashes,
                   nginx=capture([str(binaries[0]),'-V']))
    for name,value in [('build',build),('runtime',runtime)]:
        (output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
