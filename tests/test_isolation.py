"""OS-enforced boundary probes; no model/API inference is used by these tests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cynthiabench.native_agent import ROOT, clean_env, sandbox_profile


def run_probe(workspace, code, port=None):
    profile = workspace.parent / (workspace.name + '.sb')
    profile.write_text(sandbox_profile(workspace, port))
    result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/python3', '-c', code],
                            cwd=workspace, env=clean_env(workspace), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_private_paths_symlinks_hardlinks_and_process_boundaries():
    workspace = ROOT / 'work/isolation-audit' / uuid.uuid4().hex
    workspace.mkdir(parents=True)
    peer = workspace.parent / (workspace.name + '-peer')
    peer.mkdir()
    secret = peer / 'private.txt'
    secret.write_text('not agent input')
    (workspace / 'escape').symlink_to(secret)
    paths = [secret, workspace / 'escape', ROOT / 'src/cynthiabench/worker.py',
             ROOT / 'assets/private/diamond.nds', ROOT / 'work/admin.json',
             Path.home() / '.config/cynthiabench/gemini.key',
             Path.home() / '.codex/state_5.sqlite', Path.home() / '.codex/sqlite/state_5.sqlite',
             Path.home() / '.claude/history.jsonl']
    paths = [str(p) for p in paths if p.exists()]
    target = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(45)'])
    try:
        code = '''
import ctypes,json,os
from pathlib import Path
paths=PATHS
results={}
for path in paths:
 try:
  fd=os.open(path,os.O_RDONLY);os.close(fd)
 except PermissionError:results[path]='denied'
 else:raise AssertionError('private path opened: '+path)
try:os.link(SOURCE,'new-hardlink')
except PermissionError:results['hardlink']='denied'
else:raise AssertionError('hardlink escape allowed')
try:os.kill(TARGET,0)
except PermissionError:results['foreign_signal']='denied'
else:raise AssertionError('outside-process signal permission allowed')
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.mach_task_self.restype=ctypes.c_uint
task=ctypes.c_uint()
status=lib.task_for_pid(lib.mach_task_self(),TARGET,ctypes.byref(task))
assert status!=0, 'outside-process task port acquired'
results['foreign_task_port_return_code']=status
proc=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
buf=ctypes.create_string_buffer(4096)
n=proc.proc_pidpath(TARGET,buf,4096)
assert n==0,'outside-process path inspection succeeded'
results['foreign_process_info']='denied'
assert sum(i*i for i in range(10))==285
Path('calculation.txt').write_text('285')
results['local_calculation']='passed'
print(json.dumps(results))
'''.replace('PATHS', repr(paths)).replace('SOURCE', repr(str(secret))).replace('TARGET', str(target.pid))
        result = run_probe(workspace, code)
        assert result['hardlink'] == result['foreign_process_info'] == 'denied'
        assert target.poll() is None
        report = ROOT / 'reports/isolation-boundary-probe.json'
        report.write_text(json.dumps({'workspace': str(workspace), 'results': result,
            'scope_note': 'Task-port acquisition fails under the sandbox; ordinary macOS permissions may also deny it. No security certification is implied.'}, indent=2) + '\n')
    finally:
        target.terminate()
        target.wait(timeout=5)


def test_only_battle_loopback_port_and_no_host_unix_socket():
    workspace = ROOT / 'work/isolation-audit' / uuid.uuid4().hex
    workspace.mkdir(parents=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):pass
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'allowed')
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    unix = socket.socket(socket.AF_UNIX)
    unix_path = str(workspace / 'host.sock')
    # macOS Unix socket names have a short path limit; this fixture path fits.
    if len(unix_path.encode()) >= 104:
        unix_path = '/tmp/cynthia-socket-' + uuid.uuid4().hex
    unix.bind(unix_path)
    unix.listen()
    try:
        code = '''
import json,socket,urllib.request,urllib.error
with urllib.request.urlopen('http://127.0.0.1:ALLOWED',timeout=5) as r:assert r.read()==b'allowed'
try:urllib.request.urlopen('http://127.0.0.1:BLOCKED',timeout=3)
except urllib.error.URLError:pass
else:raise AssertionError('unrelated loopback port reachable')
s=socket.socket(socket.AF_UNIX)
try:s.connect(UNIX_PATH_TOKEN)
except PermissionError:pass
else:raise AssertionError('host Unix socket reachable')
print(json.dumps({'battle_rpc':'allowed','other_loopback':'denied','host_unix_socket':'denied'}))
'''.replace('ALLOWED', str(servers[0].server_port)).replace('BLOCKED', str(servers[1].server_port)).replace('UNIX_PATH_TOKEN', repr(unix_path))
        result = run_probe(workspace, code, servers[0].server_port)
        assert result['battle_rpc'] == 'allowed'
    finally:
        unix.close()
        Path(unix_path).unlink(missing_ok=True)
        for server in servers:
            server.shutdown();server.server_close()


def test_blocked_model_clients_and_native_credentials():
    workspace = ROOT / 'work/isolation-audit' / uuid.uuid4().hex
    workspace.mkdir(parents=True)
    profile = workspace.parent / (workspace.name + '.sb')
    profile.write_text(sandbox_profile(workspace, block_ai_clients=True))
    import shutil
    for provider in ('codex', 'claude'):
        executable = shutil.which(provider)
        if executable:
            result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), executable, '--version'],
                env=clean_env(workspace), capture_output=True, text=True, timeout=15)
            assert result.returncode != 0
    code = '''
import os
from pathlib import Path
for relative in ('.codex/auth.json','.claude/.credentials.json','.claude.json'):
 try:fd=os.open(Path.home()/relative,os.O_RDONLY)
 except (PermissionError,FileNotFoundError):pass
 else:os.close(fd);raise AssertionError('native auth readable')
'''
    result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/python3', '-c', code],
        env=clean_env(workspace), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
