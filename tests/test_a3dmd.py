"""Regression tests for :mod:`a3driverdoctor.a3dmd`."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
# Support both flat Files-tab downloads and tests/ in the repository.
for candidate in (ROOT.parent / 'src' / 'a3driverdoctor', ROOT.parent, ROOT):
    sys.path.insert(0, str(candidate))

from a3dmd import *  # Test the module's format helpers.

"""No image library, assembler or network required for these tests."""
def fixture(name='.TEST', secondary=False):
    code = bytearray(b'\xEA' * (192 if secondary else 96))
    def dib(offset, name, link=0):
        code[offset:offset + 34] = bytes(34)
        code[offset:offset + 2] = le16(link)
        code[offset + 2:offset + 4] = le16(48)
        encoded = name.encode('ascii')
        code[offset + 4] = len(encoded)
        code[offset + 5:offset + 20] = encoded.ljust(15, b' ')
        code[offset + 20] = 0x80
    dib(0, name, 128 if secondary else 0)
    if secondary:
        dib(128, '.SECOND')
    return Driver(b'test comment', bytes(code), le16(2))

def o65(driver, relocation=b'\x03\x83\x00', text_marker=True, text=None):
    # Explicit text permits malformed-input tests without changing the header.
    if text is None:
        if text_marker:
            text = b'\xff\xff' + bytes([len(driver.comment)]) + driver.comment
        else:
            if driver.comment:
                raise ValueError('A comment requires the $FFFF TEXT marker')
            text = b''
    header = b'\x01\x00o65\x00' + le16(0)
    header += struct.pack('<9H', 0, len(text), 0, len(driver.code), 0, 0, 0, 0, 0)
    # One binary option including a non-ASCII byte, then option terminator.
    return (header + b'\x03\x02\xff\x00' + text + driver.code
            + le16(0) + b'\x00' + relocation + le16(0))

class Tests(unittest.TestCase):
    def test_cli_double_dash_options(self):
        parser = argument_parser()
        cases = [
            ['add', '--inputimage', 'source.dsk', '--inputfile', 'SOS.DRIVER',
             '--driver', '.CONSOLE', '--outputimage', 'test.dsk',
             '--outputfile', 'SOS.DRIVER', '--hash-mode', 'immutable',
             '--dry-run', '--no-backup'],
            ['add', '--inputimage', 'source.dsk', '--inputfile', 'CONS.DRVR',
             '--outputimage', 'test.dsk'],
            ['add', '--o65file', 'mydriver.o65', '--outputimage', 'test.dsk'],
            ['delete', '--inputimage', 'test.dsk', '--inputfile', 'SOS.DRIVER',
             '--driver', '.CONSOLE'],
            ['extract', '--bundlefile', 'SOS.DRIVER', '--driver', '.CONSOLE',
             '--outputfile', 'console.driver', '--force', '--relocate'],
            ['convert', '--o65file', 'mydriver.o65',
             '--outputfile', 'mydriver.driver', '--force'],
            ['hash', '--relocfile', 'mydriver.driver'],
            ['list', '--inputimage', 'test.dsk'],
        ]
        from contextlib import redirect_stderr
        from io import StringIO
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])
            for index, token in enumerate(argv):
                if not token.startswith('--'):
                    continue
                bad = argv[:]
                bad[index] = token[1:]
                with self.subTest(rejected=bad), redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(bad)
                    self.assertEqual(error.exception.code, 2)
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                parser.parse_args(['hash', '--reloc', 'mydriver.driver'])
            self.assertEqual(error.exception.code, 2)

    def test_cli_help_and_registered_options(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        parser = argument_parser()
        def check_options(p):
            for action in p._actions:
                for name in action.option_strings:
                    self.assertTrue(name.startswith('--'), name)
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values():
                        check_options(child)
        check_options(parser)
        for command in (None, 'add', 'delete', 'extract', 'convert',
                        'hash', 'list'):
            prefix = [command] if command else []
            with self.subTest(command=command):
                with redirect_stdout(StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(prefix + ['--help'])
                    self.assertEqual(error.exception.code, 0)
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(prefix + ['-h'])
                    self.assertEqual(error.exception.code, 2)

    def test_primary_driver_version(self):
        d = fixture(secondary=True)
        for raw, expected in ((0x0000, '0.00'), (0x1000, '1.00'),
                              (0x1230, '1.23'), (0x116C, '1.16C'),
                              (0x799E, '7.99E'), (0x1004, '$1004'),
                              (0x1A00, '$1A00'), (0x10A0, '$10A0'),
                              (0x8000, '$8000'), (0xFFFF, '$FFFF')):
            with self.subTest(raw=raw):
                code = bytearray(d.code)
                code[0x1E:0x20] = le16(raw)
                code[128 + 0x1E:128 + 0x20] = le16(0x2340)
                changed = Driver(d.comment, bytes(code), d.relocation)
                self.assertEqual(changed.version, expected)
                self.assertEqual(Driver.parse(changed.pack()).version, expected)

    def test_list_version_column(self):
        from contextlib import redirect_stdout
        from io import StringIO
        d = fixture()
        code = bytearray(d.code)
        code[0x1E:0x20] = le16(0x116C)
        d = Driver(d.comment, bytes(code), d.relocation)
        bundle = Bundle(SIGNATURE + le16(0), (d,), b'\xff\xff')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'SOS.DRIVER'
            path.write_bytes(bundle.pack())
            for mode in ('immutable', 'driv3rs'):
                output = StringIO()
                args = argument_parser().parse_args(
                    ['list', '--bundlefile', str(path), '--hash-mode', mode])
                with redirect_stdout(output):
                    self.assertEqual(run(args), 0)
                lines = output.getvalue().splitlines()
                self.assertEqual(lines[0].split()[:5],
                                 ['PRIMARY', 'VERSION', 'COMMENT', 'CODE', 'RELOC'])
                self.assertEqual(lines[1].split()[:6],
                                 ['.TEST', '1.16C', 'test', 'comment', '96', '2'])
                self.assertIn(d.digest(mode), lines[1])
            self.assertEqual(path.read_bytes(), bundle.pack())

    def test_comment_display(self):
        d = fixture()
        for comment, expected in (
                (b'', ''), (b'Plain ASCII comment', 'Plain ASCII comment'),
                (b'line\r\n\t\x00\x1b\x7f\x80\xff',
                 r'line\r\n\t\x00\x1B\x7F\x80\xFF'),
                (b'path\\name', r'path\\name')):
            with self.subTest(comment=comment):
                changed = Driver(comment, d.code, d.relocation)
                self.assertEqual(changed.comment_text, expected)
                self.assertEqual(Driver.parse(changed.pack()).comment, comment)

    def test_list_comment_alignment_and_empty_bundle(self):
        from contextlib import redirect_stdout
        from io import StringIO
        d = fixture()
        drivers = tuple(Driver(comment, d.code, d.relocation) for comment in
                        (b'', b'long comment with spaces', b'line\r\nnext'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'SOS.DRIVER'
            for records in (drivers, ()):
                bundle = Bundle(SIGNATURE + le16(0), records, b'\xff\xff')
                path.write_bytes(bundle.pack())
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run(argument_parser().parse_args(
                        ['list', '--bundlefile', str(path)])), 0)
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), len(records) + 2)
                start = lines[0].index('COMMENT')
                width = max([len('COMMENT')] + [len(x.comment_text) for x in records])
                for line, driver in zip(lines[1:], records):
                    self.assertEqual(line[start:start + width].rstrip(), driver.comment_text)
                    self.assertEqual(line[start + width:].split()[:2],
                                     [str(len(driver.code)), str(len(driver.relocation))])

    def test_relocated_record_preamble_and_code_base(self):
        original = fixture()
        for length in (0, 1, 48, 49, 50, 51, 255):
            with self.subTest(comment_length=length):
                d = Driver(b'x' * length, original.code, original.relocation)
                packed = d.pack()
                result = relocated_record(d)
                preamble = (b'' if length == 0 else
                            b'\xff\xff' + bytes([length]) + d.comment)
                start = len(preamble)
                expected_code = bytearray(d.code)
                expected_code[2:4] = le16(0x2030)
                self.assertEqual(result, preamble + bytes(expected_code))
                self.assertEqual(len(result), start + len(d.code))
                self.assertEqual(word(result, start + 2), 0x2030)
                self.assertEqual(d.pack(), packed)
                self.assertEqual(RELOCATED_CODE_BASE - start + len(result),
                                 0x2000 + len(d.code))
        for length in (256, 300):
            d = Driver(b'x' * length, original.code, original.relocation)
            self.assertEqual(Driver.parse(d.pack()), d)
            with self.assertRaisesRegex(DoctorError, '255'):
                relocated_record(d)

    def test_relocated_text_marker_layout(self):
        original = fixture()
        # An explicit empty o65 TEXT preamble remains $FFFF $00.
        d = Driver(b'', original.code, original.relocation)
        result = relocated_record(convert_o65(o65(d)))
        self.assertEqual(result[:3], b'\xff\xff\x00')
        self.assertEqual(RELOCATED_CODE_BASE - 3, 0x1FFD)
        self.assertEqual(word(result, 5), 0x2030)
        # Without the optional marker, CODE begins immediately at $2000.
        result = relocated_record(convert_o65(o65(d, text_marker=False)))
        self.assertFalse(result.startswith(b'\xff\xff'))
        self.assertEqual(word(result, 2), 0x2030)
        for length, load in ((48, 0x1FCD), (50, 0x1FCB),
                             (51, 0x1FCA), (255, 0x1EFE)):
            d = Driver(b'x' * length, original.code, original.relocation)
            result = relocated_record(convert_o65(o65(d)))
            self.assertEqual(result[:3], b'\xff\xff' + bytes([length]))
            self.assertEqual(result[3:3 + length], d.comment)
            self.assertEqual(RELOCATED_CODE_BASE - (3 + length), load)
            self.assertEqual(word(result, 3 + length + 2), 0x2030)
        literal = Driver(bytes.fromhex('FF FF'), original.code, b'')
        self.assertEqual(relocated_record(literal)[:5],
                         bytes.fromhex('FF FF 02 FF FF'))

    def test_relocated_record_secondary_links_and_word_carry(self):
        d = fixture(secondary=True)
        code = bytearray(d.code)
        code[60:62] = le16(0x01F0)
        d = Driver(b'x' * 51, bytes(code),
                   b''.join(le16(x) for x in (60, 130, 0, 2)))
        result = relocated_record(d)
        start = 54
        expected = bytearray(d.code)
        for offset in (0, 2, 60, 130):
            expected[offset:offset + 2] = le16(word(d.code, offset) + 0x2000)
        self.assertEqual(result, b'\xff\xff\x33' + d.comment + bytes(expected))
        self.assertEqual(word(result, start + 128), 0)
        self.assertEqual(word(result, start + 60), 0x21F0)

    def test_relocated_record_empty_and_invalid_relocations(self):
        d = fixture()
        literal = Driver(d.comment, d.code, b'')
        self.assertEqual(relocated_record(literal),
                         b'\xff\xff' + bytes([len(d.comment)]) + d.comment + d.code)
        for reloc in (b'\x01', le16(95), le16(2) + le16(2),
                      le16(2) + le16(3)):
            with self.subTest(reloc=reloc), self.assertRaises(DoctorError):
                relocated_record(Driver(d.comment, d.code, reloc))

    def test_relocated_record_address_limits(self):
        d = fixture()
        base = 0x2000
        code = bytearray(d.code)
        code[60:62] = le16(0xFFFF - base)
        edge = Driver(d.comment, bytes(code), le16(60))
        self.assertEqual(word(relocated_record(edge), 3 + len(d.comment) + 60),
                         0xFFFF)
        code[60:62] = le16(0x10000 - base)
        with self.assertRaisesRegex(DoctorError, 'overflows'):
            relocated_record(Driver(d.comment, bytes(code), le16(60)))
        code_length = 0x10000 - base
        code = d.code + bytes(code_length - len(d.code))
        for comment in (b'', b'x' * 255):
            edge = Driver(comment, code, d.relocation)
            load = base - len(relocated_preamble(edge))
            self.assertEqual(len(relocated_record(edge)) + load, 0x10000)
            with self.assertRaisesRegex(DoctorError, 'extend beyond'):
                relocated_record(Driver(comment, code + b'\0', b''))

    def test_relocated_extract_sources_and_plain_output(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        d = fixture()
        bundle = Bundle(SIGNATURE + le16(0), (d,), b'\xff\xff')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cases = [('--relocfile', 'test.driver', d.pack(), []),
                     ('--bundlefile', 'SOS.DRIVER', bundle.pack(),
                      ['--driver', '.TEST']),
                     ('--o65file', 'test.o65', o65(d), [])]
            for option, name, data, selector in cases:
                src, dst = directory / name, directory / 'output.bin'
                src.write_bytes(data)
                for relocate in (False, True):
                    output = StringIO()
                    argv = ['extract', option, str(src), *selector,
                            '--outputfile', str(dst), '--force']
                    if relocate:
                        argv.append('--relocate')
                    with redirect_stdout(output):
                        self.assertEqual(run(argument_parser().parse_args(argv)), 0)
                    self.assertEqual(dst.read_bytes(),
                                     relocated_record(d) if relocate else d.pack())
                    if relocate:
                        exported = dst.read_bytes()
                        self.assertEqual(exported[:2], bytes.fromhex('FF FF'))
                        self.assertEqual(exported[2], len(d.comment))
                        self.assertEqual(len(exported), 3 + len(d.comment) + len(d.code))
                        base = 0x2000
                        self.assertEqual(word(exported, 3 + len(d.comment) + 2), base + 48)
                        self.assertIn(f'code/DIB base: ${base:04X}', output.getvalue())
                    self.assertEqual(src.read_bytes(), data)
                    self.assertEqual(f'Disassembly load address: ${0x2000 - 3 - len(d.comment):04X}' in
                                     output.getvalue(), relocate)
                    self.assertIn(d.digest(), output.getvalue())
            # Exercise image source dispatch without writing an image or using diskii.
            argv = ['extract', '--inputimage', 'source.dsk', '--driver', '.TEST',
                    '--outputfile', str(dst), '--force', '--relocate']
            with patch.dict(run.__globals__,
                            image_read=lambda *args: bundle.pack()):
                with redirect_stdout(StringIO()):
                    self.assertEqual(run(argument_parser().parse_args(argv)), 0)
            self.assertEqual(dst.read_bytes(), relocated_record(d))

    def test_relocated_extract_safety(self):
        from contextlib import redirect_stdout
        from io import StringIO
        d = fixture()
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'source.driver'
            dst = Path(directory) / 'output.bin'
            src.write_bytes(d.pack())
            dst.write_bytes(b'existing')
            argv = ['extract', '--relocfile', str(src), '--relocate',
                    '--outputfile', str(dst)]
            with redirect_stdout(StringIO()):
                with self.assertRaises(FileExistsError):
                    run(argument_parser().parse_args(argv))
                self.assertEqual(dst.read_bytes(), b'existing')
                same = argv[:-1] + [str(src), '--force']
                with self.assertRaises(DoctorError):
                    run(argument_parser().parse_args(same))
                self.assertEqual(src.read_bytes(), d.pack())
                code = bytearray(d.code)
                code[60:62] = le16(0xFFFF)
                bad = Driver(d.comment, bytes(code), le16(60))
                src.write_bytes(bad.pack())
                with self.assertRaises(DoctorError):
                    run(argument_parser().parse_args(argv + ['--force']))
                self.assertEqual(dst.read_bytes(), b'existing')
                dst.unlink()
                with self.assertRaises(DoctorError):
                    run(argument_parser().parse_args(argv))
                self.assertFalse(dst.exists())

    def test_relocate_option_scope(self):
        from contextlib import redirect_stderr
        from io import StringIO
        parser = argument_parser()
        for argv in (
                ['convert', '--o65file', 'in.o65', '--outputfile', 'out', '--relocate'],
                ['extract', '--relocfile', 'in', '--outputfile', 'out',
                 '--relocate', '--base-address', '0x2000'],
                ['extract', '--relocfile', 'in', '--outputfile', 'out', '--reloc']):
            with self.subTest(argv=argv), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser.parse_args(argv)
                self.assertEqual(error.exception.code, 2)


    def test_local_bundle_and_relocfile_type_checks_use_duofile(self):
        """DUOFILE is a real one-record bundle, not a standalone driver."""
        candidates = (ROOT / 'data' / 'DUOFILE', ROOT / 'tests' / 'data' / 'DUOFILE')
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            self.skipTest('Optional real-driver fixture tests/data/DUOFILE is not available')
        data = path.read_bytes()
        self.assertTrue(data.startswith(SIGNATURE))
        bundle = Bundle.parse(data)
        self.assertEqual(len(bundle.drivers), 1)
        driver = bundle.drivers[0]

        parser = argument_parser()
        mistaken = parser.parse_args(['hash', '--relocfile', str(path)])
        with self.assertRaisesRegex(DoctorError,
                                    r'not a SOS\.DRIVER bundle.*--bundlefile'):
            source_driver(mistaken)

        selected = parser.parse_args(
            ['hash', '--bundlefile', str(path), '--driver', driver.name])
        self.assertEqual(source_driver(selected), driver)

    def test_bundlefile_rejects_a_standalone_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'standalone.driver'
            path.write_bytes(fixture().pack())
            args = argument_parser().parse_args(
                ['hash', '--bundlefile', str(path), '--driver', '.TEST'])
            with self.assertRaisesRegex(DoctorError,
                                        r'beginning with SOS DRVR'):
                source_driver(args)

    def test_record_roundtrip(self):
        d = fixture(secondary=True)
        self.assertEqual(Driver.parse(d.pack()), d)
        self.assertEqual(d.names, ('.TEST', '.SECOND'))

    def test_bundle_roundtrip_and_secondary_selection(self):
        d = fixture(secondary=True)
        b = Bundle(SIGNATURE + le16(4) + b'HEAD', (d,), b'\xff\xffPAD')
        self.assertEqual(Bundle.parse(b.pack()), b)
        self.assertEqual(b.locate('.second'), 0)

    def test_upsert_order_and_preservation(self):
        first, last = fixture('.FIRST'), fixture('.LAST')
        b = Bundle(SIGNATURE + le16(0), (first, last), b'\xff\xff\0')
        replacement = Driver(b'new', first.code, first.relocation)
        result, action = b.upsert(replacement)
        self.assertEqual(action, 'replaced')
        self.assertEqual(result.drivers, (replacement, last))
        result, action = result.upsert(fixture('.NEW'))
        self.assertEqual(action, 'added')
        self.assertEqual(result.header, b.header)
        self.assertEqual(result.trailer, b.trailer)
        result, removed = result.delete('.first')
        self.assertEqual(removed, replacement)
        self.assertEqual(result.drivers[0], last)

    def test_ambiguous_names_and_collisions(self):
        d = fixture(secondary=True)
        b = Bundle(SIGNATURE + le16(0), (d, d), b'\xff\xff')
        with self.assertRaises(DoctorError):
            b.locate('.TEST')
        with self.assertRaises(DoctorError):
            b.upsert(d)
        b = Bundle(b.header, (d,), b.trailer)
        with self.assertRaises(DoctorError):
            b.upsert(fixture('.SECOND'))

    def test_hash_mutability(self):
        d = fixture(secondary=True)
        code = bytearray(d.code)
        code[20:23] = b'\0\x04\x03'
        code[128 + 20:128 + 23] = b'\0\x02\x07'
        code[5:20] = b'.RENAMED       '
        code[4] = 8
        changed = Driver(b'different comment', bytes(code), b'')
        self.assertEqual(d.digest(), changed.digest())
        self.assertNotEqual(d.digest('driv3rs'), changed.digest('driv3rs'))
        self.assertEqual(d.digest('driv3rs'),
                         hashlib.md5(d.code[48:], usedforsecurity=False).hexdigest())
        code[60] ^= 1
        self.assertNotEqual(d.digest(), Driver(d.comment, bytes(code), b'').digest())

    def test_o65_conversion(self):
        d = fixture()
        self.assertEqual(convert_o65(o65(d)), d)

    def test_o65_long_relocation_delta(self):
        d = fixture()
        code = d.code + bytes(300)
        large = Driver(d.comment, code, le16(300))
        self.assertEqual(convert_o65(o65(large, b'\xff\x2f\x83\x00')), large)

    def test_reject_o65_unsupported_fixup_and_mode(self):
        d = fixture()
        with self.assertRaises(DoctorError):
            convert_o65(o65(d, b'\x03\x43\x00'))
        data = bytearray(o65(d))
        data[7] = 0x20
        with self.assertRaises(DoctorError):
            convert_o65(bytes(data))

    def test_reject_bad_lengths_links_and_relocations(self):
        d = fixture(secondary=True)
        for end in (0, 1, 3, len(d.pack()) - 1):
            with self.assertRaises(DoctorError):
                Driver.parse(d.pack()[:end])
        with self.assertRaises(DoctorError):
            Driver.parse(d.pack() + b'\0')
        code = bytearray(d.code)
        code[128:130] = le16(128)
        with self.assertRaises(DoctorError):
            Driver(d.comment, bytes(code), d.relocation).validate()
        for reloc in (b'\x01', le16(191), le16(2) + le16(3)):
            with self.assertRaises(DoctorError):
                Driver(d.comment, d.code, reloc).validate()
        with self.assertRaises(DoctorError):
            Bundle.parse(SIGNATURE + le16(0) + d.pack())


class TextCommentTests(unittest.TestCase):
    """Standard uint8 TEXT lengths and historical zero-high-byte uint16 input."""

    def test_all_one_byte_comment_lengths(self):
        original = fixture()
        for length in range(256):
            with self.subTest(length=length):
                # Includes NUL, high-bit bytes and literal FF FF as payload.
                comment = (b'\xff\xff' + bytes(range(256)))[:length]
                text = b'\xff\xff' + bytes([length]) + comment
                d = Driver(comment, original.code, original.relocation)
                converted = convert_o65(o65(d, text=text))
                self.assertEqual(converted.comment, comment)
                self.assertIs(converted.text_preamble, True)
                self.assertEqual(converted.code, original.code)
                self.assertEqual(converted.relocation, original.relocation)
                self.assertEqual(relocated_preamble(converted), text)
                expected_code = bytearray(original.code)
                expected_code[2:4] = le16(0x2030)
                self.assertEqual(relocated_record(converted), text + bytes(expected_code))
                self.assertEqual(Driver.parse(converted.pack()).comment, comment)

    def test_all_legacy_comment_lengths(self):
        original = fixture()
        for length in range(256):
            with self.subTest(length=length):
                comment = bytes(range(256))[:length]
                text = b'\xff\xff' + le16(length) + comment
                d = convert_o65(o65(original, text=text))
                self.assertEqual(d.comment, comment)
                self.assertIs(d.text_preamble, True)
                self.assertEqual(d.code, original.code)
                self.assertEqual(d.relocation, original.relocation)
                canonical = b'\xff\xff' + bytes([length]) + comment
                self.assertEqual(relocated_preamble(d), canonical)
                expected = bytearray(original.code)
                expected[2:4] = le16(0x2030)
                self.assertEqual(relocated_record(d), canonical + bytes(expected))
                self.assertEqual(Driver.parse(d.pack()).comment, comment)

    def test_uploaded_word_sized_comment_driver(self):
        # Exact bytes of uploaded test2.o65.hex.txt, compressed for a portable
        # regression fixture (no assembler or external binary file required).
        import base64
        import zlib
        data = zlib.decompress(base64.b64decode(
            'eJxlUk1oG1cQ/nZX/3Zs2ZaMFFP6qsS2XNfGWkfSJlB6SalqVAI1NNDT0thFaZsYGp16SBSWhVJYR87VBNzDQrf0FaW33NTGcbIUwlpSEpUecmlMD0Xspe2pVWdXRqV0Hm9n3sx83/ywArYKeXgyTfe2hKGMorp5rSove/EZ8ZONQp69Jy/nzrIl9tblKvugmP9QKcqYCbyzdZVduFRlOZnJ8rm8fE4mY0UuICCIQL8/iYtbn24sXbv82eYGu7R15crm1SpwFtHlixfePb/+9vtvMl9qfdQEILJ+HnHCDbpYxH+ly6qS+b0bsiaU8jrMph11JSMDdh3splQfK5En4gYqLJuuKKqFlPqGsCNcF7m4jW3cpvMlKgIlxRojsa/ufYzSvY9QUi1GyXN044rKZ+H+mfKVmCIKYx48CbvvjtlCfc4ONjJGFuUfYQkUIZNNCPUpPg831suCZ2EH67/5PFlE48YirOC3C/jmVbCpdCNJTiMJY85Dz4LIT1OL1qyfzTJpcrQTHrCpt62I3tqT7rZ+2pac8OfxP1JlRVAilpeWVX2kUYBRBHsksl00kuwXoRE3O8YpsF2hEaICHjfPwBbcjA3nJD8NN+mlTvFT0DvWMb68K3ijewHJWlRUs+PQ3Ox3oS6o5g96y7yvt/n9BDVGCAXGYYK9LpkHxgrMh0YObFrsFdHz++itwJF6ORx0nF9TqrnvRM0Ha42A3qEZX1NUhZ/EiwP9CZ/Bi4f60/qo+dQJmU8edOreopdoCXOIkXzNc76OGTL4CtbW1k7IIPvOzSY9B5FVWKJxBlZTP9zz9pgHX8WdWvOLaE137jo7Lfay1MvjyDGdhXGn11sFz8P5+fEZOIcpb1h/Ojdi/+2OW6/Q+N+JlriHnVbKN/ueSRRW3/N4CbSGlrlPq9gfrqJMe2jacImpVTLbJd5OlLiCkvlIf2baepfbCaObMJ8RsEvA7hBIC9wDLwxK8CLpCnkrFK7o7YreUo9aTvqoTVzUnAewBPofahRXqaZKdeI5+5a7ZLbdUfuv+uzCDb3doyAF7JobeUxlanqb/mpC3fKKCsShRkr8eT9aM573K9nBY+HG4MFBYwCSNq9NajEtqkl0pjXxf2dEC2gTWtjPGKcb9DODvh451pGhL0q+cfqO+Z6BN0zfMOlBdtj3hilrWgsd4//NnaFImvg8xpB/Iz5yUHvkmG+SrKj2Er0SWmbYTYheYe2EVtRosH8AUw6w5A=='))
        d = convert_o65(data)
        self.assertEqual(d.name, '.WORDSIZE')
        self.assertEqual(d.comment, b'Word-sized comment')
        self.assertEqual(len(d.code), 914)
        self.assertEqual(len(d.relocation) // 2, 107)
        self.assertEqual(Driver.parse(d.pack()), d)
        preamble = b'\xff\xff\x12Word-sized comment'
        self.assertEqual(relocated_preamble(d), preamble)
        relocated = relocated_record(d)
        expected = bytearray(d.code)
        for (offset,) in struct.iter_unpack('<H', d.relocation):
            expected[offset:offset + 2] = le16(word(d.code, offset) + 0x2000)
        self.assertEqual(relocated, preamble + bytes(expected))

    def test_standard_leading_nul_is_payload(self):
        for length in range(1, 256):
            with self.subTest(length=length):
                comment = b'\0' + b'A' * (length - 1)
                text = b'\xff\xff' + bytes([length]) + comment
                d = convert_o65(o65(fixture(), text=text))
                self.assertEqual(d.comment, comment)
                self.assertEqual(relocated_preamble(d), text)

    def test_reject_malformed_legacy_comments(self):
        for text in (b'\xff\xff\x01\x01A',  # Nonzero high byte.
                     b'\xff\xff\x00\x01',
                     b'\xff\xff\x01\x00AB',  # Extra payload.
                     b'\xff\xff\x03\x00A',   # Short payload.
                     b'\xff\xff\x00\x01' + bytes(256)):
            with self.subTest(text=text), self.assertRaisesRegex(DoctorError, 'TEXT'):
                convert_o65(o65(fixture(), text=text))

    def test_cli_legacy_convert_and_relocate(self):
        from contextlib import redirect_stdout
        from io import StringIO
        original = fixture()
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'legacy.o65'
            dst = Path(directory) / 'out.bin'
            for comment in (b'', b'A', b'\0A', b'Word-sized comment'):
                src.write_bytes(o65(original, text=b'\xff\xff' +
                                    le16(len(comment)) + comment))
                expected = Driver(comment, original.code, original.relocation,
                                  text_preamble=True)
                for command in ('convert', 'extract'):
                    with self.subTest(comment=comment, command=command):
                        args = [command, '--o65file', str(src),
                                '--outputfile', str(dst), '--force']
                        if command == 'extract':
                            args.append('--relocate')
                        output = StringIO()
                        with redirect_stdout(output):
                            self.assertEqual(main(args), 0)
                        self.assertEqual(dst.read_bytes(), expected.pack()
                                         if command == 'convert' else
                                         relocated_record(expected))
                        if command == 'extract':
                            load = 0x2000 - 3 - len(comment)
                            self.assertIn(f'Disassembly load address: ${load:04X}',
                                          output.getvalue())

    def test_empty_text_is_no_preamble(self):
        original = fixture()
        d = convert_o65(o65(original, text=b''))
        self.assertEqual(d.comment, b'')
        self.assertIs(d.text_preamble, False)
        self.assertEqual(relocated_preamble(d), b'')
        expected = bytearray(original.code)
        expected[2:4] = le16(0x2030)
        self.assertEqual(relocated_record(d), bytes(expected))

    def test_reject_malformed_text(self):
        cases = (
            b'\xff',                   # Partial marker.
            b'\xff\xff',             # Missing length.
            b'\xff\xff\x01',        # Missing comment byte.
            b'\xff\xff\x02A',       # Short comment.
            b'\xff\xff\x00A',       # Extra byte after empty comment.
            b'\xff\xff\x01AB',      # Extra byte after nonempty comment.
            b'\xff\xff\xff' + bytes(254),
            b'\x00', b'\x00\x00',   # Unmarked length is not a preamble.
            b'\x01\x00A', b'ABC', b'\x00\xff\xff\x00',
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(DoctorError, 'TEXT'):
                    convert_o65(o65(fixture(), text=text))

    def test_container_lengths_and_terminator_are_unchanged(self):
        original = fixture()
        for comment in (b'', b'\xff\xff', bytes(range(256)), b'x' * 300):
            with self.subTest(length=len(comment)):
                d = Driver(comment, original.code, original.relocation)
                packed = d.pack()
                self.assertEqual(packed[:2], le16(len(comment)))
                self.assertEqual(Driver.parse(packed), d)
                bundle = Bundle(SIGNATURE + le16(0), (d,), b'\xff\xffPAD')
                self.assertEqual(Bundle.parse(bundle.pack()).pack(), bundle.pack())

    def test_empty_preamble_container_roundtrip_limitation(self):
        # Container records have no bit recording presence of an empty TEXT
        # preamble. Do not invent an incompatible extension to retain it.
        original = fixture()
        marked = convert_o65(o65(original, text=b'\xff\xff\x00'))
        absent = convert_o65(o65(original, text=b''))
        self.assertEqual(relocated_preamble(marked), b'\xff\xff\x00')
        self.assertEqual(relocated_preamble(absent), b'')
        self.assertEqual(marked.pack(), absent.pack())
        for d in (marked, absent):
            restored = Driver.parse(d.pack())
            self.assertIsNone(restored.text_preamble)
            self.assertEqual(relocated_preamble(restored), b'')

    def test_cli_o65_preamble_and_load_address(self):
        from contextlib import redirect_stdout
        from io import StringIO
        original = fixture()
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'driver.o65'
            dst = Path(directory) / 'driver.bin'
            for text, load in ((b'', 0x2000),
                               (b'\xff\xff\x00', 0x1FFD),
                               (b'\xff\xff\x01A', 0x1FFC)):
                with self.subTest(text=text):
                    src.write_bytes(o65(original, text=text))
                    output = StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main([
                            'extract', '--o65file', str(src), '--relocate',
                            '--outputfile', str(dst), '--force']), 0)
                    expected = bytearray(original.code)
                    expected[2:4] = le16(0x2030)
                    self.assertEqual(dst.read_bytes(), text + bytes(expected))
                    self.assertIn(f'Disassembly load address: ${load:04X}',
                                  output.getvalue())
                    self.assertIn('code/DIB base: $2000', output.getvalue())

    def test_cli_convert_uses_container_length(self):
        from contextlib import redirect_stdout
        from io import StringIO
        original = fixture()
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'driver.o65'
            dst = Path(directory) / 'driver.DRVR'
            for text, comment in ((b'', b''), (b'\xff\xff\x00', b''),
                                  (b'\xff\xff\x01A', b'A')):
                with self.subTest(text=text):
                    src.write_bytes(o65(original, text=text))
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main([
                            'convert', '--o65file', str(src),
                            '--outputfile', str(dst), '--force']), 0)
                    expected = Driver(comment, original.code, original.relocation)
                    self.assertEqual(dst.read_bytes(), expected.pack())

    def test_cli_malformed_text_leaves_output_untouched(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'driver.o65'
            dst = Path(directory) / 'driver.bin'
            src.write_bytes(o65(fixture(), text=b'\xff\xff\x01\x00AB'))
            for command in ('convert', 'extract'):
                for exists in (False, True):
                    with self.subTest(command=command, exists=exists):
                        dst.unlink(missing_ok=True)
                        if exists:
                            dst.write_bytes(b'existing')
                        args = [command, '--o65file', str(src),
                                '--outputfile', str(dst), '--force']
                        if command == 'extract':
                            args.append('--relocate')
                        errors = StringIO()
                        with redirect_stdout(StringIO()), redirect_stderr(errors):
                            self.assertEqual(main(args), 1)
                        self.assertIn('one-byte comment length', errors.getvalue())
                        if exists:
                            self.assertEqual(dst.read_bytes(), b'existing')
                        else:
                            self.assertFalse(dst.exists())

    def test_bundle_source_type_checks_without_external_fixture(self):
        d = fixture()
        bundle = Bundle(SIGNATURE + le16(0), (d,), b'\xff\xff')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bundle'
            path.write_bytes(bundle.pack())
            parser = argument_parser()
            args = parser.parse_args(['hash', '--relocfile', str(path)])
            with self.assertRaisesRegex(DoctorError, r'not a SOS\.DRIVER bundle.*--bundlefile'):
                source_driver(args)
            args = parser.parse_args(['hash', '--bundlefile', str(path),
                                      '--driver', d.name])
            self.assertEqual(source_driver(args), d)


class ImageCommitTests(unittest.TestCase):
    """Exercise real host-file commits with a fake disk-filesystem adapter.

    The fsync wrapper enforces macOS-style writable-descriptor requirements
    even when these tests run on a platform accepting read-only descriptors.
    These are transaction tests, not diskii integration tests.
    """

    def exercise_commit(self, command='add', dry_run=False, no_backup=False,
                        sync_error=False, legacy_comment=False):
        import errno
        from contextlib import contextmanager, redirect_stdout, redirect_stderr
        from io import StringIO
        from unittest.mock import patch

        original_driver = fixture('.EXISTING')
        incoming = Driver(b'Legacy comment' if legacy_comment else b'',
                          fixture('.NOCOMMNT').code, le16(2))
        original_bundle = Bundle(SIGNATURE + le16(0), (original_driver,), b'\xff\xff')
        original_payload = original_bundle.pack()
        if command == 'add':
            expected_bundle, _ = original_bundle.upsert(incoming)
        else:
            expected_bundle, _ = original_bundle.delete('.EXISTING')
        expected_payload = expected_bundle.pack()

        def disk_bytes(payload):
            return (le16(len(payload)) + payload).ljust(4096, b'\0')

        # Directory metadata is supplied directly: filesystem allocation is
        # outside the scope of these host-file transaction regressions.
        entry = bytearray(39)
        entry[0] = 0x1A
        entry[1:11] = b'SOS.DRIVER'
        entry[16] = 0x0C
        entry[30] = 0x01  # Locked; the temporary image must be unlocked.
        unlocks = []

        class FakeImage:
            def __init__(self, path, writable):
                self.path, self.writable = Path(path), writable

            def read_file(self, filename):
                data = self.path.read_bytes()
                return data[2:2 + word(data, 0)]

            def read_block(self, number):
                return bytes(512)

            def write_block(self, number, data):
                assert self.writable

            def modify_file_attributes(self, filename, **kwargs):
                assert self.writable
                unlocks.append(kwargs['access'])

            def delete_file(self, filename):
                assert self.writable

            def create_file(self, filename, file_type, data, **kwargs):
                assert self.writable
                self.path.write_bytes(disk_bytes(data))

        @contextmanager
        def fake_open_image(path, writable=False):
            yield FakeImage(path, writable)

        real_open, real_fsync, real_chmod = Path.open, os.fsync, os.chmod
        streams, sync_modes, events = {}, [], []

        def tracked_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            streams[stream.fileno()] = (Path(path), stream)
            return stream

        def strict_fsync(fd):
            path, stream = streams[fd]
            if not stream.writable():
                raise OSError(errno.EBADF, 'Bad file descriptor')
            sync_modes.append(stream.mode)
            if '.a3dmd-' in path.name:
                events.append('sync')
                if sync_error:
                    raise OSError(errno.EIO, 'Injected sync failure')
            return real_fsync(fd)

        def tracked_chmod(path, mode):
            if '.a3dmd-' in Path(path).name:
                events.append('chmod')
            return real_chmod(path, mode)

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            image = directory / 'drivers.dsk'
            src = directory / 'test3.o65'
            original = disk_bytes(original_payload)
            image.write_bytes(original)
            original_mode = stat.S_IMODE(image.stat().st_mode)
            source = o65(incoming, text=(b'\xff\xff' +
                         le16(len(incoming.comment)) + incoming.comment)
                         if legacy_comment else b'')
            src.write_bytes(source)
            if command == 'add':
                argv = ['add', '--o65file', str(src), '--outputimage', str(image)]
            else:
                argv = ['delete', '--inputimage', str(image), '--driver', '.EXISTING']
            if dry_run:
                argv.append('--dry-run')
            if no_backup:
                argv.append('--no-backup')
            output, errors = StringIO(), StringIO()
            with patch.dict(run.__globals__,
                            open_image=fake_open_image,
                            directory_record=lambda *args: (2, 43, bytes(entry)),
                            file_manifest=lambda *args: {}), \
                    patch.object(Path, 'open', tracked_open), \
                    patch.object(os, 'fsync', strict_fsync), \
                    patch.object(os, 'chmod', tracked_chmod), \
                    redirect_stdout(output), redirect_stderr(errors):
                status = main(argv)

            if sync_error:
                self.assertEqual(status, 1)
                self.assertIn('Injected sync failure', errors.getvalue())
                self.assertEqual(events, ['sync'])
            else:
                self.assertEqual(status, 0, errors.getvalue())
                self.assertEqual(errors.getvalue(), '')
                self.assertEqual(events, [] if dry_run else ['sync', 'chmod'])
            self.assertEqual(image.read_bytes(), original if dry_run or sync_error
                             else disk_bytes(expected_payload))
            self.assertEqual(stat.S_IMODE(image.stat().st_mode), original_mode)
            self.assertEqual(src.read_bytes(), source)
            backup = Path(str(image) + '.bak')
            self.assertEqual(backup.exists(), not dry_run and not no_backup)
            if backup.exists():
                self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(list(directory.glob('.drivers.a3dmd-*')), [])
            self.assertEqual(unlocks, [0xC3])
            self.assertEqual(len(sync_modes), 0 if dry_run else (1 if no_backup else 2))

    def test_add_o65_syncs_writable_descriptor(self):
        self.exercise_commit()

    def test_add_legacy_o65_comment(self):
        self.exercise_commit(legacy_comment=True)

    def test_delete_syncs_writable_descriptor(self):
        self.exercise_commit(command='delete')

    def test_add_without_backup(self):
        self.exercise_commit(no_backup=True)

    def test_dry_run_does_not_commit_or_sync(self):
        self.exercise_commit(dry_run=True)

    def test_sync_failure_preserves_original_and_cleans_temporary(self):
        self.exercise_commit(sync_error=True)


if __name__ == '__main__':
    unittest.main()
