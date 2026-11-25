#!/usr/bin/env python3
"""akkijp fork: check that every shared library the packaged programs need is there.

Reads the DT_NEEDED entries of the server and kvm_system of a release tarball, following them
through the libraries the package ships, and requires each one to be found where that program
finds it on the device:
  - the server: server/dl_lib or the device's own system libraries;
  - kvm_system: the system libraries only. It runs the first-start migration before the server's
    libraries are in place (S95nanokvm), so it must not need any of server/dl_lib.
The system libraries are listed in scripts/device-system-libs.txt (taken from the device image).
A library dropped from a later version is fine as long as nothing needs it any more.

Usage: scripts/verify-elf-deps.py <nanokvm_<version>.tar.gz>
       scripts/verify-elf-deps.py --unresolved <tarball>   (prints what is neither shipped nor listed)
"""
import pathlib
import struct
import sys
import tarfile

HERE = pathlib.Path(__file__).resolve().parent
SYSTEM_LIBS = HERE / "device-system-libs.txt"
PROGRAMS = ("server/NanoKVM-Server", "kvm_system/kvm_system")
DT_NEEDED, DT_STRTAB = 1, 5
PT_LOAD, PT_DYNAMIC = 1, 2


def needed(data: bytes) -> list:
    """DT_NEEDED names of a 64-bit little-endian ELF (the device is riscv64)."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not a 64-bit little-endian ELF")
    phoff, = struct.unpack_from("<Q", data, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    loads, dynamic = [], None
    for i in range(phnum):
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack_from("<IIQQQQ", data, phoff + i * phentsize)
        if p_type == PT_LOAD:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return []  # statically linked

    def offset_of(vaddr):
        for start, offset, size in loads:
            if start <= vaddr < start + size:
                return offset + (vaddr - start)
        raise ValueError(f"address {vaddr:#x} is outside every PT_LOAD")

    entries, strtab = [], None
    offset, size = dynamic
    for pos in range(offset, offset + size, 16):
        tag, value = struct.unpack_from("<qQ", data, pos)
        if tag == 0:
            break
        if tag == DT_NEEDED:
            entries.append(value)
        elif tag == DT_STRTAB:
            strtab = offset_of(value)
    if entries and strtab is None:
        raise ValueError("DT_NEEDED without DT_STRTAB")
    names = []
    for value in entries:
        start = strtab + value
        names.append(data[start:data.index(b"\0", start)].decode())
    return names


def main() -> int:
    args = sys.argv[1:]
    report = args[:1] == ["--unresolved"]
    if report:
        args = args[1:]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    text = SYSTEM_LIBS.read_text() if SYSTEM_LIBS.exists() or not report else ""
    system = {line.split("#", 1)[0].strip() for line in text.splitlines()} - {""}
    files = {}
    with tarfile.open(args[0], "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if rel in PROGRAMS or rel.startswith("server/dl_lib/"):
                files[rel] = tar.extractfile(member).read()
    shipped = {rel.rsplit("/", 1)[1]: rel for rel in files if rel.startswith("server/dl_lib/")}
    missing = [p for p in PROGRAMS if p not in files]
    if missing:
        print(f"[ERROR] {args[0]}: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    unresolved, seen = {}, set()
    # (program, where its libraries are found)
    for program, search in (("server/NanoKVM-Server", shipped), ("kvm_system/kvm_system", {})):
        todo, done = [program], set()
        while todo:
            rel = todo.pop()
            if rel in done:
                continue
            done.add(rel)
            seen.add(rel)
            try:
                names = needed(files[rel])
            except ValueError as e:
                print(f"[ERROR] {rel}: {e}", file=sys.stderr)
                return 1
            for name in names:
                if name in search:
                    todo.append(search[name])
                elif name not in system:
                    unresolved.setdefault(name, []).append(rel)
    if report:
        for name in sorted(unresolved):
            print(name)
        return 0
    if unresolved:
        for name, users in sorted(unresolved.items()):
            print(f"[ERROR] {name} (needed by {', '.join(sorted(users))}) is neither in server/dl_lib "
                  f"nor a device system library ({SYSTEM_LIBS.name})", file=sys.stderr)
        return 1
    print(f"[OK] {args[0]}: every needed library is shipped or on the device ({len(seen)} files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
