"""Control-plane checks that require neither Docker nor FreeBSD."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('platform_runner',ROOT/'scripts/test-platforms.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class Matrix(unittest.TestCase):
    def test_all_required_cells_and_filters(self):
        manifest=json.loads(runner.MANIFEST.read_text())
        cells=runner.cells(manifest)
        self.assertEqual(len(cells),20)
        self.assertEqual(len({c['id'] for c in cells}),20)
        for system in manifest['oses']:
            self.assertEqual({(c['arch'],c['variant']) for c in cells if c['os']==system},
                             {('amd64','system'),('amd64','borrowed'),('arm64','system'),('arm64','borrowed')})
        self.assertEqual(runner.cells(manifest,['freebsd15.1'],['arm64'],['borrowed']),
                         [dict(id='freebsd15.1-arm64-borrowed',os='freebsd15.1',arch='arm64',variant='borrowed')])

    def test_dry_run_without_docker_on_path(self):
        env=dict(os.environ,PATH='')
        r=subprocess.run([sys.executable,str(ROOT/'scripts/test-platforms.py')],env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(r.stdout)['count'],20)

    def test_freebsd_exec_survives_ssh_shell_join_without_copying_vm_cache(self):
        manifest=json.loads(runner.MANIFEST.read_text())
        cell=runner.cells(manifest,['freebsd15.1'],['arm64'],['system'])[0]
        command="cd /work && printf '%s\\n' 'a; $(literal text)'"
        cmd=runner.freebsd_invocation(manifest,cell,Path('/snapshot with spaces'),Path('/cache'),command)
        remote=cmd[cmd.index('--')+1:]
        self.assertEqual(shlex.split(' '.join(remote)),['sh','-ec',command])
        self.assertIn('/snapshot with spaces:/workspace',cmd)
        self.assertIn('/cache:/cache',cmd) # Docker mount, not a guest sync.
        anyvm=cmd[cmd.index('/anyvm.org/anyvm.py'):]
        self.assertNotIn('/cache:/cache',anyvm)
        self.assertEqual(anyvm[anyvm.index('--sync')+1],'tar')
        self.assertIn('--snapshot',anyvm)
        cmd=runner.freebsd_invocation(manifest,cell,Path('/source'),Path('/unused'),command,'pgxio-test-volume')
        self.assertIn('pgxio-test-volume:/data',cmd)
        self.assertNotIn('/unused:/cache',cmd)
        self.assertEqual(cmd[cmd.index('--cache-dir')+1],'/data/cache')

    def test_partial_and_failed_evidence_cannot_pass(self):
        cells=runner.cells(json.loads(runner.MANIFEST.read_text()))
        with tempfile.TemporaryDirectory() as name:
            output=Path(name)
            self.assertFalse(runner.report(output,[],cells))
            rows=[dict(cell=c,passed=True,evidence='example') for c in cells]
            self.assertTrue(runner.report(output,rows,cells))
            rows[-1]['passed']=False
            self.assertFalse(runner.report(output,rows,cells))
            self.assertIn('FAIL',(output/'RESULTS.md').read_text())

    def test_snapshot_excludes_secrets_builds_and_results(self):
        files=runner.source_files()
        self.assertIn('crates/pgxio-module/src/lib.rs',files)
        self.assertIn('scripts/platforms/matrix.json',files)
        self.assertIn('bin/pgxio',files)
        for name in files:
            self.assertFalse(name.startswith(('artifacts/','.git/','.pgxio/')))
            self.assertNotIn('node_modules',Path(name).parts)

    def test_native_tls_rejection_token_uses_fixture_secret_without_dotenv(self):
        import base64, hashlib, hmac, importlib.util
        spec=importlib.util.spec_from_file_location('platform_integration',ROOT/'tests/integration.py')
        integration=importlib.util.module_from_spec(spec);spec.loader.exec_module(integration)
        with patch.dict(os.environ,{'PGXIO_TEST_DEV_SECRET':'fixture-only','PGXIO_TEST_RSA_KEY':'unused'}):
            with patch.object(integration.subprocess,'check_output',side_effect=AssertionError('unexpected CLI or signer')):
                encoded=integration.development_token()
        header,payload,signature=encoded.split('.')
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header+'=='))['alg'],'HS256')
        expected=hmac.new(b'fixture-only',(header+'.'+payload).encode(),hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(signature+'=='),expected)

    def test_resume_rejects_modified_dependency_evidence_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'install').mkdir();(root/'production').mkdir()
            cell=dict(os='freebsd15.1',arch='arm64',variant='borrowed',id='freebsd15.1-arm64-borrowed')
            actual=dict(cell,passed=True)
            (root/'outcomes.json').write_text(json.dumps(actual))
            for path in ('stages.json','packages.txt','install/build.json','production/build.json'):
                (root/path).write_text('frozen dependency evidence')
            row=dict(cell=cell,passed=True,evidence=str(root),evidence_sha256=runner.evidence_hashes(root))
            self.assertEqual(runner.retained_evidence(row),row)
            (root/'packages.txt').write_text('different package versions')
            with self.assertRaisesRegex(ValueError,'evidence changed'):runner.retained_evidence(row)
            (root/'outcomes.json').write_text(json.dumps(dict(actual,arch='amd64')))
            with self.assertRaisesRegex(ValueError,'does not match'):runner.retained_evidence(row)

    def test_guest_failure_is_not_relabelled_as_a_pass(self):
        with tempfile.TemporaryDirectory() as name:
            log=Path(name)/'runner.log'
            with patch.object(runner.shutil,'disk_usage',return_value=SimpleNamespace(free=20*1024**3)):
                code=runner.run_guest([sys.executable,'-c',"print('failed case'); raise SystemExit(3)"],log,'pgxio-test-only')
            self.assertEqual(code,3)
            self.assertIn('failed case',log.read_text())

    def test_disk_pressure_stops_only_the_named_fixture(self):
        with tempfile.TemporaryDirectory() as name:
            with patch.object(runner.shutil,'disk_usage',return_value=SimpleNamespace(free=0)), patch.object(runner.subprocess,'run') as command:
                with self.assertRaisesRegex(RuntimeError,'host disk fell below'):
                    runner.run_guest([sys.executable,'-c','import time; time.sleep(20)'],Path(name)/'runner.log','pgxio-test-only')
            self.assertEqual([call.args[0] for call in command.call_args_list],
                             [['docker','pause','pgxio-test-only'],['docker','rm','-f','pgxio-test-only']])


class NativeOperator(unittest.TestCase):
    def test_native_start_reload_stop_and_replacement(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);runtime=root/'runtime';runtime.mkdir()
            executable=root/'nginx'
            executable.write_text('#!'+sys.executable+'''
import os,signal,time
from pathlib import Path
runtime=Path(os.environ['FAKE_RUNTIME'])
def stop(*args):
    (runtime/'nginx.pid').unlink(missing_ok=True)
    raise SystemExit(0)
def reload(*args):
    (runtime/'reloaded').write_text('yes')
signal.signal(signal.SIGQUIT,stop)
signal.signal(signal.SIGHUP,reload)
(runtime/'nginx.pid').write_text(str(os.getpid()))
while True: time.sleep(.02)
''')
            executable.chmod(0o755)
            manifest=root/'native.json'
            manifest.write_text(json.dumps(dict(runtime=str(runtime),nginx=str(executable),pg_bin='unused',api_env={'FAKE_RUNTIME':str(runtime)})))
            env=dict(os.environ,PGXIO_TEST_NATIVE=str(manifest))
            base=[sys.executable,str(ROOT/'tests/native-operator.py')]
            try:
                subprocess.run(base+['up','api'],env=env,check=True,capture_output=True,timeout=8)
                first=(runtime/'nginx.pid').read_text()
                subprocess.run(base+['kill','-s','HUP','api'],env=env,check=True,capture_output=True,timeout=8)
                for _ in range(100):
                    if (runtime/'reloaded').exists():break
                    time.sleep(.01)
                self.assertTrue((runtime/'reloaded').exists())
                (runtime/'block').touch()
                subprocess.run(base+['up','api'],env=env,check=True,capture_output=True,timeout=8)
                self.assertFalse((runtime/'block').exists())
                self.assertNotEqual((runtime/'nginx.pid').read_text(),first)
                subprocess.run(base+['stop','api'],env=env,check=True,capture_output=True,timeout=8)
                self.assertFalse((runtime/'nginx.pid').exists())
            finally:
                if (runtime/'nginx.pid').exists():
                    subprocess.run(base+['stop','api'],env=env,capture_output=True,timeout=8)

    def test_commands_preserve_argument_boundaries_and_sql_stdin(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); runtime=root/'runtime';runtime.mkdir(); pg=root/'pg';pg.mkdir()
            executable=root/'capture'
            executable.write_text('#!'+sys.executable+'\nimport json,sys\nprint(json.dumps(dict(argv=sys.argv[1:],stdin=sys.stdin.read())))\n')
            executable.chmod(0o755);(pg/'psql').symlink_to(executable)
            manifest=root/'native.json'
            manifest.write_text(json.dumps(dict(runtime=str(runtime),nginx=str(executable),pg_bin=str(pg),api_env={})))
            env=dict(os.environ,PGXIO_TEST_NATIVE=str(manifest))
            base=[sys.executable,str(ROOT/'tests/native-operator.py')]
            result=subprocess.run(base+['exec','-T','postgres','psql','-U','postgres','-d','pgxio','-At'],env=env,input="SELECT 'a;b';",text=True,capture_output=True,check=True)
            value=json.loads(result.stdout)
            self.assertEqual(value['stdin'],"SELECT 'a;b';")
            self.assertEqual(value['argv'][-1],'-At')
            result=subprocess.run(base+['exec','-T','api','nginx','-t','-c','/etc/pgxio/check.conf'],env=env,input='',text=True,capture_output=True,check=True)
            self.assertEqual(json.loads(result.stdout)['argv'],['-t','-c',str(runtime/'check.conf')])


class PortableAllocationHooks(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'),'requires C compiler')
    def test_failed_realloc_retains_original_and_failure_is_one_shot(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);src=root/'test.c';exe=root/'test'
            src.write_text('''#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include "test_alloc.h"
'''+(ROOT/'patches/libpq/test_alloc.c').read_text()+'''
int main(void) {
    char *original=pgxio_test_malloc(8); assert(original); original[0]='x';
    libpq_test_fail_alloc(0); errno=0;
    assert(pgxio_test_realloc(original,16)==NULL && errno==ENOMEM);
    assert(original[0]=='x'); free(original);
    void *p=pgxio_test_malloc(8); assert(p); free(p);
    libpq_test_fail_alloc(1);
    p=pgxio_test_calloc(2,8); assert(p);
    assert(pgxio_test_malloc(8)==NULL); free(p);
    libpq_test_fail_alloc(-1); p=pgxio_test_malloc(8); assert(p); free(p);
    return 0;
}
''')
            subprocess.run(['cc','-Wall','-Wextra','-Werror','-I'+str(ROOT/'patches/libpq'),str(src),'-o',str(exe)],check=True,capture_output=True)
            subprocess.run([str(exe)],check=True)
            subprocess.run(['cc','-Wall','-Wextra','-Werror','-DPGXIO_IO_ONLY','-fsyntax-only',str(ROOT/'patches/libpq/test_faults.c')],check=True,capture_output=True)


if __name__=='__main__':unittest.main()
