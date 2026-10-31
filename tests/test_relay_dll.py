"""End-to-end test of the relay DLL against the Python relay.

Starts a RelayServer on a loopback port, then runs the compiled self-test
``directplay-relay/test_relay.exe`` (host + two joiners in one process)
with ``TW1MP_RELAY`` pointing at it. Skipped when the exe has not been built
(``directplay-relay/build.bat`` + ``build-test.bat``) or off Windows.
"""

import os
import subprocess
import sys
import threading
import unittest

from tw1mp import relay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, 'directplay-relay', 'test_relay.exe')
DLL = os.path.join(ROOT, 'directplay-relay', 'dpnetrelay.dll')


@unittest.skipUnless(sys.platform == 'win32' and os.path.exists(EXE)
                     and os.path.exists(DLL),
                     'needs directplay-relay/test_relay.exe + dpnetrelay.dll')
class TestRelayDll(unittest.TestCase):
    def _relay(self, **kw):
        srv = relay.RelayServer(bind='127.0.0.1', port=0, **kw)
        srv.open()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        def stop():
            srv.shutdown()
            thread.join(timeout=2)
            srv.server_close()
        self.addCleanup(stop)
        env = dict(os.environ, TW1MP_RELAY='127.0.0.1:%d' % srv.sock.getsockname()[1])
        return srv, env, stop

    def test_three_peers_end_to_end(self):
        srv, env, _stop = self._relay()
        proc = subprocess.run([EXE], cwd=os.path.dirname(EXE), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         'self-test failed:\n' + proc.stdout + proc.stderr)
        self.assertIn('=== RESULT: PASS', proc.stdout)
        st = srv.stats()
        self.assertGreater(st['forwarded'], 0)

    def test_relay_dies_mid_session(self):
        _srv, env, stop = self._relay()
        proc = subprocess.Popen([EXE, 'relaykill'], cwd=os.path.dirname(EXE), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        try:
            for line in proc.stdout:
                out.append(line)
                if 'READY_KILL_RELAY' in line:
                    stop()
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        text = ''.join(out)
        self.assertEqual(proc.returncode, 0, 'relaykill failed:\n' + text)
        self.assertIn('=== RESULT: PASS', text)

    def test_slow_lossy_relay(self):
        _srv, env, _stop = self._relay(delay_ms=100, jitter_ms=25, loss=0.05)
        proc = subprocess.run([EXE, 'impaired'], cwd=os.path.dirname(EXE), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         'impaired failed:\n' + proc.stdout + proc.stderr)
        self.assertIn('=== RESULT: PASS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
