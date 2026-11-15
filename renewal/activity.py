"""Read supported harness status metadata, without loading turns or messages."""
import json
import os
import base64
import hashlib
import re
import subprocess
import socket
import struct
import time
from pathlib import Path


def classify(idle_seconds, claude, codex, threshold=600):
    from .nightly import Activity
    if claude is None or codex is None:
        return Activity()
    active = set()
    for rows, id_key in ((claude,'sessionId'),(codex,'id')):
        for row in rows:
            status = row.get('status')
            status = status.get('type') if isinstance(status,dict) else status
            if status not in {'idle','notLoaded','active','busy','working','running','waiting_for_permission'}:
                return Activity()
            if status not in {'idle','notLoaded'}:
                if not row.get(id_key):
                    return Activity()
                active.add(row[id_key])
    return Activity(verified=True,idle=idle_seconds>=threshold and not active,
                    active_sessions=frozenset(active))


class RpcMetadata:
    def __init__(self):
        self.deadline = time.monotonic() + 30
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.buffer = b''
        self.serial = 0
        try:
            home = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
            self.sock.connect(str(home/'app-server-control/app-server-control.sock'))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: '+key+'\r\n\r\n').encode())
            while b'\r\n\r\n' not in self.buffer:
                self.buffer += self.sock.recv(4096)
                if len(self.buffer)>16384 or not self.buffer:
                    raise ValueError('invalid metadata handshake')
            headers,self.buffer=self.buffer.split(b'\r\n\r\n',1)
            expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if not headers.startswith(b'HTTP/1.1 101 ') or expected not in headers:
                raise ValueError('metadata handshake rejected')
        except Exception:
            self.sock.close()
            raise

    def read(self, size):
        while len(self.buffer)<size:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:
                raise TimeoutError('metadata deadline')
            self.sock.settimeout(min(5,remaining))
            chunk=self.sock.recv(min(65536,size-len(self.buffer)))
            if not chunk:
                raise ValueError('metadata socket ended')
            self.buffer+=chunk
        value,self.buffer=self.buffer[:size],self.buffer[size:]
        return value

    def send(self, message, opcode=1):
        payload=json.dumps(message).encode() if opcode==1 else message
        mask=os.urandom(4)
        size=len(payload)
        header=bytes([128|opcode,128|size]) if size<126 else bytes([128|opcode,128|126])+struct.pack('!H',size)
        self.sock.sendall(header+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))

    def receive(self):
        fragments=b''
        for _ in range(1000):
            first,second=self.read(2)
            opcode=first&15
            size=second&127
            if size==126: size=struct.unpack('!H',self.read(2))[0]
            elif size==127: size=struct.unpack('!Q',self.read(8))[0]
            if second&128 or size+len(fragments)>2*1024*1024 or first&112:
                raise ValueError('unsupported metadata frame')
            payload=self.read(size)
            if opcode==9:
                self.send(payload,10)
                continue
            if opcode==10: continue
            if opcode not in {0,1}:
                raise ValueError('metadata connection closed or nontext')
            fragments+=payload
            if first&128:
                return json.loads(fragments)
        raise ValueError('metadata frame bound')

    def request(self,method,params):
        self.serial += 1
        self.send(dict(id=self.serial,method=method,params=params))
        for _ in range(1000):
            reply=self.receive()
            if reply.get('id')==self.serial:
                if 'error' in reply:
                    raise ValueError('metadata request rejected')
                return reply['result']
        raise ValueError('metadata response bound')

    def close(self):
        self.sock.close()  # Only our read-only client connection.


def codex_metadata():
    client=RpcMetadata()
    try:
        client.request('initialize',dict(clientInfo=dict(name='renewal-metadata',version='1'),
            capabilities=dict(experimentalApi=True)))
        client.send(dict(method='initialized'))
        cursor=None; ids=[]; cursors=set()
        for _ in range(20):
            page=client.request('thread/loaded/list',dict(limit=100,cursor=cursor))
            ids.extend(page['data'])
            cursor=page.get('nextCursor')
            if not cursor:
                break
            if cursor in cursors:
                raise ValueError('repeated metadata cursor')
            cursors.add(cursor)
        else:
            raise ValueError('incomplete metadata inventory')
        result=[]
        for sid in ids:
            thread=client.request('thread/read',dict(threadId=sid,includeTurns=False))['thread']
            result.append(dict(id=thread['id'],status=thread['status']))
        return result
    finally:
        client.close()


def probe(threshold=600):
    from .nightly import Activity
    try:
        output=subprocess.run(['/usr/sbin/ioreg','-c','IOHIDSystem'],
            capture_output=True,text=True,timeout=10,check=True).stdout
        matches=re.findall(r'HIDIdleTime[\"\s]*=\s*(\d+)',output)
        if not matches:
            return Activity()
        idle=min(map(int,matches))/1e9
        claude=[]
        for path in (Path.home()/'.claude/sessions').glob('*.json'):
            row=json.loads(path.read_text())
            try:
                os.kill(int(row['pid']),0)
            except ProcessLookupError:
                continue
            started=subprocess.run(['/bin/ps','-p',str(int(row['pid'])),'-o','lstart='],
                capture_output=True,text=True,timeout=5,check=True,
                env=dict(os.environ,TZ='UTC',LC_ALL='C')).stdout.strip()
            if started != row.get('procStart'):
                return Activity()
            claude.append({k:row[k] for k in ('sessionId','status')})
        return classify(idle,claude,codex_metadata(),threshold)
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        return Activity()
