#!/usr/bin/env python3
"""Writes the checksum into the SEGA header ("TMR SEGA" at 0x7FF0) of a 32KB ROM.

The export SMS BIOS refuses to boot cartridges with a wrong checksum.
Checksum = 16-bit sum of bytes 0x0000-0x7FEF (ROM size code 0xC = 32KB).
"""
import sys


def main():
    path = sys.argv[1]
    with open(path, 'rb') as f:
        rom = bytearray(f.read())
    if len(rom) != 0x8000 or rom[0x7FF0:0x7FF8] != b'TMR SEGA':
        sys.exit("error: expected a 32KB ROM with 'TMR SEGA' at 0x7FF0")
    csum = sum(rom[:0x7FF0]) & 0xFFFF
    rom[0x7FFA] = csum & 0xFF
    rom[0x7FFB] = csum >> 8
    with open(path, 'wb') as f:
        f.write(rom)
    print("%s: checksum 0x%04X" % (path, csum))


if __name__ == '__main__':
    main()
