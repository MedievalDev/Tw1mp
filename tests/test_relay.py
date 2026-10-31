"""Protocol tests for the UDP game-session relay.

Each test drives the relay through raw UDP sockets standing in for the two
`dpnetreplace` DLLs, so the exact wire contract the DLL must implement is
pinned here.
"""

import socket
import struct
import threading
import time
import unittest

from tw1mp import relay


def _recv(sock, timeout=1.0):
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(relay._MAX_DGRAM)
    magic, type_, session, src, dst = relay._HDR.unpack_from(data, 0)
    assert magic == relay.RL_MAGIC
    return type_, session, src, dst, data[relay._HDR_LEN:]


def _send(sock, addr, type_, session, src, dst, payload=b''):
    sock.sendto(relay._pack(type_, session, src, dst, payload), addr)


class TestRelay(unittest.TestCase):
    def setUp(self):
        # Bind an ephemeral port on loopback so tests never collide.
        self.srv = relay.RelayServer(bind='127.0.0.1', port=0)
        self.srv.open()
        self.addr = self.srv.sock.getsockname()
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.socks = []

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=2)
        self.srv.server_close()
        for s in self.socks:
            s.close()

    def _client(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        self.socks.append(s)
        return s

    def _host_session(self):
        """HELLO a host and return (socket, session_id)."""
        host = self._client()
        _send(host, self.addr, relay.RL_HELLO, 0, 0, 0,
              bytes([relay.ROLE_HOST]))
        type_, session, src, _dst, _p = _recv(host)
        self.assertEqual(type_, relay.RL_HELLO_OK)
        self.assertNotEqual(session, 0)
        self.assertEqual(src, 1)  # host is slot 1
        return host, session

    def _join_session(self, session, expect_slot):
        joiner = self._client()
        _send(joiner, self.addr, relay.RL_HELLO, session, 0, 0,
              bytes([relay.ROLE_JOIN]))
        type_, sess2, src, _dst, _p = _recv(joiner)
        self.assertEqual(type_, relay.RL_HELLO_OK)
        self.assertEqual(sess2, session)
        self.assertEqual(src, expect_slot)
        return joiner

    def test_host_gets_session_and_slot1(self):
        _host, session = self._host_session()
        self.assertNotEqual(session, 0)

    def test_join_unknown_session_errors(self):
        joiner = self._client()
        _send(joiner, self.addr, relay.RL_HELLO, 0xDEADBEEF, 0, 0,
              bytes([relay.ROLE_JOIN]))
        type_, _s, _src, _dst, reason = _recv(joiner)
        self.assertEqual(type_, relay.RL_ERROR)
        self.assertIn(b'no such session', reason)

    def test_data_forwarded_host_to_joiner(self):
        host, session = self._host_session()
        joiner = self._join_session(session, 2)
        # Host broadcasts (dst=0): joiner must receive it, tagged src=1.
        _send(host, self.addr, relay.RL_DATA, session, 1, 0, b'hello-joiner')
        type_, sess2, src, _dst, payload = _recv(joiner)
        self.assertEqual(type_, relay.RL_DATA)
        self.assertEqual(sess2, session)
        self.assertEqual(src, 1)
        self.assertEqual(payload, b'hello-joiner')

    def test_data_forwarded_joiner_to_host(self):
        host, session = self._host_session()
        joiner = self._join_session(session, 2)
        _send(joiner, self.addr, relay.RL_DATA, session, 2, 0, b'hi-host')
        type_, _s, src, _dst, payload = _recv(host)
        self.assertEqual(type_, relay.RL_DATA)
        self.assertEqual(src, 2)
        self.assertEqual(payload, b'hi-host')

    def test_broadcast_reaches_all_other_members(self):
        host, session = self._host_session()
        j2 = self._join_session(session, 2)
        j3 = self._join_session(session, 3)
        _send(host, self.addr, relay.RL_DATA, session, 1, 0, b'all')
        for j in (j2, j3):
            type_, _s, src, _dst, payload = _recv(j)
            self.assertEqual(type_, relay.RL_DATA)
            self.assertEqual(src, 1)
            self.assertEqual(payload, b'all')

    def test_directed_send_only_to_target_slot(self):
        host, session = self._host_session()
        j2 = self._join_session(session, 2)
        j3 = self._join_session(session, 3)
        # Host addresses slot 3 only.
        _send(host, self.addr, relay.RL_DATA, session, 1, 3, b'just-3')
        type_, _s, src, dst, payload = _recv(j3)
        self.assertEqual(payload, b'just-3')
        self.assertEqual(dst, 3)
        # j2 must NOT get it.
        j2.settimeout(0.4)
        with self.assertRaises(socket.timeout):
            j2.recvfrom(relay._MAX_DGRAM)

    def test_rehello_keeps_same_slot(self):
        host, session = self._host_session()
        joiner = self._join_session(session, 2)
        _send(joiner, self.addr, relay.RL_HELLO, session, 0, 0,
              bytes([relay.ROLE_JOIN]))
        type_, _s, src, _dst, _p = _recv(joiner)
        self.assertEqual(type_, relay.RL_HELLO_OK)
        self.assertEqual(src, 2)  # unchanged

    def test_second_host_refused(self):
        _host, session = self._host_session()
        other = self._client()
        _send(other, self.addr, relay.RL_HELLO, session, 0, 0,
              bytes([relay.ROLE_HOST]))
        type_, _s, _src, _dst, reason = _recv(other)
        self.assertEqual(type_, relay.RL_ERROR)
        self.assertIn(b'host exists', reason)

    def test_session_full(self):
        host, session = self._host_session()
        for slot in range(2, relay.MAX_SLOTS + 1):
            self._join_session(session, slot)
        overflow = self._client()
        _send(overflow, self.addr, relay.RL_HELLO, session, 0, 0,
              bytes([relay.ROLE_JOIN]))
        type_, _s, _src, _dst, reason = _recv(overflow)
        self.assertEqual(type_, relay.RL_ERROR)
        self.assertIn(b'session full', reason)

    def test_joiner_bye_notifies_others(self):
        host, session = self._host_session()
        j2 = self._join_session(session, 2)
        j3 = self._join_session(session, 3)
        _send(j2, self.addr, relay.RL_BYE, session, 2, 0)
        for s in (host, j3):
            type_, sess2, src, _dst, _p = _recv(s, timeout=2)
            self.assertEqual(type_, relay.RL_LEFT)
            self.assertEqual(sess2, session)
            self.assertEqual(src, 2)  # the departed member's id

    def test_member_ids_never_reused(self):
        # A departed joiner's id must not come back for the next joiner: the
        # DLLs use the id as the DirectPlay player id for the whole session.
        host, session = self._host_session()
        j2 = self._join_session(session, 2)
        _send(j2, self.addr, relay.RL_BYE, session, 2, 0)
        _recv(host, timeout=2)  # RL_LEFT
        self._join_session(session, 3)

    def test_timeout_notifies_others(self):
        old = relay.MEMBER_TIMEOUT
        relay.MEMBER_TIMEOUT = 1.0
        try:
            host, session = self._host_session()
            _joiner = self._join_session(session, 2)
            # Keep the host alive (ping well inside the timeout), let the
            # joiner go silent; the next reap must report it as gone.
            deadline = time.time() + 2 * relay._REAP_INTERVAL + 2
            got = None
            while time.time() < deadline and got is None:
                _send(host, self.addr, relay.RL_PING, session, 1, 0)
                try:
                    got = _recv(host, timeout=0.2)
                except socket.timeout:
                    pass
            self.assertIsNotNone(got, 'no RL_LEFT after joiner timeout')
            self.assertEqual(got[0], relay.RL_LEFT)
            self.assertEqual(got[2], 2)
        finally:
            relay.MEMBER_TIMEOUT = old

    def test_host_bye_closes_session_for_joiners(self):
        host, session = self._host_session()
        joiner = self._join_session(session, 2)
        _send(host, self.addr, relay.RL_BYE, session, 1, 0)
        type_, _s, _src, _dst, reason = _recv(joiner, timeout=2)
        self.assertEqual(type_, relay.RL_ERROR)
        self.assertIn(b'host left', reason)

    def test_stats_count_forwarding(self):
        host, session = self._host_session()
        self._join_session(session, 2)
        _send(host, self.addr, relay.RL_DATA, session, 1, 0, b'x')
        time.sleep(0.2)
        st = self.srv.stats()
        self.assertGreaterEqual(st['forwarded'], 1)
        self.assertEqual(st['sessions'], 1)

    def _hello_token(self, sock, session, role):
        _send(sock, self.addr, relay.RL_HELLO, session, 0, 0, bytes([role]))
        type_, sess, src, _dst, payload = _recv(sock)
        self.assertEqual(type_, relay.RL_HELLO_OK)
        self.assertEqual(len(payload), 4, 'HELLO_OK carries a 4-byte token')
        return sess, src, payload

    def test_ping_with_payload_is_echoed(self):
        host, session = self._host_session()
        _send(host, self.addr, relay.RL_PING, session, 1, 0, b'\x01\x02\x03\x04')
        type_, sess, src, _dst, payload = _recv(host)
        self.assertEqual((type_, sess, src, payload),
                         (relay.RL_PING, session, 1, b'\x01\x02\x03\x04'))

    def test_empty_ping_is_not_echoed(self):
        # Older DLLs send empty pings and do not expect an answer.
        host, session = self._host_session()
        _send(host, self.addr, relay.RL_PING, session, 1, 0)
        with self.assertRaises(socket.timeout):
            _recv(host, timeout=0.4)

    def test_rebind_moves_member_to_new_address(self):
        host, session = self._host_session()
        old = self._client()
        _sess, slot, token = self._hello_token(old, session, relay.ROLE_JOIN)
        # Same member, new NAT port: the relay does not know the address yet
        # and asks for a rebind instead of dropping the packet silently.
        new = self._client()
        _send(new, self.addr, relay.RL_DATA, session, slot, 0, b'lost?')
        type_, _s, _src, _dst, reason = _recv(new)
        self.assertEqual((type_, reason), (relay.RL_ERROR, b'rebind'))
        _send(new, self.addr, relay.RL_HELLO, session, slot, 0,
              bytes([relay.ROLE_REBIND]) + token)
        type_, _s, src, _dst, token2 = _recv(new)
        self.assertEqual((type_, src, token2), (relay.RL_HELLO_OK, slot, token))
        _send(new, self.addr, relay.RL_DATA, session, slot, 1, b'after-move')
        type_, _s, src, _dst, payload = _recv(host)
        self.assertEqual((type_, src, payload), (relay.RL_DATA, slot, b'after-move'))
        # Traffic for the member now goes to the new address only.
        _send(host, self.addr, relay.RL_DATA, session, 1, slot, b'to-new')
        self.assertEqual(_recv(new)[4], b'to-new')
        with self.assertRaises(socket.timeout):
            _recv(old, timeout=0.4)

    def test_rebind_with_wrong_token_refused(self):
        _host, session = self._host_session()
        old = self._client()
        _sess, slot, token = self._hello_token(old, session, relay.ROLE_JOIN)
        bad = bytes([token[0] ^ 0xFF]) + token[1:]
        thief = self._client()
        _send(thief, self.addr, relay.RL_HELLO, session, slot, 0,
              bytes([relay.ROLE_REBIND]) + bad)
        type_, _s, _src, _dst, reason = _recv(thief)
        self.assertEqual((type_, reason), (relay.RL_ERROR, b'rebind refused'))

    def test_unknown_address_with_unknown_slot_stays_silent(self):
        _host, session = self._host_session()
        stranger = self._client()
        _send(stranger, self.addr, relay.RL_DATA, session, 7, 0, b'x')
        with self.assertRaises(socket.timeout):
            _recv(stranger, timeout=0.4)


class TestRelayImpairment(unittest.TestCase):
    """The test-only delay/loss knobs the DLL self-test drives."""

    def _run(self, **kw):
        srv = relay.RelayServer(bind='127.0.0.1', port=0, **kw)
        srv.open()
        addr = srv.sock.getsockname()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        socks = []
        try:
            host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); socks.append(host)
            host.bind(('127.0.0.1', 0))
            _send(host, addr, relay.RL_HELLO, 0, 0, 0, bytes([relay.ROLE_HOST]))
            session = _recv(host)[1]
            j = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); socks.append(j)
            j.bind(('127.0.0.1', 0))
            _send(j, addr, relay.RL_HELLO, session, 0, 0, bytes([relay.ROLE_JOIN]))
            _recv(j)
            return srv, addr, session, host, j
        except Exception:
            srv.shutdown(); srv.server_close()
            raise
        finally:
            self.addCleanup(lambda: [s.close() for s in socks])
            self.addCleanup(lambda: (srv.shutdown(), t.join(timeout=2), srv.server_close()))

    def test_delay_holds_data_back(self):
        _srv, addr, session, host, j = self._run(delay_ms=200)
        t0 = time.time()
        _send(host, addr, relay.RL_DATA, session, 1, 0, b'late')
        self.assertEqual(_recv(j, timeout=2)[4], b'late')
        self.assertGreaterEqual(time.time() - t0, 0.18)

    def test_loss_drops_data(self):
        _srv, addr, session, host, j = self._run(loss=1.0)
        _send(host, addr, relay.RL_DATA, session, 1, 0, b'gone')
        with self.assertRaises(socket.timeout):
            _recv(j, timeout=0.4)


if __name__ == '__main__':
    unittest.main()
