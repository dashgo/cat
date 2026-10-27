import struct
import unittest
from server import Parser, packet, MIC, TALK

class ProtocolTests(unittest.TestCase):
    def test_fragmented_stream(self):
        raw = packet(MIC, bytes(range(256)) * 2 + bytes(128), 42)
        parser = Parser()
        result = []
        for offset in range(0, len(raw), 7):
            result.extend(parser.feed(raw[offset:offset+7]))
        self.assertEqual(result, [(MIC, 42, bytes(range(256))*2+bytes(128))])

    def test_corruption_and_noise_resynchronize(self):
        corrupt = bytearray(packet(MIC, bytes(640), 1))
        corrupt[30] ^= 0x80
        parser = Parser()
        result = list(parser.feed(b'noise' + corrupt + packet(TALK, b'\x01', 2)))
        self.assertEqual(result, [(TALK, 2, b'\x01')])
        self.assertEqual(parser.bad_crc, 1)

    def test_oversize_header_cannot_block_parser(self):
        bad = struct.pack('<4sBBHH', b'CAT1', MIC, 1, 2, 65535)
        self.assertEqual(list(Parser().feed(bad + packet(TALK, b'\x00', 3))), [(TALK,3,b'\x00')])

if __name__ == '__main__':
    unittest.main()
