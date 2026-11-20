"""Independent actual macOS boundary probes; no model or credential queries."""
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from cynthiabench.opencode_isolation import (
    CALCULATION_PYTHON, isolated_agent_env, strict_sandbox_profile,
)


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt probes')
ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parents[1]


def run_probe(workspace, code, battle_port=19991, proxy_port=19992):
    result = subprocess.run([
        '/usr/bin/sandbox-exec', '-p', strict_sandbox_profile(
            workspace, battle_port, executable=Path('/usr/bin/true'), proxy_port=proxy_port),
        str(CALCULATION_PYTHON), '-I', '-c', code,
    ], cwd=workspace, env=isolated_agent_env(workspace, 'synthetic-episode-token'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_historical_trees_peers_native_histories_and_aliases_denied(tmp_path):
    workspace = tmp_path / 'own-episode'
    workspace.mkdir()
    peer = tmp_path / 'peer-episode'
    peer.mkdir()
    (peer / 'strategy.txt').write_text('peer sentinel')
    private = tmp_path / 'private-history'
    private.mkdir()
    existing = [private / name for name in ['results.json', 'history.jsonl', 'auth.json', 'settings.json']]
    for file in existing:
        file.write_text('private sentinel')
    forbidden = [str(peer / 'strategy.txt'), *map(str, existing)]
    target = existing[1]
    (workspace / 'escape').symlink_to(target)
    forbidden += [str(workspace / 'escape'),
                  str(workspace / '..' / peer.name / 'strategy.txt')]
    code = f'''
import decimal, hashlib, json, os, ssl
from pathlib import Path
for path in {forbidden!r}:
    try:
        fd=os.open(path,os.O_RDONLY)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise AssertionError('outside read allowed: '+path)
try:
    Path({str(peer / 'injection.txt')!r}).write_text('outside')
except PermissionError:
    pass
else:
    raise AssertionError('peer write allowed')
try:
    os.link({str(peer / 'strategy.txt')!r},'hardlink-escape')
except PermissionError:
    pass
else:
    raise AssertionError('outside hardlink allowed')
Path('calculation.txt').write_text(str(sum(i*i for i in range(10))))
assert Path('calculation.txt').read_text()=='285'
assert decimal.Decimal('1.5')*2==3
assert hashlib.sha256(b'math').hexdigest()
print(json.dumps({{'denied_reads':len({forbidden!r}),'own_write':'allowed','math':'allowed','ssl':'allowed'}}))
'''
    assert run_probe(workspace, code)['denied_reads'] == len(forbidden)
    assert not (peer / 'injection.txt').exists()


def test_battle_proxy_allowed_other_loopback_and_host_socket_denied(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'local engineering probe')

    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(3)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    unix = socket.socket(socket.AF_UNIX)
    unix_path = str(workspace / 'host.sock')
    if len(unix_path.encode()) >= 104:
        import uuid
        unix_path = '/tmp/opencode-boundary-' + uuid.uuid4().hex
    unix.bind(unix_path)
    unix.listen()
    try:
        code = f'''
import json, socket, urllib.request, urllib.error
for port in {[s.server_port for s in servers[:2]]!r}:
    with urllib.request.urlopen('http://127.0.0.1:'+str(port),timeout=5) as r:
        assert r.read()==b'local engineering probe'
for host in ('127.0.0.1','localhost'):
    try:
        urllib.request.urlopen('http://'+host+':{servers[2].server_port}',timeout=3)
    except urllib.error.URLError as e:
        assert isinstance(e.reason,PermissionError), repr(e)
    else:
        raise AssertionError('unrelated loopback allowed')
s=socket.socket(socket.AF_UNIX)
try:
    s.connect({unix_path!r})
except PermissionError:
    pass
else:
    raise AssertionError('host Unix socket allowed')
print(json.dumps({{'battle':'allowed','proxy':'allowed','other_loopback':'denied','host_socket':'denied'}}))
'''
        assert run_probe(workspace, code, servers[0].server_port,
                         servers[1].server_port)['other_loopback'] == 'denied'
    finally:
        unix.close()
        Path(unix_path).unlink(missing_ok=True)
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(os.environ.get('POKEBENCH_NETWORK_PROBES') != '1', reason='Optional public-network probe')
def test_public_https_and_calculation_allowed(tmp_path):
    # Distinguish lack of Internet on the host from a sandbox regression.
    try:
        with urlopen('https://example.com', timeout=10) as response:
            assert response.status == 200
    except OSError as error:
        pytest.skip('Host public HTTPS unavailable: ' + type(error).__name__)
    code = '''
import json, urllib.request
with urllib.request.urlopen('https://example.com',timeout=15) as r:
    assert r.status==200
    assert b'Example Domain' in r.read()
print(json.dumps({'public_https':'allowed'}))
'''
    assert run_probe(tmp_path, code)['public_https'] == 'allowed'


def test_no_local_listener_privilege(tmp_path):
    code = '''
import json,socket
s=socket.socket()
try:
    s.bind(('127.0.0.1',0))
except PermissionError:
    pass
else:
    raise AssertionError('local listener privilege granted')
print(json.dumps({'local_listener':'denied'}))
'''
    assert run_probe(tmp_path, code)['local_listener'] == 'denied'


def test_security_executable_and_keychain_access_denied_without_query(tmp_path):
    profile = strict_sandbox_profile(tmp_path, 19991, executable=Path('/usr/bin/true'), proxy_port=19992)
    assert '(deny process-exec (literal "/usr/bin/security"))' in profile
    assert 'com.apple.securityd' not in profile
    assert 'com.apple.cfprefsd.daemon' not in profile
    assert '/Library/Keychains/' not in profile
    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
    code = f'''
import json,os,subprocess
try:
    subprocess.run(['/usr/bin/security','help'],capture_output=True,text=True,timeout=5)
except PermissionError:
    pass
else:
    raise AssertionError('security executable allowed')
if {keychain.is_file()!r}:
    try:
        fd=os.open({str(keychain)!r},os.O_RDONLY)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise AssertionError('Keychain database readable')
print(json.dumps({{'security_executable':'denied','keychain_read':'denied_or_absent'}}))
'''
    assert run_probe(tmp_path, code)['security_executable'] == 'denied'


def test_foreign_process_inspection_and_signaling_denied(tmp_path):
    target = subprocess.Popen([str(CALCULATION_PYTHON), '-I', '-c', 'import time;time.sleep(45)'])
    try:
        code = f'''
import ctypes,json,os
try:
    os.kill({target.pid},0)
except PermissionError:
    pass
else:
    raise AssertionError('foreign signaling permitted')
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.mach_task_self.restype=ctypes.c_uint
task=ctypes.c_uint()
assert lib.task_for_pid(lib.mach_task_self(),{target.pid},ctypes.byref(task))!=0
proc=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
buf=ctypes.create_string_buffer(4096)
assert proc.proc_pidpath({target.pid},buf,4096)==0
print(json.dumps({{'signal':'denied','foreign_task':'denied','process_info':'denied'}}))
'''
        assert run_probe(tmp_path, code)['signal'] == 'denied'
        assert target.poll() is None
    finally:
        target.terminate()
        target.wait(timeout=5)


def test_own_shell_pty_allowed_foreign_terminal_denied(tmp_path):
    master, slave = os.openpty()
    foreign_terminal = os.ttyname(slave)
    command = 'exec "$CYNTHIA_PYTHON" -I -c ' + shlex.quote(
        'import time;print(285,flush=True);time.sleep(0.3)')
    try:
        code = f'''
import fcntl,json,os,struct,subprocess,termios
try:
    fd=os.open({foreign_terminal!r},os.O_RDWR|os.O_NOCTTY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError('foreign terminal accessible')
master,slave=os.openpty()
termios.tcsetattr(slave,termios.TCSANOW,termios.tcgetattr(slave))
fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
child=subprocess.Popen(['/bin/zsh','-f','-c',{command!r}],stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
os.close(slave)
output=os.read(master,4096)
assert child.wait(timeout=10)==0
assert b'285' in output,output
os.close(master)
print(json.dumps({{'own_pty':'allowed','shell_math':'allowed','foreign_terminal':'denied'}}))
'''
        assert run_probe(tmp_path, code)['shell_math'] == 'allowed'
    finally:
        os.close(master)
        os.close(slave)


def test_host_secrets_and_global_state_env_not_inherited(tmp_path, monkeypatch):
    for key in ('DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY',
                'ANTHROPIC_API_KEY', 'SSH_AUTH_SOCK', 'OPENCODE_CONFIG_CONTENT',
                'OPENCODE_SERVER_PASSWORD', 'BASH_ENV', 'NODE_OPTIONS', 'PYTHONPATH'):
        monkeypatch.setenv(key, 'synthetic-host-secret')
    env = isolated_agent_env(tmp_path, 'synthetic-scoped-token')
    assert env['HOME'] == os.environ['HOME']
    assert env['DEEPSEEK_API_KEY'] == 'synthetic-scoped-token'
    assert 'synthetic-host-secret' not in env.values()
    for key in ('TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                'XDG_STATE_HOME', 'OPENCODE_DB', 'OPENCODE_TEST_HOME',
                'OPENCODE_TEST_MANAGED_CONFIG_DIR'):
        assert Path(env[key]).is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize('port', [True, 0, -1, 65536, '19991'])
def test_bad_battle_ports_fail_closed(tmp_path, port):
    with pytest.raises(ValueError):
        strict_sandbox_profile(tmp_path, port, proxy_port=19992)


@pytest.mark.parametrize('port', [True, 0, -1, 65536, '19992', 19991])
def test_bad_proxy_ports_fail_closed(tmp_path, port):
    with pytest.raises(ValueError):
        strict_sandbox_profile(tmp_path, 19991, proxy_port=port)
