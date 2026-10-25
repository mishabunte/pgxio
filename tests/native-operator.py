#!/usr/bin/env python3
"""Native implementation of the small Compose operator interface used by tests."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    m = json.loads(Path(os.environ['PGXIO_TEST_NATIVE']).read_text())
    runtime = Path(m['runtime'])
    args = sys.argv[1:]
    command = args.pop(0)
    if command == 'exec':
        if args[0] == '-T': args.pop(0)
        service = args.pop(0)
        if service == 'postgres':
            assert args.pop(0) == 'psql'
            return subprocess.call([m['pg_bin']+'/psql', '-h', '127.0.0.1', '-p', '5432', *args])
        assert service == 'api'
        mapped = []
        for arg in args:
            if arg == 'nginx': arg = m['nginx']
            arg = arg.replace('/etc/nginx/nginx.conf', str(runtime/'nginx.conf'))
            arg = arg.replace('/etc/pgxio/', str(runtime)+'/')
            arg = arg.replace('/tmp/pgxio-test-block-db-writes', str(runtime/'block'))
            mapped.append(arg)
        return subprocess.call(mapped)
    if command == 'logs':
        print((runtime/'nginx.log').read_text(), end=''); return 0
    if command in ('stop', 'kill'):
        pid = int((runtime/'nginx.pid').read_text())
        os.kill(pid, signal.SIGHUP if command == 'kill' else signal.SIGQUIT)
        if command == 'stop':
            for _ in range(100):
                if not (runtime/'nginx.pid').exists(): return 0
                time.sleep(.05)
            raise RuntimeError('Nginx graceful shutdown timed out')
        return 0
    if command == 'up':
        if (runtime/'nginx.pid').exists():
            os.kill(int((runtime/'nginx.pid').read_text()), signal.SIGQUIT)
            for _ in range(120):
                if not (runtime/'nginx.pid').exists(): break
                time.sleep(.05)
            else: raise RuntimeError('old worker did not exit')
        # Compose recreates the API's tmpfs on up after stop. Native fixtures
        # must likewise discard transient write-gate state before a fresh master.
        (runtime/'block').unlink(missing_ok=True)
        env = dict(os.environ, **m['api_env'])
        with (runtime/'nginx.log').open('ab') as log:
            subprocess.Popen([m['nginx'], '-p', str(runtime)+'/', '-c', str(runtime/'nginx.conf'), '-g', 'daemon off;'], env=env, stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            if (runtime/'nginx.pid').exists(): return 0
            time.sleep(.05)
        raise RuntimeError('Nginx failed to create its PID file')
    raise ValueError('unsupported native operator command: '+command)


if __name__ == '__main__': sys.exit(main())
