"""Download a checksum-pinned source archive, including verification on cache hits."""
import hashlib
import json
from pathlib import Path
import os
import tempfile
import urllib.request

MANIFEST = Path(__file__).with_name('sources.json')


def fetch(url, destination, expected):
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('a lowercase SHA-256 checksum is required')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        candidate = destination
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(dir=destination.parent)
            os.close(fd)
            candidate = Path(temporary)
            urllib.request.urlretrieve(url, candidate)
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('source checksum mismatch: ' + str(destination))
        if temporary:
            os.replace(temporary, destination)
        return actual
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def prepare_tree(archive, directory, identity):
    """Invalidate only a caller-owned extraction directory when inputs change."""
    import shutil
    directory = Path(directory)
    marker = directory / '.pgxio-source-identity'
    serialized = json.dumps(identity, sort_keys=True)
    if directory.exists() and (not marker.exists() or marker.read_text() != serialized):
        shutil.rmtree(directory)
    return marker, serialized


if __name__ == '__main__':
    import sys
    import re
    name = sys.argv[2] if len(sys.argv) > 2 else 'nginx'
    source = json.loads(MANIFEST.read_text())[name]
    prefix = 'NGINX' if name == 'nginx' else 'POSTGRES'
    field = 'version' if name == 'nginx' else 'ref'
    version = os.environ.get(prefix + ('_VERSION' if name == 'nginx' else '_REF')) or source[field]
    pattern = r'[0-9]+\.[0-9]+\.[0-9]+' if name == 'nginx' else r'[0-9a-f]{40}'
    if not re.fullmatch(pattern, version):
        raise ValueError('invalid source identifier')
    expected = os.environ.get(prefix + '_SHA256') or (source['sha256'] if version == source[field] else '')
    url = source['url'] if version == source[field] else (
        'https://nginx.org/download/nginx-' + version + '.tar.gz' if name == 'nginx' else
        source['repository'].replace('https://github.com/', 'https://codeload.github.com/') + '/tar.gz/' + version)
    fetch(url, sys.argv[1], expected)
    print(version)
