#!/usr/bin/env python3
"""Print or execute the isolated native build/runtime release matrix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'scripts/platforms/matrix.json'


def cells(manifest, oses=(), arches=(), variants=()):
    return [dict(id=f'{system}-{arch}-{variant}',os=system,arch=arch,variant=variant)
            for arch in manifest['architectures'] if not arches or arch in arches
            for system in manifest['oses'] if not oses or system in oses
            for variant in manifest['variants'] if not variants or variant in variants]


def freebsd_invocation(manifest,cell,work,cache,command,volume=None,container=None):
    # Bypass the image's interactive wrapper: it shares /cache into the guest,
    # which would duplicate the entire VM cache when tar synchronization is used.
    argv=['docker','run','--rm','--name',container or 'pgxio-platform-'+cell['id'],
            '-v',str(work)+':/workspace','--entrypoint','python3',manifest['vm_runner'],
            '/anyvm.org/anyvm.py','--data-dir','/data','--os','freebsd','--builder',manifest['freebsd_builder'],
            '--release',manifest['oses'][cell['os']]['release'],'--arch','aarch64' if cell['arch']=='arm64' else 'x86_64',
            '--mem','3072','--cpu','2','--boot-timeout-sec','600','--cache-dir','/data/cache' if volume else '/cache','--vnc','off',
            '--snapshot',
            '--sync','tar','-v','/workspace:/work','--','sh','-ec',shlex.quote(command)]
    if volume:
        argv[argv.index('--entrypoint'):argv.index('--entrypoint')]=['-v',volume+':/data']
    else:
        argv[argv.index('--entrypoint'):argv.index('--entrypoint')]=['-v',str(cache)+':/cache']
    if Path('/dev/kvm').exists():
        argv[argv.index('--entrypoint'):argv.index('--entrypoint')]=['--device','/dev/kvm']
    return argv


def source_files():
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    roots={'Cargo.toml','Cargo.lock','bin','crates','scripts','tests','examples','deploy','sql','patches','sdk','benchmarks'}
    return sorted({name for name in names if name and name.split('/')[0] in roots and (ROOT/name).is_file()
                   and 'node_modules' not in Path(name).parts and '__pycache__' not in Path(name).parts
                   and (not name.startswith('benchmarks/') or name=='benchmarks/requirements.txt')
                   and (not name.startswith('sdk/') or name.startswith('sdk/python/'))})


def fingerprint(files):
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(), hashes


def evidence_hashes(directory):
    required=('outcomes.json','stages.json','packages.txt','install/build.json','production/build.json')
    if (directory/'runtime/outcomes.json').exists(): required+=('runtime/outcomes.json',)
    if (directory/'runtime-smoke/outcomes.json').exists(): required+=('runtime-smoke/outcomes.json',)
    return {name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in required}


def retained_evidence(row):
    evidence=Path(row['evidence']).resolve()
    actual=json.loads((evidence/'outcomes.json').read_text())
    cell=row['cell']
    if not actual['passed'] or any(actual[key]!=cell[key] for key in ('os','arch','variant')):
        raise ValueError('retained guest evidence does not match its passing cell')
    if evidence_hashes(evidence)!=row['evidence_sha256']:
        raise ValueError('retained dependency/build evidence changed')
    return row


def run_guest(cmd, log_path, container, timeout=6*3600):
    """Stop only our workload before host disk exhaustion, including swap growth."""
    with log_path.open('w') as log:
        process=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+timeout
        try:
            while process.poll() is None:
                if shutil.disk_usage(log_path.parent).free < 2*1024**3:
                    subprocess.run(['docker','pause',container],capture_output=True,timeout=15)
                    subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=30)
                    raise RuntimeError('host disk fell below 2 GiB; stopped only the platform fixture')
                if time.monotonic()>=deadline: raise subprocess.TimeoutExpired(cmd,timeout)
                time.sleep(1)
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill();process.wait()


def report(output, rows, expected):
    (output/'outcomes.json').write_text(json.dumps(rows,indent=2)+'\n')
    passed=len(rows)==len(expected) and all(r['passed'] for r in rows)
    lines=['# Platform compatibility validation','',f'Passed: **{passed}**. Completed {len(rows)}/{len(expected)} required cells.','',
           '| OS | CPU | libpq | Result | Evidence |','|---|---|---|---|---|']
    by_id={r['cell']['id']:r for r in rows}
    for cell in expected:
        row=by_id.get(cell['id']); state=('PASS' if row['passed'] else 'FAIL') if row else 'NOT RUN'
        lines.append(f"| {cell['os']} | {cell['arch']} | {cell['variant']} | {state} | {row.get('evidence','') if row else ''} |")
    lines+=['','FreeBSD ran under its own kernel in QEMU. Linux containers test distribution userspace on the Docker host kernel. Emulation timings are not performance evidence.','']
    (output/'RESULTS.md').write_text('\n'.join(lines))
    return passed


def main():
    manifest=json.loads(MANIFEST.read_text())
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--os',choices=tuple(manifest['oses']),action='append',default=[])
    p.add_argument('--arch',choices=('amd64','arm64'),action='append',default=[])
    p.add_argument('--libpq',choices=('system','borrowed'),action='append',default=[])
    p.add_argument('--resume-from',type=Path,action='append',default=[])
    p.add_argument('--keep-toolchains',action='store_true',help='retain this run\'s temporary Linux builders/images and VM base caches for debugging')
    p.add_argument('--min-free-gib',type=int,default=12,help='cold-cell disk headroom (minimum 8); workload stops below 2 GiB')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/platform-validation')
    args=p.parse_args(); jobs=cells(manifest,args.os,args.arch,args.libpq)
    if args.min_free_gib < 8: p.error('--min-free-gib must be at least 8')
    if not args.execute:
        print(json.dumps(dict(cells=jobs,count=len(jobs),vm_runner=manifest['vm_runner']),indent=2)); return 0
    output=args.output.resolve()
    if (output/'source.json').exists(): raise FileExistsError('use a fresh output directory; resume with --resume-from')
    output.mkdir(parents=True,exist_ok=True)
    run_id=hashlib.sha256(str(output).encode()).hexdigest()[:8]
    files=source_files(); source_hash,hashes=fingerprint(files)
    snapshot=output/'source'; snapshot.mkdir()
    for name in files:
        target=snapshot/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(ROOT/name,target)
    metadata=dict(sha256=source_hash,files=hashes,manifest=manifest)
    (output/'source.json').write_text(json.dumps(metadata,indent=2))
    rows=[]; retained={}
    def capture(cmd): return subprocess.check_output(cmd,cwd=ROOT,text=True,stderr=subprocess.STDOUT,timeout=30).strip()
    locks={}
    try:
        # Listing images reads storage metadata; fail before builds when the
        # daemon is reachable but its backing filesystem is broken.
        capture(['docker','image','ls','--format','{{.ID}}'])
        for system in sorted({j['os'] for j in jobs if not j['os'].startswith('freebsd')}):
            image=manifest['oses'][system]['image']
            info=capture(['docker','buildx','imagetools','inspect',image])
            digest=re.search(r'^Digest:\s+(sha256:[a-f0-9]+)',info,re.M).group(1)
            locks[system]=image+'@'+digest
    except (OSError,subprocess.SubprocessError) as error:
        (output/'blocked.json').write_text(json.dumps(dict(error=str(error),stage='docker-preflight'),indent=2))
        report(output,[],jobs)
        print('BLOCKED: Docker preflight failed: '+str(error),file=sys.stderr)
        return 2
    dependencies=dict(images=locks,vm_runner=manifest['vm_runner'])
    (output/'dependencies.json').write_text(json.dumps(dependencies,indent=2))
    for old in args.resume_from:
        old=old.resolve()
        previous=json.loads((old/'source.json').read_text())
        if previous['sha256']!=source_hash or previous['manifest']!=manifest: raise ValueError('resume source or platform manifest differs')
        previous_deps=json.loads((old/'dependencies.json').read_text())
        if previous_deps['vm_runner']!=dependencies['vm_runner']: raise ValueError('VM runner identity differs')
        for key,value in locks.items():
            if key in previous_deps['images'] and previous_deps['images'][key]!=value: raise ValueError('base image identity differs')
        for row in json.loads((old/'outcomes.json').read_text()):
            if row['passed']:
                retained[row['cell']['id']]=retained_evidence(row)
    cache=ROOT/'artifacts/platform-vm-cache'; cache.mkdir(parents=True,exist_ok=True)
    prepared=set(); builders={}; vm_volumes={}; vm_identities={}
    for index,cell in enumerate(jobs,1):
        if cell['id'] in retained:
            rows.append(retained[cell['id']]); report(output,rows,jobs); continue
        print(f"CELL {index}/{len(jobs)} {cell['id']}",flush=True)
        key=(cell['os'],cell['arch'])
        required_gib=8 if key in prepared or key in vm_volumes else args.min_free_gib
        if shutil.disk_usage(output).free < required_gib*1024**3:
            (output/'blocked.json').write_text(json.dumps(dict(stage='disk-headroom',required_free_gib=required_gib,detail='Free space is below the temporary build/VM budget. No new cell started.'),indent=2))
            report(output,rows,jobs)
            print(f'BLOCKED: at least {required_gib} GiB free disk is required before starting another cell',file=sys.stderr)
            return 2
        directory=output/'cells'/cell['id']; directory.mkdir(parents=True,exist_ok=True)
        container='pgxio-platform-'+run_id+'-'+cell['id']
        row=dict(cell=cell,passed=False,evidence=str(directory),started_unix=time.time())
        try:
            if cell['os'].startswith('freebsd'):
                # Copy only the frozen source snapshot; never synchronize the working tree back from a VM.
                work=output/'vm-work'/cell['id']; shutil.copytree(snapshot,work)
                key=(cell['os'],cell['arch'])
                volume='pgxio-platform-vm-'+run_id+'-'+cell['os'].replace('.','-')+'-'+cell['arch']
                vm_volumes[key]=volume
                guest_output='evidence-'+cell['id']
                command=f'cd /work && sh scripts/platforms/bootstrap.sh && python3 -m venv --without-pip /opt/test-python && python3 -m pip --python /opt/test-python/bin/python install pip && /opt/test-python/bin/pip install -r benchmarks/requirements.txt "./sdk/python[django]" && /opt/test-python/bin/python scripts/platforms/guest.py --os {cell["os"]} --arch {cell["arch"]} --libpq {cell["variant"]} --output /work/{guest_output}'
                cmd=freebsd_invocation(manifest,cell,work,cache,command,volume,container)
            else:
                image='pgxio-platform-toolchain:'+run_id+'-'+cell['os']+'-'+cell['arch']
                key=(cell['os'],cell['arch'])
                if key not in prepared:
                    if key not in builders:
                        builder='pgxio-platform-'+run_id+'-'+cell['os'].replace('.','-')+'-'+cell['arch']
                        subprocess.run(['docker','buildx','create','--name',builder,'--driver','docker-container'],check=True,capture_output=True,timeout=60)
                        builders[key]=builder
                    cmd=['docker','buildx','build','--builder',builders[key],'--load','--platform','linux/'+cell['arch'],'--target','toolchain','-f',str(snapshot/'scripts/platforms/Dockerfile'),'--build-arg','BASE_IMAGE='+locks[cell['os']],'-t',image,str(snapshot)]
                    with (directory/'toolchain.log').open('w') as log: subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
                    prepared.add(key)
                row['toolchain_image']=capture(['docker','image','inspect',image,'--format','{{.Id}}'])
                work=None
                cmd=['docker','run','--rm','--platform','linux/'+cell['arch'],'--name',container,
                     '-v',str(snapshot)+':/source:ro','-v',str(directory)+':/evidence','--entrypoint','/opt/test-python/bin/python',image,
                     '/source/scripts/platforms/guest.py','--os',cell['os'],'--arch',cell['arch'],'--libpq',cell['variant'],'--output','/evidence']
            (directory/'command.json').write_text(json.dumps(cmd,indent=2))
            returncode=run_guest(cmd,directory/'runner.log',container)
            if work is not None:
                if (work/guest_output).exists(): shutil.copytree(work/guest_output,directory,dirs_exist_ok=True)
            if returncode: raise RuntimeError(f'guest exited {returncode}; see runner.log')
            evidence=json.loads((directory/'outcomes.json').read_text())
            if not evidence['passed']: raise RuntimeError('guest release gate did not pass')
            if any(evidence[k]!=cell[k] for k in ('os','arch','variant')): raise ValueError('guest identity differs from requested cell')
            if work is None:
                context=directory/'runtime-context';context.mkdir()
                shutil.copytree(directory/'production',context/'install')
                for name in ('runtime.Dockerfile','runtime-deps.sh'):
                    shutil.copy(snapshot/'scripts/platforms'/name,context/name)
                shutil.copy(snapshot/'deploy/nginx.conf',context/'nginx.conf')
                runtime_image='pgxio-platform-runtime:'+run_id+'-'+cell['id']
                cmd=['docker','buildx','build','--builder',builders[(cell['os'],cell['arch'])],'--load','--platform','linux/'+cell['arch'],
                     '-f',str(context/'runtime.Dockerfile'),'--build-arg','BASE_IMAGE='+locks[cell['os']],
                     '--build-arg','CARGO_FEATURES='+('borrowed-libpq' if cell['variant']=='borrowed' else ''),'-t',runtime_image,str(context)]
                with (directory/'runtime-image.log').open('w') as log:
                    subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1800)
                row['runtime_image']=capture(['docker','image','inspect',runtime_image,'--format','{{.Id}}'])
                cmd=[sys.executable,str(snapshot/'tests/run-isolated.py'),'--image',runtime_image,'--project-name','pgxio-platform-'+run_id+'-runtime','--production','--suite','all','--output',str(directory/'runtime')]
                with (directory/'runtime-tests.log').open('w') as log:
                    subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
                runtime_results=json.loads((directory/'runtime/outcomes.json').read_text())
                if not all(s['passed'] for s in runtime_results['suites']): raise RuntimeError('minimal runtime image tests did not pass')
                smoke_cmd=cmd.copy()
                smoke_cmd[smoke_cmd.index('--suite')+1]='smoke'
                smoke_cmd[smoke_cmd.index('--output')+1]=str(directory/'runtime-smoke')
                with (directory/'runtime-smoke.log').open('w') as log:
                    subprocess.run(smoke_cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
                if not args.keep_toolchains:
                    subprocess.run(['docker','image','rm',runtime_image],capture_output=True,timeout=120)
            if work is not None:
                # x86_64 is the builder's default and has no filename suffix.
                suffix='' if cell['arch']=='amd64' else '-aarch64'
                filename=f"freebsd-{manifest['oses'][cell['os']]['release']}{suffix}.qcow2"
                # Keep extraction and cache on the same filesystem: snapshot
                # mode can rename the base instead of making a second 3 GiB copy.
                script='''import hashlib,json,sys
from pathlib import Path
root=Path('/data/cache');result={}
for f in root.rglob(sys.argv[1]):
 h=hashlib.sha256()
 with f.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1048576),b''):h.update(chunk)
 result[str(f.relative_to(root))]=h.hexdigest()
print(json.dumps(result))
'''
                identities=json.loads(capture(['docker','run','--rm','--entrypoint','python3','-v',vm_volumes[(cell['os'],cell['arch'])]+':/data:ro',manifest['vm_runner'],'-c',script,filename]))
                if not identities: raise RuntimeError('no immutable VM base image found for identity capture')
                key=(cell['os'],cell['arch'])
                if key in vm_identities and vm_identities[key]!=identities: raise RuntimeError('VM base identity changed between client variants')
                vm_identities[key]=identities
                row['vm_image_sha256']=identities
            row['evidence_sha256']=evidence_hashes(directory)
            row['passed']=True
        except Exception as error:
            row['error']=str(error)
            print('FAIL '+cell['id']+': '+str(error),flush=True)
            # A timed-out Docker client does not stop its container by itself.
            subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=60)
        row['finished_unix']=time.time(); rows.append(row); report(output,rows,jobs)
        if cell['os'].startswith('freebsd') and not args.keep_toolchains:
            if not any((j['os'],j['arch'])==(cell['os'],cell['arch']) and j['id'] not in retained for j in jobs[index:]):
                key=(cell['os'],cell['arch'])
                if key in vm_volumes:
                    subprocess.run(['docker','volume','rm',vm_volumes[key]],capture_output=True,timeout=120)
            if work is not None and row['passed']: shutil.rmtree(work)
        if not cell['os'].startswith('freebsd') and not args.keep_toolchains:
            key=(cell['os'],cell['arch'])
            if not any((j['os'],j['arch'])==key and j['id'] not in retained for j in jobs[index:]):
                # Only builders/images created under this tool's namespace are
                # removed. Never prune the shared/default builder or volumes.
                if key in builders:
                    subprocess.run(['docker','buildx','rm',builders[key]],capture_output=True,timeout=120)
                if key in prepared:
                    subprocess.run(['docker','image','rm','pgxio-platform-toolchain:'+run_id+'-'+cell['os']+'-'+cell['arch']],capture_output=True,timeout=120)
    return 0 if report(output,rows,jobs) else 1


if __name__=='__main__': sys.exit(main())
