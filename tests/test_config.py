import os
import tempfile
import unittest

from tw1mp import config
from tw1mp.config import Config


class TestConfig(unittest.TestCase):
    def test_defaults_written_and_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(root=tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, 'Config.ini')))
            self.assertEqual(cfg.port, 17171)
            self.assertTrue(cfg.auto_register)
            self.assertTrue(cfg.send_nops)
            self.assertFalse(cfg.compat_login_errors)
            self.assertEqual(len(cfg.maps), 4)
            # \r\n escape sequence is unescaped on load
            self.assertTrue(cfg.motd.endswith('\r\n'))
            self.assertNotIn('\\r', cfg.motd)

    def test_malformed_values_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'Config.ini'), 'w') as f:
                f.write('[Server]\nport = abc\nauto_register = maybe\n'
                        'channels_per_map = many\n'
                        '[Web]\nenabled = yes-ish\n')
            cfg = Config(root=tmp)  # must not raise
            self.assertEqual(cfg.port, 17171)
            self.assertTrue(cfg.auto_register)
            self.assertEqual(cfg.channels_per_map, 1)
            self.assertFalse(cfg.web_enabled)

    def test_channels_per_map_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'Config.ini'), 'w') as f:
                f.write('[Server]\nchannels_per_map = 99\n')
            self.assertEqual(Config(root=tmp).channels_per_map, 20)


if __name__ == '__main__':
    unittest.main()


class TestAdminPassword(unittest.TestCase):
    """The panel's admin gate. A convenience lock, but it must at least not
    store the password in the clear and must reject wrong input."""

    def test_roundtrip(self):
        stored = config.hash_password('geheim')
        self.assertTrue(config.check_password('geheim', stored))
        self.assertFalse(config.check_password('falsch', stored))

    def test_not_stored_in_clear(self):
        stored = config.hash_password('geheim')
        self.assertNotIn('geheim', stored)
        self.assertIn('$', stored)          # salt$digest

    def test_salted_so_equal_passwords_differ(self):
        self.assertNotEqual(config.hash_password('x'), config.hash_password('x'))

    def test_empty_or_missing_never_passes(self):
        for stored in ('', None, 'garbage'):
            self.assertFalse(config.check_password('anything', stored))

    def test_panel_defaults_to_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(config.Config(root=tmp).panel_mode, 'client')
