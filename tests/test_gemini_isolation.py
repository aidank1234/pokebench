"""Real macOS process probes, with no inference or credential reads."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cynthiabench.gemini_isolation import (
    CALCULATION_PYTHON, DEFAULT_CLIENT, isolated_agent_env, strict_sandbox_profile,
)


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt probes')


def run_probe(workspace, code, port=19991, internal_ports=(),
              allow_ephemeral_loopback_listener=False, allow_keychain=False):
    result = subprocess.run([
        '/usr/bin/sandbox-exec', '-p', strict_sandbox_profile(
            workspace, port, executable=Path('/usr/bin/true'), internal_ports=internal_ports,
            allow_ephemeral_loopback_listener=allow_ephemeral_loopback_listener,
            allow_keychain=allow_keychain),
        str(CALCULATION_PYTHON), '-I', '-c', code,
    ], cwd=workspace, env=isolated_agent_env(workspace),
       capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_only_episode_data_is_readable_and_writable(tmp_path):
    workspace = tmp_path / 'episode-one'
    workspace.mkdir()
    peer = tmp_path / 'episode-two'
    peer.mkdir()
    (peer / 'strategy.txt').write_text('peer sentinel')
    private = tmp_path / 'private-history'
    private.mkdir()
    files = [private / name for name in ['results.json', 'history.jsonl', 'settings.json']]
    for file in files:
        file.write_text('private sentinel')
    forbidden = [str(peer / 'strategy.txt'), *map(str, files)]
    (workspace / 'symlink-escape').symlink_to(peer / 'strategy.txt')
    forbidden.append(str(workspace / 'symlink-escape'))
    code = f'''
import json, os, ssl, decimal, hashlib, urllib.request
from pathlib import Path
for path in {forbidden!r}:
    try:
        fd = os.open(path, os.O_RDONLY)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise AssertionError('outside read allowed: ' + path)
try:
    Path({str(peer / 'injected.txt')!r}).write_text('wrong')
except PermissionError:
    pass
else:
    raise AssertionError('peer write allowed')
try:
    os.link({str(peer / 'strategy.txt')!r}, 'hardlink-escape')
except PermissionError:
    pass
else:
    raise AssertionError('outside hardlink allowed')
Path('calculation.txt').write_text(str(sum(i*i for i in range(10))))
assert Path('calculation.txt').read_text() == '285'
assert hashlib.sha256(b'calculation').hexdigest()
assert decimal.Decimal('1.5') * 2 == 3
print(json.dumps({{'outside_reads_denied': len({forbidden!r}), 'peer_write': 'denied',
                  'hardlink': 'denied', 'own_workspace': 'allowed', 'calculation': 'passed',
                  'ssl_import': 'passed'}}))
'''
    result = run_probe(workspace, code, allow_keychain=True,
                       allow_ephemeral_loopback_listener=True)
    assert result['outside_reads_denied'] == len(forbidden)
    assert not (peer / 'injected.txt').exists()


def test_only_battle_loopback_and_no_host_unix_socket(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'battle')

    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    unix_path = str(workspace / 'host.sock')
    if len(unix_path.encode()) >= 104:
        # Only the host test binds this socket. The agent must not connect.
        import uuid
        unix_path = '/tmp/gemini-boundary-' + uuid.uuid4().hex
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(unix_path)
    unix.listen()
    try:
        code = f'''
import json, socket, urllib.request, urllib.error
assert socket.getaddrinfo('localhost', 443)
with urllib.request.urlopen('http://127.0.0.1:{servers[0].server_port}', timeout=5) as r:
    assert r.read() == b'battle'
try:
    urllib.request.urlopen('http://127.0.0.1:{servers[1].server_port}', timeout=3)
except urllib.error.URLError:
    pass
else:
    raise AssertionError('unrelated localhost reachable')
s=socket.socket(socket.AF_UNIX)
try:
    s.connect({unix_path!r})
except PermissionError:
    pass
else:
    raise AssertionError('host socket reachable')
print(json.dumps({{'battle':'allowed','other_localhost':'denied','unix_socket':'denied'}}))
'''
        assert run_probe(workspace, code, servers[0].server_port,
                         allow_ephemeral_loopback_listener=True,
                         allow_keychain=True)['battle'] == 'allowed'
    finally:
        unix.close()
        Path(unix_path).unlink(missing_ok=True)
        for server in servers:
            server.shutdown()
            server.server_close()


def test_only_reserved_internal_listener_is_allowed(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    reservations = [socket.socket() for _ in range(2)]
    for reservation in reservations:
        reservation.bind(('127.0.0.1', 0))
    allowed, forbidden = [reservation.getsockname()[1] for reservation in reservations]
    for reservation in reservations:
        reservation.close()
    code = f'''
import json, socket
allowed=socket.socket()
allowed.bind(('127.0.0.1', {allowed}))
allowed.listen()
client=socket.create_connection(('127.0.0.1', {allowed}), timeout=3)
peer,_=allowed.accept()
client.sendall(b'own-ls')
assert peer.recv(16)==b'own-ls'
blocked=socket.socket()
try:
    blocked.bind(('127.0.0.1', {forbidden}))
except PermissionError:
    pass
else:
    raise AssertionError('arbitrary listener permitted')
print(json.dumps({{'reserved_listener':'allowed','other_listener':'denied'}}))
'''
    assert run_probe(workspace, code, internal_ports=(allowed,))['reserved_listener'] == 'allowed'


def test_ephemeral_listener_does_not_grant_outbound_self_access(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    code = '''
import json, socket
server=socket.socket()
server.bind(('127.0.0.1', 0))
server.listen()
port=server.getsockname()[1]
try:
    socket.create_connection(('127.0.0.1', port), timeout=3)
except PermissionError:
    pass
else:
    raise AssertionError('ephemeral listener enabled outbound access')
print(json.dumps({'ephemeral_listener':'allowed','outbound_self':'denied'}))
'''
    assert run_probe(workspace, code,
                     allow_ephemeral_loopback_listener=True)['outbound_self'] == 'denied'


def test_keychain_cli_permission_is_explicit_without_querying_credentials(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    code = '''
import json, subprocess
try:
    r=subprocess.run(['/usr/bin/security', 'help'], capture_output=True, text=True, timeout=5)
except PermissionError:
    print(json.dumps({'security_executable':'denied'}))
else:
    print(json.dumps({'security_executable':'allowed','code':r.returncode}))
'''
    assert run_probe(workspace, code)['security_executable'] == 'denied'
    assert run_probe(workspace, code, allow_keychain=True)['security_executable'] == 'allowed'


def test_native_login_keychain_exception_is_exact_and_read_only(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
    if not keychain.is_file():
        pytest.skip('Local native login Keychain is unavailable')
    host = subprocess.run(['/usr/bin/security', 'default-keychain', '-d', 'user'],
                          capture_output=True, text=True, timeout=10)
    if host.returncode:
        pytest.skip('Host has no default user Keychain')
    expected = host.stdout.strip().strip('"')
    if expected != str(keychain):
        pytest.skip('Host uses a nonstandard user Keychain location')
    code = f'''
import json, os, subprocess
r=subprocess.run(['/usr/bin/security', 'default-keychain', '-d', 'user'],
                 capture_output=True, text=True, timeout=10)
assert r.returncode==0, r.stderr
assert r.stdout.strip().strip('"')=={expected!r}
fd=os.open({str(keychain)!r}, os.O_RDONLY)
os.close(fd)
try:
    fd=os.open({str(keychain)!r}, os.O_WRONLY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError('native Keychain database became writable')
print(json.dumps({{'native_default_keychain':'resolved','encrypted_database':'read_only'}}))
'''
    assert run_probe(workspace, code, allow_keychain=True)['encrypted_database'] == 'read_only'


def test_foreign_process_boundaries(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    target = subprocess.Popen([str(CALCULATION_PYTHON), '-I', '-c', 'import time;time.sleep(45)'])
    try:
        code = f'''
import ctypes, json, os
try:
    os.kill({target.pid}, 0)
except PermissionError:
    pass
else:
    raise AssertionError('foreign signaling permitted')
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
lib.mach_task_self.restype=ctypes.c_uint
task=ctypes.c_uint()
assert lib.task_for_pid(lib.mach_task_self(), {target.pid}, ctypes.byref(task)) != 0
proc=ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
buf=ctypes.create_string_buffer(4096)
assert proc.proc_pidpath({target.pid},buf,4096)==0
print(json.dumps({{'signal':'denied','foreign_task':'denied','foreign_process_info':'denied'}}))
'''
        assert run_probe(workspace, code)['signal'] == 'denied'
        assert target.poll() is None
    finally:
        target.terminate()
        target.wait(timeout=5)


def test_native_command_pty_creation_does_not_expose_host_terminals(tmp_path):
    workspace = tmp_path / 'episode'
    workspace.mkdir()
    master, slave = os.openpty()
    foreign_terminal = os.ttyname(slave)
    try:
        code = f'''
import errno, fcntl, json, os, struct, subprocess, termios
try:
    fd=os.open({foreign_terminal!r}, os.O_RDWR | os.O_NOCTTY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError('foreign host terminal became accessible')
master,slave=os.openpty()
termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(slave))
fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH',24,80,0,0))
child=subprocess.Popen(['/bin/zsh','-f','-c',
    'exec "$CYNTHIA_PYTHON" -I -c \\'import time; print(285, flush=True); time.sleep(0.3)\\''],
    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
os.close(slave)
output=os.read(master,4096)
assert child.wait(timeout=10)==0
os.close(master)
assert b'285' in output, output
print(json.dumps({{'own_pty':'allowed','native_shell_calculation':'passed','foreign_pty':'denied'}}))
'''
        result = run_probe(workspace, code, allow_keychain=True,
                           allow_ephemeral_loopback_listener=True)
        assert result['native_shell_calculation'] == 'passed'
    finally:
        os.close(master)
        os.close(slave)


def test_env_drops_host_credentials_and_state_without_replacing_home(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'sentinel-not-real')
    monkeypatch.setenv('OPENAI_API_KEY', 'sentinel-not-real')
    monkeypatch.setenv('ANTIGRAVITY_PROJECT_ID', 'old-project')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/outside/host.sock')
    env = isolated_agent_env(tmp_path)
    assert env.get('HOME') == os.environ.get('HOME')
    assert not {'GEMINI_API_KEY', 'OPENAI_API_KEY', 'ANTIGRAVITY_PROJECT_ID', 'SSH_AUTH_SOCK'} & env.keys()
    assert env['CYNTHIA_PYTHON'] == str(CALCULATION_PYTHON)
    for key in ('TMPDIR', 'GEMINI_DIR', 'ANTIGRAVITY_GEMINI_DIR', 'XDG_CONFIG_HOME'):
        assert Path(env[key]).is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize('port', [True, 0, -1, 65536, '9999'])
def test_bad_ports_fail_closed(tmp_path, port):
    with pytest.raises(ValueError):
        strict_sandbox_profile(tmp_path, port)


@pytest.mark.parametrize('ports', [(True,), (0,), ('9999',), '9999', (19991,)])
def test_bad_internal_ports_fail_closed(tmp_path, ports):
    with pytest.raises(ValueError):
        strict_sandbox_profile(tmp_path, 19991, internal_ports=ports)


@pytest.mark.parametrize('kwargs', [{'allow_keychain': 'false'},
                                  {'allow_ephemeral_loopback_listener': 1}])
def test_optional_privileges_require_boolean_opt_in(tmp_path, kwargs):
    with pytest.raises(ValueError):
        strict_sandbox_profile(tmp_path, 19991, **kwargs)
