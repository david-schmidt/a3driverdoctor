"""Setslot tests: place in tests/. Image I/O is mocked; no diskii needed."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / 'src/a3driverdoctor/a3dmd.py').is_file():
    sys.path.insert(0, str(ROOT / 'src'))
    from a3driverdoctor import a3dmd as m
else:
    sys.path.insert(0, str(ROOT))
    import a3dmd as m


def fixture(name='.TEST', secondary=True):
    code = bytearray(b'\xea' * 192)
    for offset, device, link, slot in ((0, name, 128 if secondary else 0, 1),
                                       (128, '.SECOND', 0, 4)):
        if offset and not secondary:
            continue
        code[offset:offset + 34] = bytes(34)
        code[offset:offset + 2] = m.le16(link)
        code[offset + 2:offset + 4] = m.le16(48)
        code[offset + 4] = len(device)
        code[offset + 5:offset + 20] = device.encode().ljust(15, b' ')
        code[offset + 20:offset + 25] = bytes((0x80, slot, 3, 7, 9))
    return m.Driver(b'comment\x00\xff', bytes(code), m.le16(2), text_preamble=True)


def bundle():
    return m.Bundle(m.SIGNATURE + m.le16(4) + b'HEAD',
                    (fixture('.FIRST', False), fixture(), fixture('.LAST', False)),
                    b'\xff\xffPAD\x00')


class SetslotTests(unittest.TestCase):
    def cli(self, *args):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return m.main(['setslot', *map(str, args)])

    def test_all_slots_change_only_slot_bytes(self):
        for secondary in (False, True):
            original = fixture(secondary=secondary)
            for slot in (0, 1, 2, 3, 4, 255):
                with self.subTest(secondary=secondary, slot=slot):
                    result = original.setslot(slot)
                    expected = bytearray(original.code)
                    for offset in ((21, 149) if secondary else (21,)):
                        expected[offset] = slot
                    self.assertEqual(result.code, bytes(expected))
                    self.assertEqual(result.comment, original.comment)
                    self.assertEqual(result.relocation, original.relocation)
                    self.assertIs(result.text_preamble, True)
                    self.assertEqual(result.digest(), original.digest())
                    self.assertEqual(m.Driver.parse(result.pack()), result)
                    self.assertEqual(original.code[21], 1)

    def test_invalid_slots_and_chain(self):
        for slot in (-1, 5, 254, 256, '2', 2.0, True, None):
            with self.subTest(slot=slot), self.assertRaises(m.DoctorError):
                fixture().setslot(slot)
        d = fixture()
        code = bytearray(d.code)
        code[128:130] = m.le16(128)
        with self.assertRaisesRegex(m.DoctorError, 'Cyclic'):
            m.Driver(d.comment, bytes(code), d.relocation).setslot(2)

    def test_local_update_backup_secondary_selection_and_noop(self):
        original = bundle()
        expected = m.Bundle(original.header,
                            (original.drivers[0], original.drivers[1].setslot(2),
                             original.drivers[2]), original.trailer).pack()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'SOS.DRIVER'
            path.write_bytes(original.pack())
            mode = path.stat().st_mode
            self.assertEqual(self.cli('--bundlefile', path, '--driver', '.second', '--slot', 2), 0)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(Path(str(path) + '.bak').read_bytes(), original.pack())
            self.assertEqual(path.stat().st_mode, mode)
            self.assertEqual(self.cli('--bundlefile', path, '--driver', '.TEST', '--slot', 2), 0)
            self.assertFalse(Path(str(path) + '.bak.1').exists())
            self.assertEqual(self.cli('--bundlefile', path, '--driver', '.TEST', '--slot', 3), 0)
            self.assertEqual(Path(str(path) + '.bak.1').read_bytes(), expected)
            self.assertEqual(Path(str(path) + '.bak').read_bytes(), original.pack())

    def test_dry_run_and_no_backup(self):
        for flag in ('--dry-run', '--no-backup'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'SOS.DRIVER'
                original = bundle().pack()
                path.write_bytes(original)
                self.assertEqual(self.cli('--bundlefile', path, '--driver', '.TEST', '--slot', 255, flag), 0)
                self.assertEqual(path.read_bytes() == original, flag == '--dry-run')
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_bundle_and_selection_do_not_write(self):
        b = bundle()
        ambiguous = m.Bundle(b.header, (fixture(), fixture()), b.trailer).pack()
        for data, name in ((b'not a bundle', '.TEST'), (fixture().pack(), '.TEST'),
                           (b.pack(), '.MISSING'), (ambiguous, '.TEST'),
                           (b.pack()[:-7], '.TEST')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'SOS.DRIVER'
                path.write_bytes(data)
                self.assertEqual(self.cli('--bundlefile', path, '--driver', name, '--slot', 2), 1)
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_parser_and_help(self):
        valid = ['--bundlefile', 'SOS.DRIVER', '--driver', '.TEST', '--slot', '2']
        cases = [[], valid + ['--inputimage', 'disk.dsk'], valid[:-2],
                 ['--bundlefile', 'SOS.DRIVER', '--slot', '2'],
                 valid + ['--relocfile', 'driver'], valid + ['--sl', '2'],
                 ['-bundlefile', 'SOS.DRIVER', '--driver', '.TEST', '--slot', '2']]
        cases += [valid[:-1] + [x] for x in ('-1', '5', '254', '256', '0xff', 'abc', '2.0')]
        for argv in cases:
            with self.subTest(argv=argv), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    m.argument_parser().parse_args(['setslot', *argv])
                self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.cli(*valid, '--inputfile', 'OTHER'), 1)
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as error:
            m.argument_parser().parse_args(['setslot', '--help'])
        self.assertEqual(error.exception.code, 0)
        for option in ('--bundlefile', '--inputimage', '--inputfile', '--driver',
                       '--slot', '--dry-run', '--no-backup'):
            self.assertIn(option, out.getvalue())

    def test_image_dispatch_and_flags(self):
        old = bundle().pack()
        for flags, filename, backup, dry_run in (
                ([], 'SOS.DRIVER', True, False),
                (['--inputfile', 'DRIVERS/OTHER', '--dry-run', '--no-backup'],
                 'DRIVERS/OTHER', False, True)):
            with self.subTest(flags=flags):
                with patch.object(m, 'image_read', return_value=old) as read, \
                     patch.object(m, 'replace_image_file', return_value=None) as write:
                    self.assertEqual(self.cli('--inputimage', 'disk.dsk', '--driver', '.TEST', '--slot', 4, *flags), 0)
                read.assert_called_once_with('disk.dsk', filename)
                b = bundle()
                drivers = list(b.drivers)
                drivers[1] = drivers[1].setslot(4)
                expected = m.Bundle(b.header, tuple(drivers), b.trailer).pack()
                write.assert_called_once_with('disk.dsk', filename, old, expected,
                                              backup=backup, dry_run=dry_run)

    def test_invalid_image_never_reaches_writer(self):
        with patch.object(m, 'image_read', return_value=fixture().pack()), \
             patch.object(m, 'replace_image_file') as write:
            self.assertEqual(self.cli('--inputimage', 'disk.dsk', '--driver', '.TEST', '--slot', 2), 1)
        write.assert_not_called()

    def test_local_transaction_checks_and_failed_commit(self):
        old = bundle().pack()
        b = bundle()
        new = m.Bundle(b.header, tuple(d.setslot(2) for d in b.drivers), b.trailer).pack()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'SOS.DRIVER'
            path.write_bytes(old)
            with self.assertRaisesRegex(m.DoctorError, 'changed'):
                m.replace_bundle_file(path, b'stale', new)
            with self.assertRaises(m.DoctorError):
                m.replace_bundle_file(path, old, b'invalid')
            self.assertEqual(list(Path(directory).iterdir()), [path])
            with patch.object(m.os, 'replace', side_effect=OSError('commit failed')):
                with self.assertRaisesRegex(OSError, 'commit failed'):
                    m.replace_bundle_file(path, old, new)
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(Path(str(path) + '.bak').read_bytes(), old)
            self.assertFalse(list(Path(directory).glob('.*.a3dmd-*')))
            if m.os.name != 'nt':
                path.chmod(0o444)
                try:
                    with self.assertRaisesRegex(m.DoctorError, 'read-only'):
                        m.replace_bundle_file(path, old, new)
                finally:
                    path.chmod(0o644)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'SOS.DRIVER'
            link = Path(directory) / 'alias'
            path.write_bytes(bundle().pack())
            try:
                link.symlink_to(path)
            except (OSError, NotImplementedError):
                self.skipTest('Symlinks unavailable')
            self.assertEqual(self.cli('--bundlefile', link, '--driver', '.TEST', '--slot', 2), 1)
            self.assertTrue(link.is_symlink())
            self.assertEqual(path.read_bytes(), bundle().pack())


if __name__ == '__main__':
    unittest.main()
