#!/usr/bin/env python3
"""a3dmd: manipulate Apple /// SOS relocatable device drivers.

Requires Python 3.11+ and, for disk images, diskii>=0.4.17:
    python -m pip install diskii

Examples:
    python a3dmd.py add --inputimage source.dsk --inputfile SOS.DRIVER \
        --driver .CONSOLE --outputimage test.dsk
    python a3dmd.py add --inputimage source.dsk --inputfile CONS.DRVR \
        --outputimage test.dsk
    python a3dmd.py add --o65file mydriver.o65 --outputimage test.dsk
    python a3dmd.py delete --inputimage test.dsk --driver .CONSOLE
    python a3dmd.py extract --inputimage source.dsk --driver .CONSOLE \
        --outputfile console.driver
    python a3dmd.py convert --o65file mydriver.o65 --outputfile mydriver.DRVR
    python a3dmd.py hash --relocfile mydriver.DRVR
    python a3dmd.py list --inputimage test.dsk
    python a3dmd.py setslot --bundlefile SOS.DRIVER --driver .MOUSE --slot 2
    python a3dmd.py setslot --inputimage test.dsk --driver .MOUSE --slot 2

Format/algorithm references:
  Robert Justice's a3driverutil: https://github.com/robjustice/a3driverutil
  Mike Whalen, Michael Sternberg, Paul Hagstrom:
    https://github.com/thecompu/Driv3rs (code_md5 algorithm)
  Andre Fachat: http://6502.org/users/andre/o65/
  https://pypi.org/project/diskii/0.4.17/
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("a3driverdoctor")
except PackageNotFoundError:
    __version__ = "unknown (not installed)"

VERSION = '0.2.1'
SIGNATURE = b'SOS DRVR'
RELOCATED_CODE_BASE = 0x2000
TEXT_COMMENT_MARKER = bytes.fromhex('FF FF')
VALID_SLOTS = (0, 1, 2, 3, 4, 255)
DIB_SLOT_OFFSET = 0x15  # Relative to the DIB, excluding the code-length word.
SUPPORTED_EXTENSIONS = {'.dsk', '.do', '.po', '.hdv'}


class DoctorError(Exception):
    """Invalid input or an operation that cannot safely be completed."""


def require(condition, message):
    if not condition:
        raise DoctorError(message)


def word(data, offset):
    require(0 <= offset and offset + 2 <= len(data),
            f'Truncated 16-bit field at offset ${offset:X}')
    return struct.unpack_from('<H', data, offset)[0]


def le16(value):
    require(0 <= value <= 65535, f'Value exceeds a 16-bit field: {value}')
    return struct.pack('<H', value)


class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def take(self, count):
        require(count >= 0 and self.pos + count <= len(self.data),
                f'Truncated input at ${self.pos:X}: need {count} bytes')
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def byte(self):
        return self.take(1)[0]

    def u16(self):
        return int.from_bytes(self.take(2), 'little')

    def cstring(self):
        end = self.data.find(b'\0', self.pos)
        require(end >= 0, 'Unterminated o65 symbol name')
        return self.take(end - self.pos + 1)[:-1]


@dataclass(frozen=True)
class DIB:
    offset: int
    name: str
    entry: int
    end: int


@dataclass(frozen=True)
class Driver:
    comment: bytes
    code: bytes
    relocation: bytes
    # o65 TEXT can explicitly include or omit the optional $FFFF comment
    # preamble.  SOS driver records do not retain that distinction, so None
    # denotes a record read from the SOS container format.
    text_preamble: bool | None = field(default=None, compare=False)

    @classmethod
    def read(cls, reader):
        count = reader.u16()
        require(count != 0xFFFF, 'Bundle terminator is not a driver record')
        comment = reader.take(count)
        code = reader.take(reader.u16())
        reloc = reader.take(reader.u16())
        result = cls(comment, code, reloc)
        result.validate()
        return result

    @classmethod
    def parse(cls, data):
        reader = Reader(data)
        driver = cls.read(reader)
        require(reader.pos == len(data),
                'Extra bytes after standalone driver; expected exactly one record')
        return driver

    def dibs(self):
        """Follow zero-based DIB links, checking loops, bounds and overlap."""
        result, seen = [], set()
        offset = 0
        while True:
            require(offset not in seen, 'Cyclic DIB chain')
            seen.add(offset)
            require(offset + 34 <= len(self.code),
                    f'DIB at ${offset:X} is outside the code segment')
            length = self.code[offset + 4]
            require(1 <= length <= 15, f'Invalid DIB name length at ${offset:X}')
            raw_name = self.code[offset + 5:offset + 5 + length]
            require(all(33 <= b <= 126 for b in raw_name),
                    f'Non-printable DIB name at ${offset:X}')
            name = raw_name.decode('ascii')
            entry = word(self.code, offset + 2)
            require(entry < len(self.code), f'{name}: entry point is outside code')
            end = offset + 34 + word(self.code, offset + 32)
            require(end <= len(self.code), f'{name}: DCB runs past code segment')
            require(all(end <= d.offset or offset >= d.end for d in result),
                    f'{name}: overlapping DIB/DCB regions')
            result.append(DIB(offset, name, entry, end))
            offset = word(self.code, offset)
            if offset == 0:
                break
        for d in result:
            require(all(not (x.offset <= d.entry < x.end) for x in result),
                    f'{d.name}: entry point lies in a DIB/DCB')
        return tuple(result)

    @property
    def name(self):
        return self.dibs()[0].name

    @property
    def version(self):
        """Format the primary DIB's SOS version; retain nonstandard values as hex.

        The LE16 field at $1E is $VvvQ: major 0..7, two BCD minor
        digits, and qualifier 0 (none) or A..E. See the SOS driver guide.
        """
        raw = word(self.code, self.dibs()[0].offset + 0x1E)
        major = raw >> 12
        tens, units, qualifier = (raw >> 8) & 15, (raw >> 4) & 15, raw & 15
        if major > 7 or tens > 9 or units > 9 or qualifier not in (0, 10, 11, 12, 13, 14):
            return f'${raw:04X}'
        suffix = f'{qualifier:X}' if qualifier else ''
        return f'{major}.{tens}{units}{suffix}'

    @property
    def comment_text(self):
        """Display ASCII comments on one line without altering stored bytes.

        Escape controls, high-bit bytes and backslashes rather than guessing
        an encoding or allowing comment data to issue terminal commands.
        """
        escapes = {9: r'\t', 10: r'\n', 13: r'\r', 92: r'\\'}
        return ''.join(escapes.get(b, chr(b) if 32 <= b <= 126
                                   else f'\\x{b:02X}') for b in self.comment)

    @property
    def names(self):
        return tuple(d.name for d in self.dibs())

    def validate(self):
        require(len(self.comment) < 65535, 'Comment is too long')
        require(0 < len(self.code) <= 65535, 'Code length must be 1..65535')
        require(len(self.relocation) <= 65535 and len(self.relocation) % 2 == 0,
                'Relocation data must contain complete 16-bit offsets')
        self.dibs()
        occupied = set()
        for (offset,) in struct.iter_unpack('<H', self.relocation):
            require(offset + 1 < len(self.code),
                    f'Relocation word at ${offset:X} is outside code')
            require(offset not in occupied and offset + 1 not in occupied,
                    f'Duplicate/overlapping relocation at ${offset:X}')
            occupied.update((offset, offset + 1))

    def pack(self):
        self.validate()
        return (le16(len(self.comment)) + self.comment + le16(len(self.code))
                + self.code + le16(len(self.relocation)) + self.relocation)

    def setslot(self, slot):
        """Return a copy with every DIB's slot updated, like a3driverutil."""
        require(type(slot) is int and slot in VALID_SLOTS,
                'Invalid slot number; expected 0, 1-4, or 255')
        self.validate()
        code = bytearray(self.code)
        for dib in self.dibs():
            code[dib.offset + DIB_SLOT_OFFSET] = slot
        result = Driver(self.comment, bytes(code), self.relocation,
                        text_preamble=self.text_preamble)
        result.validate()
        return result

    def digest(self, mode='immutable'):
        dibs = self.dibs()
        start = dibs[0].entry
        if mode == 'driv3rs':
            data = self.code[start:]
        else:
            require(mode == 'immutable', f'Unknown hash mode: {mode}')
            # OMIT rather than zero mutable bytes; this also ignores DCB size.
            spans = sorted((d.offset, d.end) for d in dibs)
            parts, pos = [], start
            for lo, hi in spans:
                if hi <= pos:
                    continue
                if lo > pos:
                    parts.append(self.code[pos:lo])
                pos = max(pos, hi)
            parts.append(self.code[pos:])
            data = b''.join(parts)
        return hashlib.md5(data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Bundle:
    header: bytes
    drivers: tuple[Driver, ...]
    trailer: bytes  # Includes the $FFFF terminator and any original padding.

    @classmethod
    def parse(cls, data):
        require(data[:8] == SIGNATURE, 'Not a SOS.DRIVER bundle (missing SOS DRVR)')
        start = 10 + word(data, 8)
        require(start + 2 <= len(data), 'Bundle header extends beyond file')
        reader = Reader(data, start)
        drivers = []
        while word(data, reader.pos) != 0xFFFF:
            drivers.append(Driver.read(reader))
        return cls(data[:start], tuple(drivers), data[reader.pos:])

    def pack(self):
        return self.header + b''.join(d.pack() for d in self.drivers) + self.trailer

    def locate(self, name):
        hits = [i for i, d in enumerate(self.drivers)
                if name.upper() in {n.upper() for n in d.names}]
        require(hits, f'Driver {name!r} not found')
        require(len(hits) == 1, f'Driver {name!r} is ambiguous ({len(hits)} records)')
        return hits[0]

    def upsert(self, incoming):
        hits = [i for i, d in enumerate(self.drivers)
                if d.name.upper() == incoming.name.upper()]
        require(len(hits) <= 1, f'Duplicate primary name {incoming.name!r} in destination')
        index = hits[0] if hits else None
        new_names = {n.upper() for n in incoming.names}
        require(len(new_names) == len(incoming.names), 'Incoming driver has duplicate device names')
        for i, existing in enumerate(self.drivers):
            if i != index:
                overlap = new_names & {n.upper() for n in existing.names}
                require(not overlap, f'Device name collision with {existing.name}: {sorted(overlap)}')
        drivers = list(self.drivers)
        if index is None:
            drivers.append(incoming)
        else:
            drivers[index] = incoming
        return Bundle(self.header, tuple(drivers), self.trailer), ('added' if index is None else 'replaced')

    def delete(self, name):
        index = self.locate(name)
        removed = self.drivers[index]
        drivers = self.drivers[:index] + self.drivers[index + 1:]
        return Bundle(self.header, drivers, self.trailer), removed


def convert_o65(data):
    """Convert the strict, zero-based a3driverutil o65 subset to SOS format."""
    reader = Reader(data)
    require(reader.take(6) == b'\x01\x00o65\x00', 'Not a version-0 o65 file')
    mode = reader.u16()
    # Permit executable/object and BSSZERO flags only (BSS must still be empty).
    require(mode & ~0x1200 == 0,
            f'Unsupported o65 mode ${mode:04X}; require 6502, 16-bit, byte-aligned, unchained')
    tbase, tlen, dbase, dlen, bbase, blen, zbase, zlen, stack = (
        reader.u16() for _ in range(9))
    require(dlen > 0, 'o65 must have a DATA driver segment')
    require(dbase == 0, 'o65 DATA must be linked at $0000 (use Apple3_o65.cfg)')
    require(blen == 0 and zlen == 0, 'o65 BSS/ZP allocation cannot be represented in a SOS driver')
    while True:
        size = reader.byte()
        if size == 0:
            break
        require(size >= 2, 'Invalid o65 header option length')
        reader.take(size - 1)  # Includes the option type, which may be binary.
    text = reader.take(tlen)
    code = reader.take(dlen)
    # TEXT is absent or a literal SOS comment preamble. Prefer the standard
    # $FFFF + uint8 length + payload layout. Historical drivers also use a
    # little-endian length word with a zero high byte; accept that only when
    # the whole TEXT length matches. Never strip a genuine leading NUL from
    # a standard comment. Normalize legacy input to the same comment bytes;
    # relocated exports reconstruct the standard one-byte-length preamble.
    # The SOS container's independent 16-bit length format is unchanged.
    text_preamble = text.startswith(TEXT_COMMENT_MARKER)
    if text:
        require(text_preamble,
                'Nonempty o65 TEXT must begin with the $FFFF comment marker')
        require(len(text) >= 3,
                'Truncated o65 TEXT comment: missing one-byte length after $FFFF')
        length = text[2]
        if len(text) == 3 + length:
            comment = text[3:]
        else:
            require(len(text) == 4 + length and text[3] == 0,
                    'o65 TEXT must contain $FFFF, a one-byte comment length '
                    '(or a legacy two-byte length with zero high byte), '
                    'and exactly that many comment bytes')
            comment = text[4:]
    else:
        comment = b''
    require(reader.u16() == 0, 'Unresolved o65 imports are not supported')
    require(reader.byte() == 0, 'TEXT relocation is not supported (comments must be literal)')
    offsets, pos, extended = [], -1, False
    while True:
        delta = reader.byte()
        if delta == 0:
            require(not extended, 'Dangling o65 relocation extension')
            break
        if delta == 255:
            pos += 254
            require(pos < dlen, 'o65 relocation extension exceeds DATA')
            extended = True
            continue
        pos += delta
        extended = False
        kind = reader.byte()
        require(kind == 0x83,
                f'Unsupported o65 fixup ${kind:02X} at ${pos:X}; only WORD/DATA ($83) is supported')
        require(pos + 1 < dlen, 'o65 relocation word exceeds DATA')
        offsets.append(pos)
    # The export table is part of o65 even when it is empty.
    for _ in range(reader.u16()):
        reader.cstring()
        segment = reader.byte()
        require(segment in (1, 2, 3, 4, 5), 'Invalid o65 exported symbol segment')
        reader.u16()
    require(reader.pos == len(data), 'Unexpected trailing/chained data in o65 file')
    driver = Driver(comment, code, b''.join(le16(p) for p in offsets),
                    text_preamble=text_preamble)
    driver.validate()
    return driver


def relocated_preamble(driver):
    """Reconstruct the optional TEXT comment field before relocated CODE.

    For an o65 source, ``Driver.text_preamble`` records whether the optional
    $FFFF marker was present, including an explicit empty ($FFFF $00) TEXT
    comment.  The SOS driver container does not store that distinction; for
    such records, preserve the established reconstruction rule: emit a
    preamble only for a nonempty comment.
    """
    if driver.text_preamble is False:
        return b''
    if driver.text_preamble is None and not driver.comment:
        return b''
    require(len(driver.comment) <= 255,
            'Relocated TEXT comment exceeds the SOS guide limit of 255 bytes')
    return TEXT_COMMENT_MARKER + bytes((len(driver.comment),)) + driver.comment


def relocated_record(driver):
    """Return reconstructed TEXT immediately before CODE fixed at $2000.

    Load at $2000 minus the preamble size. Relocation offsets and values are
    relative to CODE, not TEXT. Literal TEXT needs no address fixups. Omit
    container code/relocation lengths and the relocation table entirely.
    """
    driver.validate()
    preamble = relocated_preamble(driver)
    require(RELOCATED_CODE_BASE + len(driver.code) <= 0x10000,
            'Relocated code at $2000 would extend beyond $FFFF')
    code = bytearray(driver.code)
    for (offset,) in struct.iter_unpack('<H', driver.relocation):
        address = word(driver.code, offset) + RELOCATED_CODE_BASE
        require(address <= 0xFFFF,
                f'Relocation at code offset ${offset:04X} overflows $FFFF')
        code[offset:offset + 2] = le16(address)
    return preamble + bytes(code)


def load_diskii():
    try:
        import diskii
        from importlib.metadata import version
    except ImportError as exc:
        raise DoctorError('Image operations require: python -m pip install diskii==0.4.17') from exc
    require(version('diskii') == '0.4.17',
            'This image adapter targets diskii==0.4.17; install that version in a virtual environment')
    return diskii


@contextmanager
def open_image(path, writable=False):
    path = Path(path)
    require(path.suffix.lower() in SUPPORTED_EXTENSIONS,
            f'Unsupported image extension {path.suffix!r}; use .dsk/.do/.po/.hdv')
    require(path.is_file(), f'Image does not exist: {path}')
    diskii = load_diskii()
    with diskii.open_disk_image(path, read_only=not writable) as image:
        from diskii.types import FilesystemType
        require(image.format.filesystem == FilesystemType.PRODOS,
                'Image must have a SOS/ProDOS filesystem, not DOS or Pascal')
        yield image


def image_read(path, filename):
    with open_image(path) as image:
        return image.read_file(filename)


def normalized_path(image, filename):
    parts = filename.strip('/').split('/')
    if parts and parts[0].upper() == image.get_volume_name().upper():
        parts.pop(0)
    require(parts and all(p and p not in ('.', '..') for p in parts),
            f'Invalid image pathname: {filename!r}')
    return parts


def directory_record(image, filename):
    """Find a raw directory entry using public block I/O, not private APIs.

    Keeping the original raw metadata avoids translating SOS-specific bytes
    through ProDOS timestamp/version conventions. Only storage allocation and
    EOF fields change during replacement.
    """
    parts = normalized_path(image, filename)
    directory = 2
    for depth, part in enumerate(parts):
        header = image.read_block(directory)
        entry_length, entries_per_block = header[35], header[36]
        require(entry_length == 39 and entries_per_block == 13,
                'Unsupported SOS/ProDOS directory layout')
        block, seen, matches = directory, set(), []
        while block:
            require(block not in seen, 'Cyclic filesystem directory chain')
            seen.add(block)
            data = image.read_block(block)
            require(len(data) == 512, 'Short directory block')
            for slot in range(13):
                if block == directory and slot == 0:
                    continue
                offset = 4 + slot * 39
                entry = data[offset:offset + 39]
                storage, length = entry[0] >> 4, entry[0] & 15
                if not storage or not length:
                    continue
                name = entry[1:1 + length].decode('ascii', errors='replace')
                if name.upper() == part.upper():
                    matches.append((block, offset, entry))
            block = word(data, 2)
        require(len(matches) == 1,
                f'Image path component {part!r} missing or ambiguous')
        block, offset, entry = matches[0]
        if depth == len(parts) - 1:
            require(entry[0] >> 4 in (1, 2, 3), 'Destination must be an ordinary SOS/ProDOS file')
            return block, offset, entry
        require(entry[0] >> 4 == 13, f'{part!r} is not a directory')
        directory = word(entry, 17)
    raise DoctorError('Empty pathname')


def file_manifest(image, excluded):
    """Fingerprint all other ordinary files to catch unintended image edits."""
    excluded = '/'.join(normalized_path(image, excluded)).upper()
    result = {}
    for entry in image.get_all_files(recursive=True):
        path = entry.full_path
        key = '/'.join(normalized_path(image, path)).upper()
        if entry.is_directory or key == excluded:
            continue
        require(key not in result, f'Duplicate filesystem path: {key}')
        result[key] = hashlib.sha256(image.read_file(path)).digest()
    return result


def unique_backup(path, original):
    """Write a new backup without overwriting any previous backup."""
    number = 0
    while True:
        suffix = '.bak' if number == 0 else f'.bak.{number}'
        backup = Path(str(path) + suffix)
        try:
            stream = backup.open('xb')
        except FileExistsError:
            number += 1
            continue
        try:
            with stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup


def replace_image_file(path, filename, expected, replacement, backup=True, dry_run=False):
    """Transactional image replacement; never hand the original to a writer."""
    path = Path(path).expanduser().resolve(strict=True)
    original = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode & 0o222, f'Image is read-only: {path}')
    fd, temporary_name = tempfile.mkstemp(prefix='.' + path.stem + '.a3dmd-',
                                         suffix=path.suffix, dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
        with open_image(temporary, writable=True) as image:
            require(image.read_file(filename) == expected,
                    'Destination bundle changed while preparing the operation')
            _, _, old = directory_record(image, filename)
            untouched = file_manifest(image, filename)
            boot = (image.read_block(0), image.read_block(1))
            # Unlock only the temporary image. Keep the original access byte
            # for recreation and exact metadata restoration below.
            if (old[30] & 0xC2) != 0xC2:
                image.modify_file_attributes(filename, access=old[30] | 0xC2)
            image.delete_file(filename)
            image.create_file(filename, old[16], replacement,
                              aux_type=word(old, 31), access=old[30])
            block, offset, new = directory_record(image, filename)
            restored = bytearray(new)
            restored[0] = (new[0] & 0xF0) | (old[0] & 15)
            restored[1:17] = old[1:17]  # Name and type.
            restored[24:39] = old[24:39]  # Exact SOS metadata, including dates.
            data = bytearray(image.read_block(block))
            data[offset:offset + 39] = restored
            image.write_block(block, bytes(data))
        # Reopen to verify bytes actually persisted, not a cached in-memory file.
        with open_image(temporary) as image:
            actual = image.read_file(filename)
            require(actual == replacement, 'Read-back verification of SOS.DRIVER failed')
            require(Bundle.parse(actual).pack() == actual, 'Written bundle failed validation')
            require(file_manifest(image, filename) == untouched,
                    'Verification failed: an unrelated file changed')
            require((image.read_block(0), image.read_block(1)) == boot,
                    'Verification failed: boot blocks changed')
            _, _, check = directory_record(image, filename)
            require(check[1:17] == old[1:17] and check[24:39] == old[24:39],
                    'Verification failed: SOS file metadata changed')
        require(temporary.stat().st_size == len(original), 'Image size unexpectedly changed')
        require(path.read_bytes() == original,
                'Original image changed concurrently; refusing to commit')
        if dry_run:
            return None
        if expected == replacement:
            return None
        saved = unique_backup(path, original) if backup else None
        require(path.read_bytes() == original,
                'Original image changed concurrently; refusing to commit')
        # fsync requires a writable descriptor on platforms such as macOS.
        # Sync before restoring permissions, which may remove our write access.
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        return saved
    finally:
        temporary.unlink(missing_ok=True)


def replace_bundle_file(path, expected, replacement, backup=True, dry_run=False):
    """Validate and atomically replace a local bundle, retaining permissions."""
    path = Path(path).expanduser()
    require(not path.is_symlink(), 'Refusing to overwrite a symlink')
    path = path.resolve(strict=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode & 0o222, f'Bundle is read-only: {path}')
    require(path.read_bytes() == expected,
            'Destination bundle changed while preparing the operation')
    require(Bundle.parse(replacement).pack() == replacement,
            'Replacement bundle failed validation')
    if dry_run or expected == replacement:
        return None
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.a3dmd-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        require(temporary.read_bytes() == replacement,
                'Read-back verification of bundle failed')
        require(path.read_bytes() == expected,
                'Original bundle changed concurrently; refusing to commit')
        saved = unique_backup(path, expected) if backup else None
        require(path.read_bytes() == expected,
                'Original bundle changed concurrently; refusing to commit')
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        return saved
    finally:
        temporary.unlink(missing_ok=True)


def write_local(path, data, force=False):
    path = Path(path).expanduser()
    if not force:
        # Exclusive create protects existing files (and symlink targets).
        with path.open('xb') as stream:
            stream.write(data)
        return
    require(not path.is_symlink(), 'Refusing to overwrite a symlink')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_driver(args):
    if args.o65file:
        require(not args.inputfile and not args.driver,
                '--o65file cannot be combined with --inputfile or --driver')
        return convert_o65(Path(args.o65file).read_bytes())
    if args.relocfile:
        require(not args.inputfile and not args.driver,
                '--relocfile cannot be combined with --inputfile or --driver')
        data = Path(args.relocfile).read_bytes()
        require(not data.startswith(SIGNATURE),
                '--relocfile must be a standalone relocatable driver, not a '
                'SOS.DRIVER bundle (use --bundlefile FILE --driver NAME)')
        return Driver.parse(data)
    if args.bundlefile:
        require(not args.inputfile, '--inputfile requires --inputimage')
        data = Path(args.bundlefile).read_bytes()
        require(data.startswith(SIGNATURE),
                '--bundlefile must be a SOS.DRIVER bundle beginning with SOS DRVR')
    else:
        data = image_read(args.inputimage, args.inputfile or 'SOS.DRIVER')
    if data.startswith(SIGNATURE):
        require(args.driver, 'A bundle source requires --driver NAME (use list to inspect it)')
        bundle = Bundle.parse(data)
        return bundle.drivers[bundle.locate(args.driver)]
    driver = Driver.parse(data)
    if args.driver:
        require(args.driver.upper() in {n.upper() for n in driver.names},
                f'Standalone driver does not contain {args.driver!r}')
    return driver


def print_digest(driver, mode):
    profile = 'immutable-v1' if mode == 'immutable' else 'driv3rs-code'
    print(f'{driver.name}  md5[{profile}]={driver.digest(mode)}')


def argument_parser():
    class DoubleDashParser(argparse.ArgumentParser):
        """Use only long options, including help, for every subcommand."""
        def __init__(self, *args, **kwargs):
            kwargs['add_help'] = False
            kwargs['allow_abbrev'] = False
            super().__init__(*args, **kwargs)
            self.add_argument('--help', action='help',
                              help='Show this help message and exit')

    parser = DoubleDashParser(prog='a3dmd', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument('--version', action='version', version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest='command', required=True)

    def option(p, name, **kwargs):
        return p.add_argument('--' + name, **kwargs)

    def hashing(p):
        p.add_argument('--hash-mode', choices=('immutable', 'driv3rs'), default='immutable',
                       help='immutable-v1 (default) or historical Driv3rs code_md5')

    def source(p):
        group = p.add_mutually_exclusive_group(required=True)
        option(group, 'inputimage', help='Source disk image')
        option(group, 'o65file', help='Local o65 file using Apple3_o65.cfg layout')
        option(group, 'relocfile', help='Local standalone SOS relocatable driver')
        option(group, 'bundlefile', help='Local SOS.DRIVER bundle')
        option(p, 'inputfile', help='Source path within image (default SOS.DRIVER)')
        option(p, 'driver', help='Device name to select from a bundle')
        hashing(p)

    def safety(p):
        p.add_argument('--dry-run', action='store_true',
                       help='Validate a temporary image update without committing')
        p.add_argument('--no-backup', action='store_true', help='Do not create an image backup')

    p = sub.add_parser('add', help='Insert or replace by primary driver name', allow_abbrev=False)
    source(p)
    option(p, 'outputimage', required=True, help='Existing destination image to update in place')
    option(p, 'outputfile', default='SOS.DRIVER', help='Destination bundle path WITHIN image')
    safety(p)

    p = sub.add_parser('delete', help='Remove a complete driver record in place', allow_abbrev=False)
    option(p, 'inputimage', required=True)
    option(p, 'inputfile', default='SOS.DRIVER')
    option(p, 'driver', required=True)
    hashing(p)
    safety(p)

    p = sub.add_parser('setslot', help='Set the slot in all DIBs of a driver in place',
                       allow_abbrev=False)
    group = p.add_mutually_exclusive_group(required=True)
    option(group, 'bundlefile', help='Local SOS.DRIVER bundle to update in place')
    option(group, 'inputimage', help='Disk image to update in place')
    option(p, 'inputfile', help='Bundle path within image (default SOS.DRIVER)')
    option(p, 'driver', required=True,
           help='Primary or secondary device name (updates the entire driver record)')
    option(p, 'slot', required=True, type=int, choices=VALID_SLOTS,
           help='Slot value: 0, 1-4, or 255 (decimal)')
    hashing(p)
    p.add_argument('--dry-run', action='store_true',
                   help='Validate the bundle/image update without committing')
    p.add_argument('--no-backup', action='store_true',
                   help='Do not create a backup of the bundle or image')

    p = sub.add_parser('extract', help='Save a driver; optionally relocate for disassembly', allow_abbrev=False)
    source(p)
    option(p, 'outputfile', required=True, help='LOCAL output filename')
    p.add_argument('--force', action='store_true', help='Overwrite an existing local output file')
    p.add_argument('--relocate', action='store_true',
                   help='Reconstruct an o65 TEXT $FFFF comment preamble when present; for SOS '
                        'container sources, emit one only for a nonempty comment. CODE is '
                        'fixed at $2000. Print the calculated binary load address. '
                        'Omit container length words and relocation table '
                        '(disassembly only, not installation)')

    p = sub.add_parser('convert', help='Convert a local o65 file to SOS relocatable format', allow_abbrev=False)
    option(p, 'o65file', required=True)
    option(p, 'outputfile', required=True, help='LOCAL output filename')
    p.add_argument('--force', action='store_true')
    hashing(p)

    p = sub.add_parser('hash', help='Print a driver identification MD5', allow_abbrev=False)
    source(p)

    p = sub.add_parser('list', help='List driver records and their secondary devices', allow_abbrev=False)
    group = p.add_mutually_exclusive_group(required=True)
    option(group, 'inputimage')
    option(group, 'bundlefile')
    option(p, 'inputfile', default='SOS.DRIVER')
    hashing(p)
    return parser


def run(args):
    if args.command == 'list':
        data = (Path(args.bundlefile).read_bytes() if args.bundlefile
                else image_read(args.inputimage, args.inputfile))
        bundle = Bundle.parse(data)
        comments = [driver.comment_text for driver in bundle.drivers]
        comment_width = max([len('COMMENT')] + [len(text) for text in comments])
        print(f'{"PRIMARY":15} {"VERSION":7} {"COMMENT":{comment_width}} '
              f'{"CODE":>6} {"RELOC":>6}  MD5 (' + args.hash_mode + ') / DEVICES')
        for driver, comment in zip(bundle.drivers, comments):
            print(f'{driver.name:15} {driver.version:7} {comment:{comment_width}} '
                  f'{len(driver.code):6} {len(driver.relocation):6}  '
                  f'{driver.digest(args.hash_mode)}  {", ".join(driver.names)}')
        print(f'{len(bundle.drivers)} driver records; {len(data)} bundle bytes')
        return 0
    if args.command == 'setslot':
        require(not args.bundlefile or args.inputfile is None,
                '--inputfile requires --inputimage')
        filename = args.inputfile or 'SOS.DRIVER'
        old = (Path(args.bundlefile).expanduser().read_bytes() if args.bundlefile
               else image_read(args.inputimage, filename))
        bundle = Bundle.parse(old)
        index = bundle.locate(args.driver)
        driver = bundle.drivers[index]
        changed = driver.setslot(args.slot)
        drivers = list(bundle.drivers)
        drivers[index] = changed
        new = Bundle(bundle.header, tuple(drivers), bundle.trailer).pack()
        print_digest(changed, args.hash_mode)
        if args.bundlefile:
            saved = replace_bundle_file(args.bundlefile, old, new,
                                        backup=not args.no_backup, dry_run=args.dry_run)
            target = args.bundlefile
        else:
            saved = replace_image_file(args.inputimage, filename, old, new,
                                       backup=not args.no_backup, dry_run=args.dry_run)
            target = f'{args.inputimage}:{filename}'
        if new == old:
            print(f'{driver.name}: slot already {args.slot}; no changes to {target}')
        else:
            action = 'Would set' if args.dry_run else 'Set'
            print(f'{action} {driver.name} slot to {args.slot} '
                  f'(devices: {", ".join(driver.names)}) in {target}')
        if saved:
            print(f'Backup: {saved}')
        return 0
    if args.command == 'delete':
        image, filename = args.inputimage, args.inputfile
        old = image_read(image, filename)
        bundle, removed = Bundle.parse(old).delete(args.driver)
        print_digest(removed, args.hash_mode)
        new = bundle.pack()
        saved = replace_image_file(image, filename, old, new,
                                   backup=not args.no_backup, dry_run=args.dry_run)
        print(f'{"Would delete" if args.dry_run else "Deleted"} {removed.name} '
              f'(devices: {", ".join(removed.names)}) from {image}:{filename}')
        if saved:
            print(f'Backup: {saved}')
        return 0
    if args.command == 'convert':
        driver = convert_o65(Path(args.o65file).read_bytes())
    else:
        driver = source_driver(args)
    print_digest(driver, args.hash_mode)
    if args.command == 'hash':
        return 0
    if args.command in ('extract', 'convert'):
        source_paths = [getattr(args, name, None) for name in
                        ('inputimage', 'o65file', 'relocfile', 'bundlefile')]
        destination = Path(args.outputfile).resolve()
        require(all(destination != Path(p).resolve() for p in source_paths if p),
                'Local output must not overwrite the source image/file')
        relocate = args.command == 'extract' and args.relocate
        data = relocated_record(driver) if relocate else driver.pack()
        write_local(args.outputfile, data, force=args.force)
        print(f'Wrote {len(data)} bytes to {args.outputfile}')
        if relocate:
            code_base = RELOCATED_CODE_BASE
            text_base = code_base - len(relocated_preamble(driver))
            entry = code_base + driver.dibs()[0].entry
            print(f'Disassembly load address: ${text_base:04X}; '
                  f'code/DIB base: ${code_base:04X}; primary entry: ${entry:04X}')
            print('Disassembly only: not an installable relocatable SOS driver. '
                  'Container code/relocation lengths and relocation table omitted.')
        return 0
    old = image_read(args.outputimage, args.outputfile)
    bundle, action = Bundle.parse(old).upsert(driver)
    new = bundle.pack()
    # Conservative SOS address-space ceiling; no silent length wrapping.
    require(len(new) <= 65535, f'Resulting bundle is {len(new)} bytes; exceeds 65535-byte safety limit')
    saved = replace_image_file(args.outputimage, args.outputfile, old, new,
                               backup=not args.no_backup, dry_run=args.dry_run)
    prefix = 'Dry run: would have ' if args.dry_run else ''
    print(f'{prefix}{action.capitalize() if not prefix else action} {driver.name} '
          f'in {args.outputimage}:{args.outputfile} ({len(new)} bytes)')
    if saved:
        print(f'Backup: {saved}')
    return 0


def main(argv=None):
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (DoctorError, OSError, ValueError) as exc:
        print(f'a3dmd: error: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        # diskii exceptions do not all inherit OSError. Keep CLI failures concise;
        # importing this module and calling functions directly preserves tracebacks.
        print(f'a3dmd: error ({type(exc).__name__}): {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
