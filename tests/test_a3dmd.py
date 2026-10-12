"""Regression tests for :mod:`a3driverdoctor.a3dmd`."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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

def o65(driver, relocation=b'\x03\x83\x00', text_marker=True):
    text = ((b'\xff\xff' if text_marker else b'')
            + le16(len(driver.comment)) + driver.comment)
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
        path = Path(__file__).with_name('data') / 'DUOFILE'
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
