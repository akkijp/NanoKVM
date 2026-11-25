import importlib.util
import pathlib
import struct
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "verify-elf-deps.py"


def load():
    spec = importlib.util.spec_from_file_location("verify_elf_deps", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elf_header(machine):
    """A 64-bit little-endian ELF header with no program headers (statically linked)."""
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 0x12, machine)
    struct.pack_into("<QHH", header, 0x20, 64, 0, 0)  # e_phoff (unused), then a fake gap
    struct.pack_into("<HH", header, 0x36, 56, 0)       # e_phentsize, e_phnum
    return bytes(header)


class NeededTest(unittest.TestCase):
    def test_a_library_for_another_cpu_is_refused(self):
        m = load()
        self.assertEqual(m.needed(elf_header(243)), [])  # RISC-V, no dynamic section
        with self.assertRaisesRegex(ValueError, "another CPU"):
            m.needed(elf_header(62))  # x86-64


if __name__ == "__main__":
    unittest.main()
