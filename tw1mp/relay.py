"""UDP game-session relay for Two Worlds 1 multiplayer.

The original game runs its play session peer-to-peer over DirectPlay: the
host advertises its own address and the joiners connect straight to it. On
the open internet that needs port-forwarding or a VPN on the hosting
player's side, and even on a LAN it depends on every player accepting an
inbound socket. This relay removes that: both the host and the joiners make
only *outbound* UDP connections to this server, which forwards packets
between the members of a session. It is the same idea the official servers'
NAT resolver used, kept deliberately small.

The relay is transport-only. It never inspects or needs to understand the
DirectPlay payload the two `dpnetrelay` DLLs exchange - it just moves the
opaque inner bytes between the members of a session, keyed by a session id
it hands out. The reliability/ordering the game needs is provided end-to-end
by the DLLs themselves; the relay may drop a datagram exactly like the
internet would.

Wire format (little-endian), every datagram DLL <-> relay is::

    magic  u32   'TWRL' (0x4C525754)
    type   u8    RL_* below
    session u32  0 in a host's first HELLO (relay assigns one)
    src    u16   sender member id (host = 1), 0 if not yet known
    dst    u16   destination member id, 0 = every other member of the session
    <payload>    RL_DATA: the opaque DirectPlay packet to forward

Member ids are handed out by the relay: the host is always 1, every joiner
gets the next unused number for the life of the session (ids are never
reused, so the DLLs can use them directly as DirectPlay player ids without
a departed player's id ever coming back for someone else). At most
MAX_SLOTS members are in a session at any one time.

Types:
    RL_HELLO(1)     member joins/refreshes. payload[0] = role (1 host, 2 join,
                    3 rebind: payload[1:5] = token, src = member id)
    RL_HELLO_OK(2)  relay -> member. session + assigned member id (in src),
                    payload = u32 token for a later rebind
    RL_DATA(3)      forward payload to dst (or all others)
    RL_BYE(4)       member leaves; if the host leaves the session ends
    RL_PING(5)      keepalive to hold the NAT mapping open. A ping with a
                    payload is echoed back unchanged (the DLL measures its
                    round trip and notices a dead relay)
    RL_ERROR(6)     relay -> member. payload = ascii reason (e.g. session gone).
                    'rebind' = your packets come from an address this session
                    does not know (NAT changed the mapping): HELLO with role 3
    RL_LEFT(7)      relay -> remaining members: member `src` left or timed out

This module has no third-party dependencies and can run either inside the
lobby server (started by CoreServer when [Relay] enabled) or standalone on
any machine - e.g. on the hosting player's PC for a purely local LAN game::

    python -m tw1mp.relay --port 17172
"""

import logging
import os
import heapq
import random
import secrets
import socket
import struct
import threading
import time

log = logging.getLogger('tw1mp.relay')

RL_MAGIC = 0x4C525754  # 'TWRL'
RL_HELLO = 1
RL_HELLO_OK = 2
RL_DATA = 3
RL_BYE = 4
RL_PING = 5
RL_ERROR = 6
RL_LEFT = 7

ROLE_HOST = 1
ROLE_JOIN = 2
ROLE_REBIND = 3

MAX_SLOTS = 8            # Two Worlds allows up to 8 players in a game
_HDR = struct.Struct('<IBIHH')   # magic, type, session, src, dst
_HDR_LEN = _HDR.size

# A member is dropped after this long without any packet from it; a whole
# session is reaped once empty. Chosen well above the DLL's 2s keepalive.
MEMBER_TIMEOUT = 20.0
_REAP_INTERVAL = 5.0
# How long recvfrom blocks before the loop rechecks the stop flag / reaps.
# Kept small so shutdown() is responsive; reaping still runs every
# _REAP_INTERVAL by wall clock, not every wake-up.
_POLL_INTERVAL = 0.5
_MAX_DGRAM = 65535


def _pack(type_, session, src, dst, payload=b''):
    return _HDR.pack(RL_MAGIC, type_, session, src, dst) + payload


def _token(member):
    return struct.pack('<I', member.token)


class _Member:
    __slots__ = ('slot', 'addr', 'role', 'last_seen', 'token')

    def __init__(self, slot, addr, role, now):
        self.slot = slot
        self.addr = addr
        self.role = role
        self.last_seen = now
        # Proves a later rebind comes from this member and not from anyone
        # who merely knows the session id and slot.
        self.token = secrets.randbits(32)


class _Session:
    """One game session: a host (member 1) plus up to MAX_SLOTS-1 joiners."""

    _last_progress = 0.0

    def __init__(self, session_id):
        self.id = session_id
        self.members = {}          # member id -> _Member
        self.by_addr = {}          # addr -> member id
        self.next_id = 2           # host is 1; ids are never reused
        self.fwd_from = {}         # member id -> datagrams forwarded from it

    def traffic(self):
        return ' '.join('%d:%d' % kv for kv in sorted(self.fwd_from.items())) or '-'

    def alloc_id(self):
        """Next member id, or None when the session is at capacity."""
        if len(self.members) >= MAX_SLOTS or self.next_id > 0xFFFF:
            return None
        mid = self.next_id
        self.next_id += 1
        return mid


class RelayServer:
    """Session-keyed UDP packet relay. Thread-safe; one background reaper."""

    def __init__(self, bind='', port=17172, delay_ms=0, jitter_ms=0, loss=0.0):
        self.bind = bind
        self.port = port
        self.sock = None
        self._sessions = {}        # session_id -> _Session
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rx = 0
        self._fwd = 0
        # Test impairment for forwarded RL_DATA only (self-tests of the DLL):
        # one-way delay with uniform jitter and random loss. Off by default.
        self.delay_ms = delay_ms
        self.jitter_ms = jitter_ms
        self.loss = loss
        self._later = []           # heap of (due, seq, data, addr)
        self._later_seq = 0
        self._later_cv = threading.Condition()
        self._later_thread = None

    # -- lifecycle ------------------------------------------------------

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.bind, self.port))
        self.sock.settimeout(_POLL_INTERVAL)
        log.info('Game relay listening on UDP %s:%s',
                 self.bind or '0.0.0.0', self.port)

    def serve_forever(self):
        if self.sock is None:
            self.open()
        if (self.delay_ms or self.jitter_ms) and self._later_thread is None:
            self._later_thread = threading.Thread(target=self._later_loop, daemon=True)
            self._later_thread.start()
        last_reap = time.time()
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(_MAX_DGRAM)
            except socket.timeout:
                data = None
            except ConnectionResetError:
                # Windows reports an ICMP "port unreachable" for an earlier
                # sendto (a member whose socket is gone) on the next receive.
                # It says nothing about this socket; keep serving.
                data = None
            except OSError:
                break
            if data:
                try:
                    self._handle(data, addr)
                except Exception:
                    log.exception('Relay packet handling failed')
            now = time.time()
            if now - last_reap >= _REAP_INTERVAL:
                self._reap(now)
                last_reap = now
                self._progress(now)

    _PROGRESS_INTERVAL = 30.0

    def _progress(self, now):
        # A heartbeat line per active session, so a live log shows whether
        # data is actually moving in both directions.
        with self._lock:
            for sess in self._sessions.values():
                if len(sess.members) < 2:
                    continue
                last = getattr(sess, '_last_progress', 0)
                if now - last >= self._PROGRESS_INTERVAL:
                    sess._last_progress = now
                    log.info('Relay session %08X: %d members, datagrams '
                             'forwarded per member: %s', sess.id,
                             len(sess.members), sess.traffic())

    def shutdown(self):
        self._stop.set()
        with self._later_cv:
            self._later_cv.notify_all()

    def server_close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- packet handling ------------------------------------------------

    def _handle(self, data, addr):
        if len(data) < _HDR_LEN:
            return
        magic, type_, session, src, dst = _HDR.unpack_from(data, 0)
        if magic != RL_MAGIC:
            return
        payload = data[_HDR_LEN:]
        self._rx += 1
        if type_ == RL_HELLO:
            self._on_hello(session, addr, payload, src)
        elif type_ == RL_DATA:
            self._on_data(session, addr, src, dst, payload)
        elif type_ == RL_PING:
            self._touch(session, addr, src, payload)
        elif type_ == RL_BYE:
            self._on_bye(session, addr)
        # RL_HELLO_OK / RL_ERROR are relay->member only; ignore if received.

    def _on_hello(self, session, addr, payload, src=0):
        role = payload[0] if payload else ROLE_JOIN
        now = time.time()
        if role == ROLE_REBIND:
            self._on_rebind(session, addr, src, payload)
            return
        with self._lock:
            if role == ROLE_HOST and session == 0:
                # New host: allocate a fresh session id and slot 1.
                session = self._new_session_id()
                sess = _Session(session)
                self._sessions[session] = sess
                m = _Member(1, addr, ROLE_HOST, now)
                sess.members[1] = m
                sess.by_addr[addr] = 1
                log.info('Relay session %08X opened by host %s:%s',
                         session, *addr)
                self._send(RL_HELLO_OK, session, 1, 0, addr, _token(m))
                return

            sess = self._sessions.get(session)
            if sess is None:
                self._send(RL_ERROR, session, 0, 0, addr, b'no such session')
                return

            # Re-HELLO from a known address just refreshes and re-acks.
            slot = sess.by_addr.get(addr)
            if slot is not None:
                sess.members[slot].last_seen = now
                self._send(RL_HELLO_OK, session, slot, 0, addr,
                           _token(sess.members[slot]))
                return

            if role == ROLE_HOST:
                # A second host claim on an existing session is refused.
                self._send(RL_ERROR, session, 0, 0, addr, b'host exists')
                return

            slot = sess.alloc_id()
            if slot is None:
                self._send(RL_ERROR, session, 0, 0, addr, b'session full')
                return
            m = _Member(slot, addr, ROLE_JOIN, now)
            sess.members[slot] = m
            sess.by_addr[addr] = slot
            log.info('Relay session %08X: joiner %s:%s -> member %d',
                     session, addr[0], addr[1], slot)
            self._send(RL_HELLO_OK, session, slot, 0, addr, _token(m))

    def _on_rebind(self, session, addr, src, payload):
        # A member whose NAT mapping changed: same session and slot, new
        # address, proven by the token from its HELLO_OK.
        with self._lock:
            sess = self._sessions.get(session)
            if sess is None:
                self._send(RL_ERROR, session, 0, 0, addr, b'no such session')
                return
            token = struct.unpack_from('<I', payload, 1)[0] if len(payload) >= 5 else None
            m = sess.members.get(src)
            if m is None or token is None or token != m.token:
                self._send(RL_ERROR, session, 0, 0, addr, b'rebind refused')
                return
            if m.addr != addr:
                sess.by_addr.pop(m.addr, None)
                old = m.addr
                m.addr = addr
                sess.by_addr[addr] = m.slot
                log.info('Relay session %08X: member %d moved %s:%s -> %s:%s',
                         session, m.slot, old[0], old[1], addr[0], addr[1])
            m.last_seen = time.time()
            self._send(RL_HELLO_OK, session, m.slot, 0, addr, _token(m))

    def _ask_rebind(self, sess, src, addr):
        # Caller holds the lock. A packet from an unknown address that names
        # an existing member: most likely that member behind a new NAT port.
        if src in sess.members:
            self._send(RL_ERROR, sess.id, 0, 0, addr, b'rebind')

    def _on_data(self, session, addr, src, dst, payload):
        with self._lock:
            sess = self._sessions.get(session)
            if sess is None:
                return
            src_slot = sess.by_addr.get(addr)
            if src_slot is None:
                self._ask_rebind(sess, src, addr)
                return  # unknown sender: must HELLO (or rebind) first
            sess.members[src_slot].last_seen = time.time()
            if dst and dst in sess.members:
                targets = [sess.members[dst]]
            else:
                targets = [m for s, m in sess.members.items() if s != src_slot]
            sess.fwd_from[src_slot] = sess.fwd_from.get(src_slot, 0) + 1
            out = _pack(RL_DATA, session, src_slot, dst, payload)
        for m in targets:
            self._forward(out, m.addr)
            self._fwd += 1

    def _on_bye(self, session, addr):
        with self._lock:
            sess = self._sessions.get(session)
            if sess is None:
                return
            slot = sess.by_addr.pop(addr, None)
            if slot is None:
                return
            member = sess.members.pop(slot, None)
            log.info('Relay session %08X: member %d left (datagrams forwarded per member: %s)',
                     session, slot, sess.traffic())
            if member and member.role == ROLE_HOST:
                self._close_session(sess, 'host left')
            elif not sess.members:
                self._sessions.pop(session, None)
            else:
                self._notify_left(sess, slot)

    def _touch(self, session, addr, src=0, payload=b''):
        with self._lock:
            sess = self._sessions.get(session)
            if sess is None:
                return
            slot = sess.by_addr.get(addr)
            if slot is None:
                self._ask_rebind(sess, src, addr)
                return
            sess.members[slot].last_seen = time.time()
            if payload:
                # Echo: the DLL measures its round trip to us and notices
                # when we go silent. Old DLLs send empty pings, no echo.
                self._send(RL_PING, session, slot, 0, addr, payload)

    # -- reaping --------------------------------------------------------

    def _reap(self, now):
        with self._lock:
            for session in list(self._sessions):
                sess = self._sessions.get(session)
                if not sess:
                    continue
                dead = [s for s, m in sess.members.items()
                        if now - m.last_seen > MEMBER_TIMEOUT]
                host_died = False
                for slot in dead:
                    m = sess.members.pop(slot, None)
                    if m:
                        sess.by_addr.pop(m.addr, None)
                        log.info('Relay session %08X: member %d timed out '
                                 '(datagrams forwarded per member: %s)',
                                 session, slot, sess.traffic())
                        if m.role == ROLE_HOST:
                            host_died = True
                if host_died:
                    self._close_session(sess, 'host timed out')
                elif not sess.members:
                    self._sessions.pop(session, None)
                else:
                    for slot in dead:
                        self._notify_left(sess, slot)

    def _notify_left(self, sess, departed):
        # A joiner is gone: the others need to drop that player. The DLLs
        # would eventually notice the silence themselves, but the relay
        # knows first and tells them at once.
        for m in list(sess.members.values()):
            self._send(RL_LEFT, sess.id, departed, 0, m.addr)

    def _close_session(self, sess, reason):
        # Tell every remaining member the session is gone, then drop it.
        for m in list(sess.members.values()):
            self._send(RL_ERROR, sess.id, 0, 0, m.addr,
                       reason.encode('ascii', 'ignore'))
        self._sessions.pop(sess.id, None)
        log.info('Relay session %08X closed (%s; datagrams forwarded per member: %s)',
                 sess.id, reason, sess.traffic())

    # -- helpers --------------------------------------------------------

    def _new_session_id(self):
        while True:
            sid = random.getrandbits(32)
            if sid != 0 and sid not in self._sessions:
                return sid

    def _send(self, type_, session, src, dst, addr, payload=b''):
        self._raw(_pack(type_, session, src, dst, payload), addr)

    def _raw(self, data, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _forward(self, data, addr):
        if self.loss and random.random() < self.loss:
            return
        if not (self.delay_ms or self.jitter_ms):
            self._raw(data, addr)
            return
        due = time.time() + max(0.0, self.delay_ms + random.uniform(
            -self.jitter_ms, self.jitter_ms)) / 1000.0
        with self._later_cv:
            self._later_seq += 1
            heapq.heappush(self._later, (due, self._later_seq, data, addr))
            self._later_cv.notify()

    def _later_loop(self):
        while not self._stop.is_set():
            with self._later_cv:
                while not self._later and not self._stop.is_set():
                    self._later_cv.wait(0.5)
                if self._stop.is_set():
                    return
                due, _seq, data, addr = self._later[0]
                wait = due - time.time()
                if wait > 0:
                    self._later_cv.wait(wait)
                    continue
                heapq.heappop(self._later)
            self._raw(data, addr)

    def stats(self):
        with self._lock:
            return {
                'sessions': len(self._sessions),
                'members': sum(len(s.members) for s in self._sessions.values()),
                'received': self._rx,
                'forwarded': self._fwd,
            }


def run(bind='', port=17172):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    srv = RelayServer(bind, port)
    srv.open()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Two Worlds 1 game-session relay')
    p.add_argument('--bind', default=os.environ.get('TW1MP_RELAY_BIND', ''),
                   help='interface to bind (default: all)')
    p.add_argument('--port', type=int,
                   default=int(os.environ.get('TW1MP_RELAY_PORT', '17172')),
                   help='UDP port (default: 17172)')
    args = p.parse_args()
    run(args.bind, args.port)
