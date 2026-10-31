"""The admin dashboard API: write gate, settings round trip, downloads."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from tw1mp.config import Config
from tw1mp.server import CoreServer


class TestWebAdmin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # in the ini, not on the object: saving settings reloads the file
        with open(os.path.join(self.tmp.name, 'Config.ini'), 'w') as f:
            f.write('[Server]\nport = 0\nbind = 127.0.0.1\n'
                    'bot_enabled = false\nsend_nops = false\n'
                    '[Web]\nenabled = true\nbind = 127.0.0.1\nport = 0\n'
                    'admin_api = true\npanel_hosts = panel.example\n')
        cfg = Config(root=self.tmp.name)
        self.server = CoreServer(cfg)
        self.web_port = self.server.webapi.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.2},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.db.close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        con = http.client.HTTPConnection('127.0.0.1', self.web_port,
                                         timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            con.request(method, '/' + path, body=data, headers=headers or {})
            resp = con.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            con.close()

    def _post(self, path, body, origin=None, host=None, admin=True):
        headers = {'Content-Type': 'application/json'}
        if admin:
            headers['X-TW1MP-Admin'] = '1'
        if origin:
            headers['Origin'] = origin
        if host:
            headers['Host'] = host
        return self._request('POST', path, body, headers)

    def test_write_needs_the_admin_header(self):
        status, _h, body = self._post('admin/config', {'settings': {}},
                                      admin=False)
        self.assertEqual(status, 403)
        self.assertIn(b'missing admin header', body)

    def test_localhost_and_configured_proxy_origins_are_accepted(self):
        for origin in ('http://localhost:%d' % self.web_port,
                       'http://127.0.0.1:%d' % self.web_port,
                       'https://panel.example'):
            status, _h, body = self._post('admin/config', {'settings': {}},
                                          origin=origin)
            self.assertEqual(status, 200, (origin, body))

    def test_rebinding_origin_matching_its_own_host_is_refused(self):
        # A DNS-rebinding page is same-origin with the Host it sends.
        status, _h, body = self._post('admin/config', {'settings': {}},
                                      origin='http://evil.example:17071',
                                      host='evil.example:17071')
        self.assertEqual(status, 403)
        self.assertIn(b'bad origin', body)

    def test_settings_round_trip(self):
        status, _h, body = self._post(
            'admin/config', {'settings': {'Server.bot_website': 'https://x.example/',
                                          'Server.send_nops': True,
                                          'Server.unknown': 'ignored'}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.server.config.bot_website, 'https://x.example/')
        status, _h, body = self._request('GET', 'config')
        self.assertEqual(status, 200)
        doc = json.loads(body)
        values = {(f['section'], f['key']): f['value']
                  for g in doc['groups'] for f in g['fields']}
        self.assertEqual(values[('Server', 'bot_website')],
                         'https://x.example/')
        self.assertNotIn(('Server', 'bot_password'), values)

    def test_bad_int_is_refused(self):
        status, _h, body = self._post('admin/config',
                                      {'settings': {'Server.port': 'abc'}})
        self.assertEqual(status, 400)

    def test_download_filename_is_sanitised(self):
        self.assertEqual(self.server.db.register('alice', b'\x01' * 8, 'pw'),
                         0)
        self.assertTrue(self.server.db.save_snapshot('alice', 'slot', b'abc'))
        status, headers, body = self._request(
            'GET', 'snapshot/download?name=alice&snap=slot')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'abc')
        self.assertEqual(headers['Content-Disposition'],
                         'attachment; filename="alice_slot.bin"')


class TestPanelHostsConfig(unittest.TestCase):
    def test_panel_hosts_parsed_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'Config.ini'), 'w') as f:
                f.write('[Web]\npanel_hosts = Panel.Example, b.example ,\n')
            cfg = Config(root=tmp)
            self.assertEqual(cfg.web_panel_hosts,
                             {'panel.example', 'b.example'})


if __name__ == '__main__':
    unittest.main()
