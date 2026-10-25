#!/usr/bin/env python3
"""One OS/CPU/variant release gate. Runs identically in a Linux container/BSD VM."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--libpq', choices=('system','borrowed'),required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--os',required=True)
    p.add_argument('--arch',choices=('amd64','arm64'),required=True)
    p.add_argument('--keep-builds',action='store_true')
    args=p.parse_args(); output=args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    observed=platform.machine(); expected='FreeBSD' if args.os.startswith('freebsd') else 'Linux'
    assert platform.system()==expected
    assert observed in (('amd64','x86_64') if args.arch=='amd64' else ('aarch64','arm64'))
    if expected=='Linux':
        release=dict(line.strip().split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        assert release['ID'].strip('"')==('debian' if args.os=='debian13' else 'ubuntu')
        version=release['VERSION_ID'].strip('"')
        assert version==('13' if args.os=='debian13' else args.os.removeprefix('ubuntu'))
    else:
        assert platform.release().startswith(args.os.removeprefix('freebsd')+'-RELEASE')
    stages=[]
    def execute(name,cmd):
        print('STAGE '+name,flush=True)
        with (output/(name+'.log')).open('w') as log:
            r=subprocess.run([str(x) for x in cmd],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        stages.append(dict(stage=name,passed=r.returncode==0))
        (output/'stages.json').write_text(json.dumps(stages,indent=2))
        if r.returncode: raise RuntimeError(name+' failed; see '+str(output/(name+'.log')))
    prefix=output/'install'; diag=output/'diagnostic-client'
    build=[sys.executable,ROOT/'scripts/platforms/build.py','--prefix',prefix,'--libpq',args.libpq]
    native=[sys.executable,ROOT/'tests/run-native.py','--prefix',prefix]
    passed=False
    try:
        execute('build-production',build)
        # Save the production binary/module before the diagnostic-feature rebuild.
        for name in ('bin','modules','libpq','build.json'):
            src=prefix/name; dst=output/'production'/name; dst.parent.mkdir(parents=True,exist_ok=True)
            if src.is_dir(): shutil.copytree(src,dst)
            else: shutil.copy(src,dst)
        execute('production-smoke',native+['--suite','smoke','--output',output/'production-smoke'])
        execute('build-selection-diagnostics',build+['--features','benchmark-auth'])
        execute('plaintext-auto',native+['--output',output/'plaintext-auto'])
        execute('tls-auto',native+['--production','--output',output/'tls-auto'])
        execute('tls-schema-copy',native+['--production','--schema','--libpq-buf','copy','--output',output/'tls-schema-copy'])
        if args.libpq=='borrowed':
            execute('build-allocation-diagnostics',[sys.executable,ROOT/'scripts/platforms/build.py','--prefix',diag,'--libpq','borrowed','--diagnostic-allocations','--client-only'])
            for tls in (False,True):
                name='borrowed-faults-'+('tls' if tls else 'plain')
                execute(name,native+['--libpq-buf','borrowed','--faults','--c-contract-prefix',diag,'--output',output/name]+(['--production'] if tls else []))
            for profile in ('sequential','unprepared','cache','failfast'):
                execute(profile,native+['--profile',profile,'--suite','performance','--libpq-buf','borrowed','--output',output/profile])
        passed=True
    finally:
        packages=subprocess.run(['pkg','info'] if expected=='FreeBSD' else ['dpkg-query','-W'],capture_output=True,text=True)
        (output/'packages.txt').write_text(packages.stdout)
        if not args.keep_builds:
            for root in (prefix,diag):
                work=root/'build'
                if work.exists(): shutil.rmtree(work)
            for cluster in output.glob('*/postgres'):
                if cluster.is_dir(): shutil.rmtree(cluster)
        (output/'outcomes.json').write_text(json.dumps(dict(passed=passed,os=args.os,arch=args.arch,variant=args.libpq,observed_os=platform.platform(),observed_arch=observed,stages=stages),indent=2))
    return 0 if passed else 1


if __name__=='__main__': sys.exit(main())
