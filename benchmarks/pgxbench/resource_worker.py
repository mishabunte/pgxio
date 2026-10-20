"""Read-only sampler executed on the Docker host, outside measured containers."""
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pgxbench.resources import host_sample, read_group


def main():
    groups = json.loads(sys.stdin.readline())
    stopped = threading.Event()
    def watch():
        sys.stdin.read()
        stopped.set()
    threading.Thread(target=watch, daemon=True).start()
    # Hard deadline also prevents an orphaned SSH session from sampling forever.
    # Leave time for warmup and postflight around a full 24-hour soak.
    deadline = time.monotonic() + 172800
    while not stopped.is_set() and time.monotonic() < deadline:
        sample = {"type": "sample", "timestamp": time.time(), "monotonic_seconds": time.monotonic(),
                  "groups": {}, "errors": []}
        for name, info in groups.items():
            try:
                cgroup = (Path('/proc') / str(info['pid']) / 'cgroup').read_text()
                relative = next(line[3:] for line in cgroup.splitlines() if line.startswith('0::'))
                sample['groups'][name] = {'role': info['role'], 'container_id': info['container_id'],
                    **read_group(Path('/sys/fs/cgroup') / relative.lstrip('/'))}
            except (OSError, StopIteration, KeyError) as error:
                sample['errors'].append(name + ':' + type(error).__name__)
        sample['host'] = host_sample()
        print(json.dumps(sample), flush=True)
        stopped.wait(1)


if __name__ == '__main__':
    main()
