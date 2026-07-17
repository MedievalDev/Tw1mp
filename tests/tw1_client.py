"""Minimal Two Worlds 1 lobby client for tests.

Speaks the documented wire protocol: length-prefixed zlib packets during
login, NUL-terminated text commands afterwards.
"""

import socket
import struct
import zlib

from tw1mp.protocol import make_dstr, parse_dstr


class TestClient:
    def __init__(self, port, host='127.0.0.1', serial=b'\x11\x22\x33\x44\x55\x66\x77\x88',
                 guid=b'\xAA' * 16):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.serial = serial
        self.guid = guid
        self.buf = b''

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    # -- login phase packets -------------------------------------------

    def _send_packet(self, payload):
        cdata = zlib.compress(payload)
        self.sock.sendall(struct.pack('<I', len(cdata) + 4) + cdata)

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionResetError('server closed connection')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_packet(self):
        [plen] = struct.unpack('<I', self._recv_exact(4))
        return zlib.decompress(self._recv_exact(plen - 4))

    def handshake(self, language='ENG'):
        payload = bytes(16) + make_dstr(language) + bytes(8) + self.serial
        self._send_packet(payload)
        res = self.recv_packet()
        [err] = struct.unpack('<I', res[0:4])
        info, _ = parse_dstr(res, 4)
        return err, info

    def login(self, username, password, register=False, email='', location='',
              age=25, gender=0, description=''):
        payload = make_dstr(username) + make_dstr(password) + self.guid
        if register:
            payload += struct.pack('<I', 1)
            payload += make_dstr(email) + make_dstr(location)
            payload += bytes([age, gender]) + make_dstr(description)
        else:
            payload += struct.pack('<I', 0)
        self._send_packet(payload)
        res = self.recv_packet()
        [err] = struct.unpack('<I', res[0:4])
        return err, res

    # -- lobby phase ----------------------------------------------------

    def send_cmd(self, text, blob=b''):
        self.sock.sendall(text.encode('latin-1') + b'\0' + blob)

    def drain(self, timeout=0.5):
        """Collect whatever the server sends within the window."""
        self.sock.settimeout(timeout)
        out = self.buf
        self.buf = b''
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(5)
        return out

    def wait_for(self, marker, timeout=5.0):
        """Read until `marker` (bytes) shows up; returns everything read."""
        self.sock.settimeout(timeout)
        out = self.buf
        self.buf = b''
        try:
            while marker not in out:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(5)
        return out
