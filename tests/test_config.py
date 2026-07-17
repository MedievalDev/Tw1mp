import os
import tempfile
import unittest

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
