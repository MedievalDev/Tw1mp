import struct
import unittest
import zlib

from tw1mp import protocol


class TestDstr(unittest.TestCase):
    def test_roundtrip(self):
        data = protocol.make_dstr('Hello') + protocol.make_dstr('Wörld')
        text, off = protocol.parse_dstr(data, 0)
        self.assertEqual(text, 'Hello')
        text, off = protocol.parse_dstr(data, off)
        self.assertEqual(text, 'Wörld')
        self.assertEqual(off, len(data))

    def test_empty(self):
        text, off = protocol.parse_dstr(protocol.make_dstr(''), 0)
        self.assertEqual(text, '')
        self.assertEqual(off, 4)


class TestPackets(unittest.TestCase):
    def _unwrap(self, packet):
        [plen] = struct.unpack('<I', packet[0:4])
        self.assertEqual(plen, len(packet))
        return zlib.decompress(packet[4:])

    def test_server_info(self):
        res = self._unwrap(protocol.server_info_packet('TW1MP'))
        [err] = struct.unpack('<I', res[0:4])
        self.assertEqual(err, 0)
        info, _ = protocol.parse_dstr(res, 4)
        self.assertEqual(info, '+"TW1MP""TWMP2;10.0.0.5"')

    def test_login_error(self):
        res = self._unwrap(protocol.login_error_packet('nope'))
        [err] = struct.unpack('<I', res[0:4])
        self.assertEqual(err, 1)
        msg, _ = protocol.parse_dstr(res, 4)
        self.assertEqual(msg, 'nope')

    def test_welcome_layout(self):
        serial = bytes(range(8))
        res = self._unwrap(protocol.server_welcome_packet(
            serial, 'Title', 'MOTD'))
        [err] = struct.unpack('<I', res[0:4])
        self.assertEqual(err, 0)
        self.assertEqual(res[4:8], bytes([0x55, 0xA6, 0xD8, 0x3B]))
        title, off = protocol.parse_dstr(res, 8)
        self.assertEqual(title, 'Title')
        motd, off = protocol.parse_dstr(res, off)
        self.assertEqual(motd, 'MOTD')
        # 49 zero bytes, 64-byte serial response, 6 uint32 tail
        self.assertEqual(res[off:off + 49], bytes(49))
        self.assertEqual(res[off + 49:off + 49 + 64],
                         protocol.gen64(serial))
        self.assertEqual(len(res), off + 49 + 64 + 24)

    def test_gen64_deterministic(self):
        a = protocol.gen64(bytes(range(8)))
        b = protocol.gen64(bytes(range(8)))
        c = protocol.gen64(bytes([7] * 8))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, c)

    def test_pretty_guid(self):
        guid = struct.pack('<IHH8s', 0x12345678, 0xABCD, 0xEF01,
                           bytes(range(8)))
        self.assertEqual(protocol.pretty_guid(guid),
                         '12345678-abcd-ef01-0001-020304050607')


if __name__ == '__main__':
    unittest.main()
