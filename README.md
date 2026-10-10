# a3driverdoctor (`a3dmd.py`)

**a3driverdoctor** is a Swiss Army Knife for working with Apple /// SOS device drivers.  It lets you add, subtract, inspect, identify, display, and extract device drivers to and from disk images. It can manipulate assembled relocatable object files as drivers, locate a relocatable driver to aid in disassembly, and convert ca65-produced o65 file format to SOS relocatable format.  It does all this with disk images directly, so you don't need to do any other filesystem extraction or insertion of disk images manually using other tools.

Its central working format is the **SOS relocatable driver**: one driver record containing its comment, code, and relocation data. A driver may support multiple devices through linked Device Information Blocks (DIBs). A driver may exist inside a floppy's SOS.DRIVER bundle, or on a floppy filesystem as a standalone file, or on your local filesystem after assembly, among other places.  The idea is to move, convert, or inspect any driver in any format existing any place.  The more modern o65 relocatable file format used by the ca65 assembler as pioneered by [Rob Justice](https://github.com/robjustice/a3driverutil) is gratefully acknowledged.

`a3dmd.py` is a standalone Python 3 script, and when installed in a Python environment via [PyPI](https://pypi.org/project/a3driverdoctor/) it is invoked simply as `a3dmd`.

## Requirements

- **Python 3.11 or newer**.
- **`diskii>=0.4.17`** for disk image operations.

To minimally run:
```bash
python3 -m pip install diskii
python3 a3dmd.py --help
```

To install in a Python virtual environment:
```bash
pip install a3driverdoctor
a3dmd --help
```

## Commands

```text
python3 a3dmd.py COMMAND [OPTIONS]
```

| Command | Purpose | Required arguments |
|---|---|---|
| `list` | List records in a bundle, including versions, comments, hashes, and devices | `--inputimage` or `--bundlefile` |
| `hash` | Print a selected driver's name, hash profile, and MD5 | One source; `--driver` for a bundle |
| `extract` | Save a selected driver as a local SOS relocatable file or disassembly-ready relocated binary | One source; `--driver` for a bundle; `--outputfile` `--relocate`|
| `convert` | Convert a local o65-formatted driver file to a local SOS relocatable file | `--o65file`, `--outputfile` |
| `add` | Insert or replace a driver in an existing image bundle | One source; `--driver` for a bundle; `--outputimage` |
| `delete` | Remove a complete driver record from an image bundle | `--inputimage`, `--driver` |

### Source options

For `add`, `extract`, and `hash`, choose **exactly one** of these sources:

| Option | Meaning |
|---|---|
| `--inputimage PATH` | Read a bundle or standalone relocatable driver from a disk image |
| `--bundlefile PATH` | Read a local `SOS.DRIVER` bundle |
| `--relocfile PATH` | Read a local standalone SOS relocatable driver |
| `--o65file PATH` | Read a local `PATH` o65 file and internally convert it to a SOS relocatable driver |

`list` accepts only `--inputimage` or `--bundlefile`, and requires a bundle. `delete` operates on a bundle in `--inputimage`. `convert` accepts only `--o65file`.

### Option dictionary

| Option | Commands | Meaning / default |
|---|---|---|
| `--inputfile PATH` | `add`, `extract`, `hash`, `list`, `delete` | Source pathname **inside the image**; defaults to `SOS.DRIVER`. Use with `--inputimage`. |
| `--driver NAME` | `add`, `extract`, `hash`, `delete` | Select a record by primary or secondary device name, case-insensitively. Required for bundle sources and deletion. For an image-based standalone driver, optional and checked against its device names. Do not combine with `--relocfile` or `--o65file`. |
| `--outputimage PATH` | `add` | Existing destination image to update in place; required. |
| `--outputfile PATH` | `add` | Destination bundle pathname **inside the image**; defaults to `SOS.DRIVER`. The bundle must already exist. |
| `--outputfile PATH` | `extract`, `convert` | **Local filesystem** output filename; required. Writes SOS relocatable format. |
| `--relocate` | `extract` | Reconstruct `$FFFF` + one-byte comment length + comment before CODE fixed at `$2000`; print the calculated binary load address. |
| `--hash-mode MODE` | All | `immutable` (default) or `driv3rs` algorithm; displays the calculated hash with given algorithm. |
| `--dry-run` | `add`, `delete` | Perform and verify the update on a temporary image, without committing or creating a backup. Requires writable image permissions and space for the temporary copy. |
| `--no-backup` | `add`, `delete` | Disable automatic image backup. |
| `--force` | `extract`, `convert` | Allow replacement of an existing local output file. Overwriting the source or a symlink is still refused. |
| `--help` | Top level or any command | Show general or command-specific help. |
| `--version` | Top level only | Print the program version and exit. |

## Usage examples

### Inspect a bundle

```bash
python3 a3dmd.py list --inputimage system.dsk
python3 a3dmd.py list --bundlefile SOS.DRIVER
```

Columns appear in this order:

```text
PRIMARY  VERSION  COMMENT  CODE  RELOC  MD5 (immutable) / DEVICES
```

- `PRIMARY`: the first DIB's device name.
- `VERSION`: the primary DIB's SOS version, such as `1.04` or `1.16C`; nonstandard values are shown as hexadecimal words.
- `COMMENT`: the record's comment, with control characters, non-ASCII bytes, and backslashes escaped for one-line display.
- `CODE` and `RELOC`: code and relocation lengths **in bytes**.
- `MD5` and `DEVICES`: the selected hash and all device names, including the primary.

### Extract a relocatable driver

From a bundle on an image:

```bash
python3 a3dmd.py extract --inputimage system.dsk \
  --driver .CONSOLE --outputfile console.driver
```

From a standalone file on an image:

```bash
python3 a3dmd.py extract --inputimage utilities.dsk \
  --inputfile CONS.DRVR --outputfile console.driver
```

From a local bundle, allowing an existing output file to be replaced:

```bash
python3 a3dmd.py extract --bundlefile SOS.DRIVER \
  --driver .CONSOLE --outputfile console.driver --force
```

### Convert an assembled o65 driver

```bash
python3 a3dmd.py convert --o65file mydriver.o65 \
  --outputfile mydriver.DRVR
```

Conversion requires the **a3driverutil `Apple3_o65.cfg` convention**, not an arbitrary o65 executable:

The tool does not assemble or link source code.

### Add or replace a driver

Transfer a selected record between image bundles:

```bash
python3 a3dmd.py add --inputimage utilities.dsk \
  --inputfile SOS.DRIVER --driver .CONSOLE --outputimage test.dsk
```

Install a standalone driver from one image to the SOS.DRIVER of another:

```bash
python3 a3dmd.py add --inputimage utilities.dsk \
  --inputfile CONS.DRVR --outputimage test.dsk
```

Install a local relocatable driver to the SOS.DRIVER of a destination disk image:

```bash
python3 a3dmd.py add --relocfile mydriver.DRVR --outputimage test.dsk
```

Convert and install o65 format driver directly; first validate without committing:

```bash
python3 a3dmd.py add --o65file mydriver.o65 --outputimage test.dsk --dry-run
python3 a3dmd.py add --o65file mydriver.o65 --outputimage test.dsk
```

**Replacement matches the incoming primary device name, case-insensitively.** An existing record is replaced in its original position; a new record is appended.

### Delete a driver

```bash
python3 a3dmd.py delete --inputimage test.dsk --driver .CONSOLE --dry-run
python3 a3dmd.py delete --inputimage test.dsk --driver .CONSOLE
```

Selecting a secondary device selects its **whole owning driver record**. Extraction and deletion therefore include all devices belonging to that record. Ambiguous names are errors.

### Identify a driver

```bash
python3 a3dmd.py hash --inputimage system.dsk --driver .CONSOLE
python3 a3dmd.py hash --inputimage utilities.dsk --inputfile CONS.DRVR
python3 a3dmd.py hash --bundlefile SOS.DRIVER --driver .CONSOLE
python3 a3dmd.py hash --relocfile mydriver.DRVR
python3 a3dmd.py hash --o65file mydriver.o65 --hash-mode driv3rs
```

`hash` prints one line containing the primary name, profile label, and hexadecimal digest. Other driver operations also report the selected hash.

| Mode | Output profile | Bytes hashed |
|---|---|---|
| `immutable` | `immutable-v1` | Code from the primary entry point onward, omitting every linked DIB and its inline Device Configuration Block (DCB) within that range |
| `driv3rs` | `driv3rs-code` | All code bytes from the primary entry point onward, matching historical Driv3rs `code_md5` |

Both profiles exclude comments, record lengths, relocation records, and configuration before the primary entry point. The default additionally excludes secondary-device descriptors and configuration, **not the executable code supporting those devices**. Driver-specific mutable data outside DIB/DCB regions remains included.

MD5 is used for identification, **not security or authenticity**.


## Command help

```bash
python3 a3dmd.py add --help
python3 a3dmd.py --version
```


## References

- [a3driverutil](https://github.com/robjustice/a3driverutil): SOS driver tooling and o65 layout convention.
- [Driv3rs](https://github.com/thecompu/Driv3rs): historical driver identification hash.
- [o65 format](http://6502.org/users/andre/o65/): relocatable object format specification.
- [diskii 0.4.17](https://pypi.org/project/diskii/0.4.17/): disk-image access library.
