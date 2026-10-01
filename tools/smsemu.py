#!/usr/bin/env python3
"""Minimal headless SEGA Master System emulator for testing the demo.

Emulates the Z80 (documented instructions + IX/IY), the VDP in TMS9918
legacy modes (Graphics II only is rendered), frame interrupts, V counter,
and controller port 1.  Takes PNG screenshots and reports statistics:

  * number of page flips (writes to VDP register 2) -> effective frame rate
  * VRAM accesses closer together than the safe limit during active display

Usage:
  smsemu.py rom.sms --frames 600 --shot 120,300 --outdir shots
            [--input "400:b1,700:b2,800-900:left"] [--sym rom.sym]
"""
import argparse
import os
import struct
import sys
import zlib

# ---------------------------------------------------------------- flags
FS, FZ, F5, FH, F3, FPV, FN, FC = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01

SZ = [0] * 256
SZP = [0] * 256
for _v in range(256):
    s = (_v & 0x80) | (FZ if _v == 0 else 0)
    SZ[_v] = s
    p = bin(_v).count('1') % 2 == 0
    SZP[_v] = s | (FPV if p else 0)

# base cycle counts for unprefixed opcodes (branches: not-taken value)
CYC = [
    4, 10, 7, 6, 4, 4, 7, 4, 4, 11, 7, 6, 4, 4, 7, 4,
    8, 10, 7, 6, 4, 4, 7, 4, 12, 11, 7, 6, 4, 4, 7, 4,
    7, 10, 16, 6, 4, 4, 7, 4, 7, 11, 16, 6, 4, 4, 7, 4,
    7, 10, 13, 6, 11, 11, 10, 4, 7, 11, 13, 6, 4, 4, 7, 4,
] + ([4, 4, 4, 4, 4, 4, 7, 4] * 8) + ([4, 4, 4, 4, 4, 4, 7, 4] * 8) + [
    5, 10, 10, 10, 10, 11, 7, 11, 5, 10, 10, 0, 10, 17, 7, 11,
    5, 10, 10, 11, 10, 11, 7, 11, 5, 4, 10, 11, 10, 0, 7, 11,
    5, 10, 10, 19, 10, 11, 7, 11, 5, 4, 10, 4, 10, 0, 7, 11,
    5, 10, 10, 4, 10, 11, 7, 11, 5, 6, 10, 4, 10, 0, 7, 11,
]
for _i in range(0x70, 0x78):
    CYC[_i] = 7
CYC[0x76] = 4


class EmuError(Exception):
    pass


class Z80:
    def __init__(self, sms):
        self.sms = sms
        self.mem = sms.mem
        self.r = [0] * 8          # b c d e h l f a
        self.alt = [0xFF] * 8
        self.ix = self.iy = 0xFFFF
        self.sp = 0xDFF0
        self.pc = 0
        self.i = 0
        self.rreg = 0
        self.iff1 = self.iff2 = False
        self.im = 0
        self.halted = False
        self.ei_delay = False
        self.cycles = 0

    # -- memory
    def rb(self, a):
        return self.mem[a & 0xFFFF]

    def wb(self, a, v):
        a &= 0xFFFF
        if a >= 0xC000:
            self.mem[a] = v
            self.mem[a ^ 0x2000] = v
        # writes to ROM area are ignored (no mapper needed for 32KB ROM)

    def rw(self, a):
        return self.mem[a & 0xFFFF] | (self.mem[(a + 1) & 0xFFFF] << 8)

    def ww(self, a, v):
        self.wb(a, v & 0xFF)
        self.wb(a + 1, (v >> 8) & 0xFF)

    def fetch(self):
        v = self.mem[self.pc]
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetchw(self):
        v = self.mem[self.pc] | (self.mem[(self.pc + 1) & 0xFFFF] << 8)
        self.pc = (self.pc + 2) & 0xFFFF
        return v

    def push(self, v):
        self.sp = (self.sp - 2) & 0xFFFF
        self.ww(self.sp, v)

    def pop(self):
        v = self.rw(self.sp)
        self.sp = (self.sp + 2) & 0xFFFF
        return v

    # -- register pairs
    def get_rp(self, p, pre):
        r = self.r
        if p == 0:
            return (r[0] << 8) | r[1]
        if p == 1:
            return (r[2] << 8) | r[3]
        if p == 2:
            if pre == 0xDD:
                return self.ix
            if pre == 0xFD:
                return self.iy
            return (r[4] << 8) | r[5]
        return self.sp

    def set_rp(self, p, v, pre):
        r = self.r
        v &= 0xFFFF
        if p == 0:
            r[0], r[1] = v >> 8, v & 0xFF
        elif p == 1:
            r[2], r[3] = v >> 8, v & 0xFF
        elif p == 2:
            if pre == 0xDD:
                self.ix = v
            elif pre == 0xFD:
                self.iy = v
            else:
                r[4], r[5] = v >> 8, v & 0xFF
        else:
            self.sp = v

    def get_rp2(self, p, pre):
        if p == 3:
            return (self.r[7] << 8) | self.r[6]
        return self.get_rp(p, pre)

    def set_rp2(self, p, v, pre):
        if p == 3:
            self.r[7], self.r[6] = (v >> 8) & 0xFF, v & 0xFF
        else:
            self.set_rp(p, v, pre)

    def hl(self):
        return (self.r[4] << 8) | self.r[5]

    # 8-bit register access; z==6 means memory at addr
    def get8(self, z, pre, addr):
        if z == 6:
            return self.mem[addr]
        if pre and z in (4, 5):
            v = self.ix if pre == 0xDD else self.iy
            return (v >> 8) if z == 4 else (v & 0xFF)
        return self.r[z]

    def set8(self, z, v, pre, addr):
        if z == 6:
            self.wb(addr, v)
            return
        if pre and z in (4, 5):
            if pre == 0xDD:
                self.ix = ((v << 8) | (self.ix & 0xFF)) if z == 4 else ((self.ix & 0xFF00) | v)
            else:
                self.iy = ((v << 8) | (self.iy & 0xFF)) if z == 4 else ((self.iy & 0xFF00) | v)
            return
        self.r[z] = v

    def idx_addr(self, pre):
        d = self.fetch()
        if d >= 128:
            d -= 256
        base = self.ix if pre == 0xDD else self.iy
        return (base + d) & 0xFFFF

    # -- ALU
    def alu(self, y, v):
        r = self.r
        a = r[7]
        f = r[6]
        if y == 0 or y == 1:            # add / adc
            c = (f & FC) if y == 1 else 0
            res = a + v + c
            rf = SZ[res & 0xFF] | (FC if res > 0xFF else 0)
            if ((a & 0xF) + (v & 0xF) + c) > 0xF:
                rf |= FH
            if (~(a ^ v) & (a ^ res)) & 0x80:
                rf |= FPV
            r[7] = res & 0xFF
            r[6] = rf
        elif y in (2, 3, 7):          # sub / sbc / cp
            c = (f & FC) if y == 3 else 0
            res = a - v - c
            rf = SZ[res & 0xFF] | FN | (FC if res < 0 else 0)
            if ((a & 0xF) - (v & 0xF) - c) < 0:
                rf |= FH
            if ((a ^ v) & (a ^ res)) & 0x80:
                rf |= FPV
            if y != 7:
                r[7] = res & 0xFF
            r[6] = rf
        elif y == 4:
            res = a & v
            r[7] = res
            r[6] = SZP[res] | FH
        elif y == 5:
            res = a ^ v
            r[7] = res
            r[6] = SZP[res]
        else:
            res = a | v
            r[7] = res
            r[6] = SZP[res]

    def inc8(self, v):
        res = (v + 1) & 0xFF
        f = (self.r[6] & FC) | SZ[res]
        if (v & 0xF) == 0xF:
            f |= FH
        if res == 0x80:
            f |= FPV
        self.r[6] = f
        return res

    def dec8(self, v):
        res = (v - 1) & 0xFF
        f = (self.r[6] & FC) | SZ[res] | FN
        if (v & 0xF) == 0:
            f |= FH
        if res == 0x7F:
            f |= FPV
        self.r[6] = f
        return res

    def rot(self, y, v):
        f = self.r[6]
        if y == 0:      # rlc
            c = v >> 7
            res = ((v << 1) | c) & 0xFF
        elif y == 1:    # rrc
            c = v & 1
            res = (v >> 1) | (c << 7)
        elif y == 2:    # rl
            c = v >> 7
            res = ((v << 1) | (f & FC)) & 0xFF
        elif y == 3:    # rr
            c = v & 1
            res = (v >> 1) | ((f & FC) << 7)
        elif y == 4:    # sla
            c = v >> 7
            res = (v << 1) & 0xFF
        elif y == 5:    # sra
            c = v & 1
            res = (v >> 1) | (v & 0x80)
        elif y == 6:    # sll (undocumented)
            c = v >> 7
            res = ((v << 1) | 1) & 0xFF
        else:           # srl
            c = v & 1
            res = v >> 1
        self.r[6] = SZP[res] | c
        return res

    def add16(self, a, b):
        res = a + b
        f = self.r[6] & (FS | FZ | FPV)
        if res > 0xFFFF:
            f |= FC
        if ((a & 0xFFF) + (b & 0xFFF)) > 0xFFF:
            f |= FH
        self.r[6] = f
        return res & 0xFFFF

    def adc16(self, a, b):
        c = self.r[6] & FC
        res = a + b + c
        f = (FC if res > 0xFFFF else 0)
        r16 = res & 0xFFFF
        f |= (FS if r16 & 0x8000 else 0) | (FZ if r16 == 0 else 0)
        if ((a & 0xFFF) + (b & 0xFFF) + c) > 0xFFF:
            f |= FH
        if (~(a ^ b) & (a ^ res)) & 0x8000:
            f |= FPV
        self.r[6] = f
        return r16

    def sbc16(self, a, b):
        c = self.r[6] & FC
        res = a - b - c
        f = FN | (FC if res < 0 else 0)
        r16 = res & 0xFFFF
        f |= (FS if r16 & 0x8000 else 0) | (FZ if r16 == 0 else 0)
        if ((a & 0xFFF) - (b & 0xFFF) - c) < 0:
            f |= FH
        if ((a ^ b) & (a ^ res)) & 0x8000:
            f |= FPV
        self.r[6] = f
        return r16

    def cond(self, y):
        f = self.r[6]
        if y == 0: return not (f & FZ)
        if y == 1: return bool(f & FZ)
        if y == 2: return not (f & FC)
        if y == 3: return bool(f & FC)
        if y == 4: return not (f & FPV)
        if y == 5: return bool(f & FPV)
        if y == 6: return not (f & FS)
        return bool(f & FS)

    # -- interrupts
    def interrupt(self):
        self.halted = False
        self.iff1 = self.iff2 = False
        self.push(self.pc)
        if self.im == 2:
            self.pc = self.rw((self.i << 8) | 0xFF)
            return 19
        self.pc = 0x38
        return 13

    def nmi(self):
        self.halted = False
        self.iff2 = self.iff1
        self.iff1 = False
        self.push(self.pc)
        self.pc = 0x66
        return 11

    # -- main step
    def step(self):
        if self.halted:
            return 4
        self.rreg = (self.rreg + 1) & 0x7F
        op = self.fetch()
        pre = 0
        extra = 0
        while op == 0xDD or op == 0xFD:
            pre = op
            extra += 4
            op = self.fetch()
        if op == 0xCB:
            return extra + self.exec_cb(pre)
        if op == 0xED:
            return self.exec_ed()
        return extra + self.exec_main(op, pre)

    def exec_main(self, op, pre):
        r = self.r
        x = op >> 6
        y = (op >> 3) & 7
        z = op & 7
        p = y >> 1
        q = y & 1
        cyc = CYC[op]

        if x == 1:
            if op == 0x76:
                self.halted = True
                return 4
            addr = 0
            if y == 6 or z == 6:
                if pre:
                    addr = self.idx_addr(pre)
                    cyc = 19
                else:
                    addr = self.hl()
                # with (ix+d) the other register is plain h/l
                if y == 6:
                    self.set8(6, self.get8(z, 0, addr), 0, addr)
                else:
                    self.set8(y, self.get8(6, 0, addr), 0, addr)
                return cyc
            self.set8(y, self.get8(z, pre, 0), pre, 0)
            return cyc

        if x == 2:
            if z == 6:
                if pre:
                    addr = self.idx_addr(pre)
                    cyc = 19
                else:
                    addr = self.hl()
                self.alu(y, self.mem[addr])
            else:
                self.alu(y, self.get8(z, pre, 0))
            return cyc

        if x == 0:
            if z == 0:
                if y == 0:
                    return 4
                if y == 1:
                    for k in (6, 7):
                        r[k], self.alt[k] = self.alt[k], r[k]
                    return 4
                if y == 2:
                    d = self.fetch()
                    r[0] = (r[0] - 1) & 0xFF
                    if r[0]:
                        self.pc = (self.pc + (d - 256 if d >= 128 else d)) & 0xFFFF
                        return 13
                    return 8
                d = self.fetch()
                if y == 3 or self.cond(y - 4):
                    self.pc = (self.pc + (d - 256 if d >= 128 else d)) & 0xFFFF
                    return 12
                return 7
            if z == 1:
                if q == 0:
                    self.set_rp(p, self.fetchw(), pre)
                    return 14 if (pre and p == 2) else 10
                v = self.add16(self.get_rp(2, pre), self.get_rp(p, pre))
                self.set_rp(2, v, pre)
                return 15 if pre else 11
            if z == 2:
                if q == 0:
                    if p == 0:
                        self.wb(self.get_rp(0, 0), r[7])
                    elif p == 1:
                        self.wb(self.get_rp(1, 0), r[7])
                    elif p == 2:
                        self.ww(self.fetchw(), self.get_rp(2, pre))
                    else:
                        self.wb(self.fetchw(), r[7])
                else:
                    if p == 0:
                        r[7] = self.rb(self.get_rp(0, 0))
                    elif p == 1:
                        r[7] = self.rb(self.get_rp(1, 0))
                    elif p == 2:
                        self.set_rp(2, self.rw(self.fetchw()), pre)
                    else:
                        r[7] = self.rb(self.fetchw())
                return cyc
            if z == 3:
                v = self.get_rp(p, pre)
                self.set_rp(p, v + (1 if q == 0 else -1), pre)
                return 10 if pre else 6
            if z == 4 or z == 5:
                fn = self.inc8 if z == 4 else self.dec8
                if y == 6:
                    addr = self.idx_addr(pre) if pre else self.hl()
                    self.wb(addr, fn(self.mem[addr]))
                    return 23 if pre else 11
                self.set8(y, fn(self.get8(y, pre, 0)), pre, 0)
                return 4
            if z == 6:
                if y == 6:
                    addr = self.idx_addr(pre) if pre else self.hl()
                    self.wb(addr, self.fetch())
                    return 19 if pre else 10
                self.set8(y, self.fetch(), pre, 0)
                return 7
            # z == 7
            a = r[7]
            f = r[6]
            if y == 0:
                c = a >> 7
                r[7] = ((a << 1) | c) & 0xFF
                r[6] = (f & (FS | FZ | FPV)) | c
            elif y == 1:
                c = a & 1
                r[7] = (a >> 1) | (c << 7)
                r[6] = (f & (FS | FZ | FPV)) | c
            elif y == 2:
                c = a >> 7
                r[7] = ((a << 1) | (f & FC)) & 0xFF
                r[6] = (f & (FS | FZ | FPV)) | c
            elif y == 3:
                c = a & 1
                r[7] = (a >> 1) | ((f & FC) << 7)
                r[6] = (f & (FS | FZ | FPV)) | c
            elif y == 4:   # daa
                corr = 0
                c = f & FC
                if (f & FH) or (a & 0xF) > 9:
                    corr |= 0x06
                if c or a > 0x99:
                    corr |= 0x60
                    c = FC
                if f & FN:
                    h = FH if (f & FH) and (a & 0xF) < 6 else 0
                    res = (a - corr) & 0xFF
                else:
                    h = FH if (a & 0xF) > 9 else 0
                    res = (a + corr) & 0xFF
                r[7] = res
                r[6] = SZP[res] | h | c | (f & FN)
            elif y == 5:
                r[7] = a ^ 0xFF
                r[6] = f | FH | FN
            elif y == 6:
                r[6] = (f & (FS | FZ | FPV)) | FC
            else:
                r[6] = (f & (FS | FZ | FPV)) | ((f & FC) << 4) | ((f & FC) ^ 1)
            return 4

        # x == 3
        if z == 0:
            if self.cond(y):
                self.pc = self.pop()
                return 11
            return 5
        if z == 1:
            if q == 0:
                self.set_rp2(p, self.pop(), pre)
                return 14 if pre else 10
            if p == 0:
                self.pc = self.pop()
                return 10
            if p == 1:
                for k in range(6):
                    r[k], self.alt[k] = self.alt[k], r[k]
                return 4
            if p == 2:
                self.pc = self.get_rp(2, pre)
                return 8 if pre else 4
            self.sp = self.get_rp(2, pre)
            return 10 if pre else 6
        if z == 2:
            nn = self.fetchw()
            if self.cond(y):
                self.pc = nn
            return 10
        if z == 3:
            if y == 0:
                self.pc = self.fetchw()
                return 10
            if y == 2:
                self.sms.port_out(self.fetch(), r[7], self.cycles)
                return 11
            if y == 3:
                r[7] = self.sms.port_in(self.fetch(), self.cycles)
                return 11
            if y == 4:
                v = self.rw(self.sp)
                self.ww(self.sp, self.get_rp(2, pre))
                self.set_rp(2, v, pre)
                return 23 if pre else 19
            if y == 5:
                r[2], r[4] = r[4], r[2]
                r[3], r[5] = r[5], r[3]
                return 4
            if y == 6:
                self.iff1 = self.iff2 = False
                return 4
            if y == 7:
                self.iff1 = self.iff2 = True
                self.ei_delay = True
                return 4
        if z == 4:
            nn = self.fetchw()
            if self.cond(y):
                self.push(self.pc)
                self.pc = nn
                return 17
            return 10
        if z == 5:
            if q == 0:
                self.push(self.get_rp2(p, pre))
                return 15 if pre else 11
            if p == 0:
                nn = self.fetchw()
                self.push(self.pc)
                self.pc = nn
                return 17
            raise EmuError("unexpected prefix at %04X" % self.pc)
        if z == 6:
            self.alu(y, self.fetch())
            return 7
        self.push(self.pc)
        self.pc = y * 8
        return 11

    def exec_cb(self, pre):
        if pre:
            addr = self.idx_addr(pre)
            op = self.fetch()
        else:
            op = self.fetch()
            addr = self.hl()
        x = op >> 6
        y = (op >> 3) & 7
        z = op & 7
        src = 6 if pre else z
        v = self.get8(src, 0, addr)
        if x == 1:
            f = self.r[6] & FC
            f |= FH
            if not (v & (1 << y)):
                f |= FZ | FPV
            elif y == 7:
                f |= FS
            self.r[6] = f
            return 20 if pre else (12 if z == 6 else 8)
        if x == 0:
            res = self.rot(y, v)
        elif x == 2:
            res = v & ~(1 << y) & 0xFF
        else:
            res = v | (1 << y)
        self.set8(src, res, 0, addr)
        if pre and z != 6:
            self.r[z] = res
        return 23 if pre else (15 if z == 6 else 8)

    def exec_ed(self):
        op = self.fetch()
        r = self.r
        x = op >> 6
        y = (op >> 3) & 7
        z = op & 7
        p = y >> 1
        q = y & 1
        if x == 1:
            if z == 0:
                v = self.sms.port_in(r[1], self.cycles)
                if y != 6:
                    r[y] = v
                r[6] = (r[6] & FC) | SZP[v]
                return 12
            if z == 1:
                self.sms.port_out(r[1], 0 if y == 6 else r[y], self.cycles)
                return 12
            if z == 2:
                hl = self.hl()
                if q == 0:
                    v = self.sbc16(hl, self.get_rp(p, 0))
                else:
                    v = self.adc16(hl, self.get_rp(p, 0))
                r[4], r[5] = v >> 8, v & 0xFF
                return 15
            if z == 3:
                nn = self.fetchw()
                if q == 0:
                    self.ww(nn, self.get_rp(p, 0))
                else:
                    self.set_rp(p, self.rw(nn), 0)
                return 20
            if z == 4:
                a = r[7]
                r[7] = 0
                self.alu(2, a)
                return 8
            if z == 5:
                self.iff1 = self.iff2
                self.pc = self.pop()
                return 14
            if z == 6:
                self.im = {0: 0, 1: 0, 2: 1, 3: 2}[y & 3]
                return 8
            if y == 0:
                self.i = r[7]
                return 9
            if y == 1:
                self.rreg = r[7]
                return 9
            if y == 2 or y == 3:
                v = self.i if y == 2 else self.rreg
                r[7] = v
                r[6] = (r[6] & FC) | SZ[v] | (FPV if self.iff2 else 0)
                return 9
            if y == 4 or y == 5:
                hl = self.hl()
                m = self.mem[hl]
                a = r[7]
                if y == 4:   # rrd
                    nm = ((a & 0xF) << 4) | (m >> 4)
                    a = (a & 0xF0) | (m & 0xF)
                else:        # rld
                    nm = ((m << 4) & 0xF0) | (a & 0xF)
                    a = (a & 0xF0) | (m >> 4)
                self.wb(hl, nm)
                r[7] = a
                r[6] = (r[6] & FC) | SZP[a]
                return 18
            return 8
        if x == 2 and z <= 3 and y >= 4:
            inc = 1 if (y & 1) == 0 else -1
            rep = y >= 6
            hl = self.hl()
            bc = self.get_rp(0, 0)
            de = self.get_rp(1, 0)
            if z == 0:      # ldi/ldd
                self.wb(de, self.mem[hl])
                hl = (hl + inc) & 0xFFFF
                de = (de + inc) & 0xFFFF
                bc = (bc - 1) & 0xFFFF
                self.set_rp(1, de, 0)
                self.set_rp(0, bc, 0)
                r[4], r[5] = hl >> 8, hl & 0xFF
                r[6] = (r[6] & (FS | FZ | FC)) | (FPV if bc else 0)
                if rep and bc:
                    self.pc = (self.pc - 2) & 0xFFFF
                    return 21
                return 16
            if z == 1:      # cpi/cpd
                v = self.mem[hl]
                a = r[7]
                res = (a - v) & 0xFF
                hl = (hl + inc) & 0xFFFF
                bc = (bc - 1) & 0xFFFF
                self.set_rp(0, bc, 0)
                r[4], r[5] = hl >> 8, hl & 0xFF
                f = (r[6] & FC) | SZ[res] | FN | (FPV if bc else 0)
                if (a & 0xF) < (v & 0xF):
                    f |= FH
                r[6] = f
                if rep and bc and res:
                    self.pc = (self.pc - 2) & 0xFFFF
                    return 21
                return 16
            if z == 2:      # ini/ind
                v = self.sms.port_in(r[1], self.cycles)
                self.wb(hl, v)
            else:           # outi/outd
                v = self.mem[hl]
                r[0] = (r[0] - 1) & 0xFF
                self.sms.port_out(r[1], v, self.cycles)
            if z == 2:
                r[0] = (r[0] - 1) & 0xFF
            hl = (hl + inc) & 0xFFFF
            r[4], r[5] = hl >> 8, hl & 0xFF
            r[6] = (r[6] & FC) | SZ[r[0]] | FN
            if rep and r[0]:
                self.pc = (self.pc - 2) & 0xFFFF
                return 21
            return 16
        return 8


# ---------------------------------------------------------------- VDP

TMS_PALETTE = [
    (0, 0, 0), (0, 0, 0), (33, 200, 66), (94, 220, 120),
    (84, 85, 237), (125, 118, 252), (212, 82, 77), (66, 235, 245),
    (252, 85, 84), (255, 121, 120), (212, 193, 84), (230, 206, 128),
    (33, 176, 59), (201, 91, 186), (204, 204, 204), (255, 255, 255),
]

ACTIVE_ACCESS_MIN = 26   # minimum CPU cycles between VRAM accesses in active display


class VDP:
    def __init__(self):
        self.vram = bytearray(0x4000)
        self.reg = [0] * 16
        self.latch = False
        self.first = 0
        self.addr = 0
        self.code = 0
        self.buffer = 0
        self.status = 0
        self.line = 0
        self.last_access = -1000
        self.violations = 0
        self.min_gap = 10 ** 9
        self.reg2_writes = 0

    def display_on(self):
        return bool(self.reg[1] & 0x40)

    def check_access(self, cycles):
        if self.line < 192 and self.display_on():
            gap = cycles - self.last_access
            if gap < self.min_gap:
                self.min_gap = gap
            if gap < ACTIVE_ACCESS_MIN:
                self.violations += 1
        self.last_access = cycles

    def ctrl_write(self, v):
        if not self.latch:
            self.first = v
            self.addr = (self.addr & 0x3F00) | v
            self.latch = True
            return
        self.latch = False
        self.code = v >> 6
        self.addr = ((v & 0x3F) << 8) | self.first
        if self.code == 0:
            self.buffer = self.vram[self.addr]
            self.addr = (self.addr + 1) & 0x3FFF
        elif self.code == 2:
            reg = v & 0x0F
            if reg < 11:
                self.reg[reg] = self.first
                if reg == 2:
                    self.reg2_writes += 1

    def data_write(self, v, cycles):
        self.latch = False
        self.check_access(cycles)
        self.buffer = v
        if self.code == 3:
            return  # CRAM (mode 4 only) ignored
        self.vram[self.addr] = v
        self.addr = (self.addr + 1) & 0x3FFF

    def data_read(self, cycles):
        self.latch = False
        self.check_access(cycles)
        v = self.buffer
        self.buffer = self.vram[self.addr]
        self.addr = (self.addr + 1) & 0x3FFF
        return v

    def status_read(self):
        v = self.status | 0x1F
        self.status = 0
        self.latch = False
        return v

    def irq(self):
        return bool(self.status & 0x80) and bool(self.reg[1] & 0x20)

    def vcounter(self):
        ln = self.line
        return ln if ln <= 0xDA else ln - 6

    def render(self):
        """Returns list of 192 rows of 256 color indices."""
        reg = self.reg
        backdrop = reg[7] & 0x0F
        blank = [[backdrop] * 256 for _ in range(192)]
        if not self.display_on():
            return blank
        if reg[0] & 0x04:
            raise EmuError("Mode 4 rendering not supported by this test emulator")
        if not (reg[0] & 0x02) or (reg[1] & 0x18):
            raise EmuError("only Graphics II (mode 2) is rendered")
        vram = self.vram
        nt = (reg[2] & 0x0F) << 10
        ct = (reg[3] & 0x80) << 6
        cmask = ((reg[3] & 0x7F) << 3) | 0x07
        pg = (reg[4] & 0x04) << 11
        pmask = ((reg[4] & 0x03) << 8) | 0xFF
        rows = []
        for y in range(192):
            row = []
            third = (y >> 6) << 8
            ly = y & 7
            nbase = nt + (y >> 3) * 32
            for col in range(32):
                name = vram[nbase + col] + third
                pat = vram[pg + ((name & pmask) << 3) + ly]
                colr = vram[ct + ((name & cmask) << 3) + ly]
                fg = colr >> 4 or backdrop
                bg = colr & 0x0F or backdrop
                for bit in range(7, -1, -1):
                    row.append(fg if (pat >> bit) & 1 else bg)
            rows.append(row)
        return rows


def write_png(path, rows, scale=2):
    h = len(rows)
    w = len(rows[0])
    raw = bytearray()
    for row in rows:
        line = bytearray([0])
        for c in row:
            rgb = bytes(TMS_PALETTE[c])
            line += rgb * scale
        for _ in range(scale):
            raw += line
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w * scale, h * scale, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(raw), 9))
    png += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)


# ---------------------------------------------------------------- system

JOY_BITS = {'up': 0x01, 'down': 0x02, 'left': 0x04, 'right': 0x08, 'b1': 0x10, 'b2': 0x20}


class SMS:
    CYCLES_PER_LINE = 228
    LINES = 262

    def __init__(self, rom):
        self.mem = bytearray(0x10000)
        n = min(len(rom), 0xC000)
        self.mem[:n] = rom[:n]
        self.vdp = VDP()
        self.cpu = Z80(self)
        self.joy = 0xFF
        self.unknown_ports = set()

    def port_in(self, port, cycles):
        port &= 0xFF
        if port < 0x40:
            return 0xFF
        if port < 0x80:
            return self.vdp.vcounter() if not (port & 1) else 0
        if port < 0xC0:
            return self.vdp.data_read(cycles) if not (port & 1) else self.vdp.status_read()
        return self.joy if not (port & 1) else 0xFF

    def port_out(self, port, v, cycles):
        port &= 0xFF
        if port < 0x40:
            return
        if port < 0x80:
            return  # PSG
        if port < 0xC0:
            if port & 1:
                self.vdp.ctrl_write(v)
            else:
                self.vdp.data_write(v, cycles)
            return
        self.unknown_ports.add(port)

    def run_frame(self):
        cpu = self.cpu
        vdp = self.vdp
        frame_img = None
        for line in range(self.LINES):
            vdp.line = line
            if line == 192:
                frame_img = vdp.render()
                vdp.status |= 0x80
            target = cpu.cycles + self.CYCLES_PER_LINE
            while cpu.cycles < target:
                if cpu.iff1 and not cpu.ei_delay and vdp.irq():
                    cpu.cycles += cpu.interrupt()
                    continue
                cpu.ei_delay = False
                pc = cpu.pc
                if pc < 0x0000 or (0xC000 <= pc):
                    raise EmuError("PC ran into RAM at %04X" % pc)
                cpu.cycles += cpu.step()
        return frame_img


def parse_input(spec):
    """'400:b1,700-760:left' -> list of (start, end, mask)"""
    events = []
    if not spec:
        return events
    for item in spec.split(','):
        rng, btn = item.split(':')
        if '-' in rng:
            a, b = rng.split('-')
            a, b = int(a), int(b)
        else:
            a = int(rng)
            b = a + 10
        events.append((a, b, JOY_BITS[btn]))
    return events


def load_syms(path):
    syms = []
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                a, n = line.split()
                syms.append((int(a, 16), n))
    syms.sort()
    return syms


def sym_for(syms, addr):
    best = None
    for a, n in syms:
        if a <= addr and not n.startswith(('VDP', 'PORT')):
            best = (a, n)
    if best:
        return "%s+%d" % (best[1], addr - best[0])
    return "?"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('rom')
    ap.add_argument('--frames', type=int, default=300)
    ap.add_argument('--shot', default='')
    ap.add_argument('--outdir', default='shots')
    ap.add_argument('--input', default='')
    ap.add_argument('--sym')
    ap.add_argument('--scale', type=int, default=2)
    args = ap.parse_args()

    with open(args.rom, 'rb') as f:
        rom = f.read()
    sms = SMS(rom)
    shots = set(int(s) for s in args.shot.split(',') if s)
    events = parse_input(args.input)
    syms = load_syms(args.sym)
    if shots:
        os.makedirs(args.outdir, exist_ok=True)

    flips_at = []
    try:
        for frame in range(args.frames):
            mask = 0
            for a, b, m in events:
                if a <= frame < b:
                    mask |= m
            sms.joy = 0xFF & ~mask
            before = sms.vdp.reg2_writes
            img = sms.run_frame()
            if sms.vdp.reg2_writes != before:
                flips_at.append(frame)
            if frame in shots:
                path = os.path.join(args.outdir, 'frame_%04d.png' % frame)
                write_png(path, img, args.scale)
                print("saved", path)
    except EmuError as e:
        print("EMULATION ERROR at frame %d: %s  (PC=%04X %s)" %
              (frame, e, sms.cpu.pc, sym_for(syms, sms.cpu.pc)))
        sys.exit(1)

    vdp = sms.vdp
    print("frames emulated     : %d" % args.frames)
    print("page flips          : %d" % len(flips_at))
    if len(flips_at) > 2:
        span = flips_at[-1] - flips_at[1]
        n = len(flips_at) - 2
        if n > 0:
            print("avg frames per flip : %.2f  (~%.1f fps @60Hz)" % (span / n, 60.0 * n / span))
    print("VRAM access min gap : %s cycles (active display)" %
          (vdp.min_gap if vdp.min_gap < 10 ** 9 else 'n/a'))
    print("VRAM timing issues  : %d (gaps < %d cycles during active display)" %
          (vdp.violations, ACTIVE_ACCESS_MIN))
    print("final PC            : %04X %s" % (sms.cpu.pc, sym_for(syms, sms.cpu.pc)))
    if sms.unknown_ports:
        print("writes to unhandled ports:", sorted(hex(p) for p in sms.unknown_ports))


if __name__ == '__main__':
    main()
