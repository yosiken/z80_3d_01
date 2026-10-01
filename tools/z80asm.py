#!/usr/bin/env python3
"""Small two-pass Z80 assembler (sjasmplus-like syntax subset).

Supported:
  * all documented Z80 instructions (incl. IX/IY, CB/ED prefixed)
  * labels "name:" and local labels ".name:" (scoped to the last global label)
  * directives: org, equ, db/defb, dw/defw, ds/defs, align, include, incbin, end
  * expressions: + - * / % & | ^ ~ << >> ( ), $ (current address),
    numbers 123, 0x1F, $1F, 1Fh, %0101, 0b0101, 'c'

Usage: z80asm.py input.asm -o output.bin [--sym out.sym] [--size N] [--fill 0xFF]
"""
import argparse
import os
import re
import sys


class AsmError(Exception):
    pass


REG8 = {'b': 0, 'c': 1, 'd': 2, 'e': 3, 'h': 4, 'l': 5, 'a': 7}
RP = {'bc': 0, 'de': 1, 'hl': 2, 'sp': 3}
RP2 = {'bc': 0, 'de': 1, 'hl': 2, 'af': 3}
CC = {'nz': 0, 'z': 1, 'nc': 2, 'c': 3, 'po': 4, 'pe': 5, 'p': 6, 'm': 7}
ALU = {'add': 0, 'adc': 1, 'sub': 2, 'sbc': 3, 'and': 4, 'xor': 5, 'or': 6, 'cp': 7}
ROT = {'rlc': 0, 'rrc': 1, 'rl': 2, 'rr': 3, 'sla': 4, 'sra': 5, 'sll': 6, 'srl': 7}
IDX = {'ix': 0xDD, 'iy': 0xFD}

SIMPLE = {
    'nop': [0x00], 'rlca': [0x07], 'rrca': [0x0F], 'rla': [0x17], 'rra': [0x1F],
    'daa': [0x27], 'cpl': [0x2F], 'scf': [0x37], 'ccf': [0x3F], 'halt': [0x76],
    'di': [0xF3], 'ei': [0xFB], 'exx': [0xD9],
    'neg': [0xED, 0x44], 'retn': [0xED, 0x45], 'reti': [0xED, 0x4D],
    'rrd': [0xED, 0x67], 'rld': [0xED, 0x6F],
    'ldi': [0xED, 0xA0], 'cpi': [0xED, 0xA1], 'ini': [0xED, 0xA2], 'outi': [0xED, 0xA3],
    'ldd': [0xED, 0xA8], 'cpd': [0xED, 0xA9], 'ind': [0xED, 0xAA], 'outd': [0xED, 0xAB],
    'ldir': [0xED, 0xB0], 'cpir': [0xED, 0xB1], 'inir': [0xED, 0xB2], 'otir': [0xED, 0xB3],
    'lddr': [0xED, 0xB8], 'cpdr': [0xED, 0xB9], 'indr': [0xED, 0xBA], 'otdr': [0xED, 0xBB],
}


# ---------------------------------------------------------------- expressions

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+|\$[0-9a-fA-F]+|%[01]+|[0-9][0-9a-fA-F]*[hH]\b|[0-9]+)
  | (?P<chr>'(?:\\.|[^'\\])')
  | (?P<id>\.?[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><<|>>|==|!=|<=|>=|[-+*/%&|^~()$<>])
""", re.VERBOSE)


def tokenize(s):
    pos = 0
    out = []
    while pos < len(s):
        m = TOKEN_RE.match(s, pos)
        if not m:
            raise AsmError("bad expression near '%s'" % s[pos:])
        pos = m.end()
        kind = m.lastgroup
        if kind == 'ws':
            continue
        out.append((kind, m.group(kind)))
    return out


def parse_number(t):
    tl = t.lower()
    if tl.startswith('0x'):
        return int(t[2:], 16)
    if tl.startswith('0b'):
        return int(t[2:], 2)
    if t.startswith('$'):
        return int(t[1:], 16)
    if t.startswith('%'):
        return int(t[1:], 2)
    if tl.endswith('h'):
        return int(t[:-1], 16)
    return int(t, 10)


class ExprParser:
    BINOPS = [
        ('==', '!=', '<', '>', '<=', '>='),
        ('|',), ('^',), ('&',), ('<<', '>>'), ('+', '-'), ('*', '/', '%'),
    ]

    def __init__(self, asm, text):
        self.asm = asm
        self.toks = tokenize(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        v = self.binary(0)
        if self.i != len(self.toks):
            raise AsmError("unexpected token '%s'" % self.peek()[1])
        return v

    def binary(self, level):
        if level == len(self.BINOPS):
            return self.unary()
        v = self.binary(level + 1)
        while True:
            k, t = self.peek()
            if k == 'op' and t in self.BINOPS[level]:
                self.take()
                r = self.binary(level + 1)
                if t == '==': v = int(v == r)
                elif t == '!=': v = int(v != r)
                elif t == '<': v = int(v < r)
                elif t == '>': v = int(v > r)
                elif t == '<=': v = int(v <= r)
                elif t == '>=': v = int(v >= r)
                elif t == '|': v = v | r
                elif t == '^': v = v ^ r
                elif t == '&': v = v & r
                elif t == '<<': v = v << r
                elif t == '>>': v = v >> r
                elif t == '+': v = v + r
                elif t == '-': v = v - r
                elif t == '*': v = v * r
                elif t == '/':
                    if r == 0:
                        if self.asm.final:
                            raise AsmError("division by zero")
                        v = 0
                    else:
                        v = int(v / r)
                elif t == '%':
                    v = v % r if r else 0
            else:
                return v

    def unary(self):
        k, t = self.take()
        if k == 'op' and t == '-':
            return -self.unary()
        if k == 'op' and t == '+':
            return self.unary()
        if k == 'op' and t == '~':
            return ~self.unary()
        if k == 'op' and t == '(':
            v = self.binary(0)
            k2, t2 = self.take()
            if t2 != ')':
                raise AsmError("missing ')'")
            return v
        if k == 'op' and t == '$':
            return self.asm.pc
        if k == 'num':
            return parse_number(t)
        if k == 'chr':
            c = t[1:-1]
            if c.startswith('\\'):
                c = {'n': '\n', 't': '\t', '0': '\0'}.get(c[1], c[1])
            return ord(c)
        if k == 'id':
            return self.asm.lookup(t)
        raise AsmError("unexpected token '%s'" % t)


# ---------------------------------------------------------------- helpers

def strip_comment(line):
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == '\\' else 1
            out.append(line[i:j + 1])
            i = j + 1
            continue
        if ch == "'" and i + 2 < n and line[i + 2] == "'":
            out.append(line[i:i + 3])
            i += 3
            continue
        if ch == ';':
            break
        out.append(ch)
        i += 1
    return ''.join(out).rstrip()


def split_operands(s):
    ops = []
    depth = 0
    cur = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            j = i + 1
            while j < len(s) and s[j] != '"':
                j += 2 if s[j] == '\\' else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if ch == "'" and i + 2 < len(s) and s[i + 2] == "'":
            cur.append(s[i:i + 3])
            i += 3
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            ops.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur or ops:
        ops.append(''.join(cur).strip())
    return ops


def is_wrapped(s):
    """True if s is '( ... )' with the outer parens matching each other."""
    if not (s.startswith('(') and s.endswith(')')):
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return False
    return True


# ---------------------------------------------------------------- assembler

class Operand:
    """kind: r8, rp, ind (register indirect), idx ((ix+d)), mem ((nn)), imm, cond-able names"""

    def __init__(self, kind, val=None, expr=None, prefix=None):
        self.kind = kind
        self.val = val
        self.expr = expr
        self.prefix = prefix

    def __repr__(self):
        return "Op(%s,%r,%r)" % (self.kind, self.val, self.expr)


class Assembler:
    def __init__(self):
        self.symbols = {}
        self.final = False
        self.pc = 0
        self.out = {}
        self.last_global = ''
        self.errors = []

    # -- symbols
    def full_name(self, name):
        if name.startswith('.'):
            return self.last_global + name
        return name

    def lookup(self, name):
        fn = self.full_name(name)
        if fn in self.symbols:
            return self.symbols[fn]
        if self.final:
            raise AsmError("undefined symbol '%s'" % name)
        return 0

    def define(self, name, value):
        fn = self.full_name(name)
        if not name.startswith('.'):
            self.last_global = name
        if self.final:
            if self.symbols.get(fn) != value:
                raise AsmError("symbol '%s' changed between passes (%r -> %r)" %
                               (fn, self.symbols.get(fn), value))
        else:
            if fn in self.symbols and self.pass_defined.get(fn):
                raise AsmError("duplicate symbol '%s'" % fn)
            self.symbols[fn] = value
            self.pass_defined[fn] = True

    def eval(self, text):
        return ExprParser(self, text).parse()

    # -- output
    def emit(self, data):
        for b in data:
            if self.final:
                if self.pc in self.out:
                    raise AsmError("overlapping output at 0x%04X" % self.pc)
                self.out[self.pc] = b & 0xFF
            self.pc += 1

    def byte(self, v, what='value'):
        if self.final and not (-128 <= v <= 255):
            raise AsmError("%s out of byte range: %d" % (what, v))
        return v & 0xFF

    def word(self, v):
        if self.final and not (-32768 <= v <= 65535):
            raise AsmError("value out of word range: %d" % v)
        return [v & 0xFF, (v >> 8) & 0xFF]

    # -- operands
    def parse_operand(self, s):
        sl = s.lower().replace(' ', '')
        if sl in REG8:
            return Operand('r8', REG8[sl])
        if sl in ('ixh', 'ixl', 'iyh', 'iyl'):
            return Operand('r8', 4 if sl[2] == 'h' else 5, prefix=IDX[sl[:2]])
        if sl in RP or sl == 'af':
            return Operand('rp', sl)
        if sl in IDX:
            return Operand('rp', sl, prefix=IDX[sl])
        if sl in ("af'",):
            return Operand('rp', "af'")
        if sl in ('i', 'r'):
            return Operand('special', sl)
        if is_wrapped(s.strip()):
            inner = s.strip()[1:-1].strip()
            il = inner.lower().replace(' ', '')
            if il == 'hl':
                return Operand('r8', 6)
            if il in ('bc', 'de', 'sp', 'c'):
                return Operand('ind', il)
            m = re.match(r'^(ix|iy)\s*([-+].*)?$', inner.strip(), re.I)
            if m:
                disp = m.group(2) or '0'
                return Operand('idx', 6, expr=disp, prefix=IDX[m.group(1).lower()])
            return Operand('mem', expr=inner)
        return Operand('imm', expr=s)

    def disp_bytes(self, op):
        d = self.eval(op.expr)
        if self.final and not (-128 <= d <= 127):
            raise AsmError("index displacement out of range: %d" % d)
        return d & 0xFF

    def r8_encode(self, op):
        """Returns (prefix_list, code, disp_list) for an r8 / (hl) / (ix+d) operand."""
        if op.kind == 'r8':
            return ([op.prefix] if op.prefix else []), op.val, []
        if op.kind == 'idx':
            return [op.prefix], 6, [self.disp_bytes(op)]
        raise AsmError("expected 8-bit register operand")

    def is_r8(self, op):
        return op.kind in ('r8', 'idx')

    def imm8(self, op):
        return self.byte(self.eval(op.expr))

    def imm16(self, op):
        return self.word(self.eval(op.expr))

    def rel(self, op, size):
        target = self.eval(op.expr)
        d = target - (self.pc + size)
        if self.final and not (-128 <= d <= 127):
            raise AsmError("relative jump out of range (%d)" % d)
        return d & 0xFF

    def hl_like(self, op):
        """'hl', 'ix', 'iy' rp operand -> prefix list or None"""
        if op.kind == 'rp' and op.val == 'hl':
            return []
        if op.kind == 'rp' and op.val in IDX:
            return [IDX[op.val]]
        return None

    def rp_code(self, op, table=RP):
        """rp operand -> (prefix, code). ix/iy map to hl slot."""
        if op.kind != 'rp':
            raise AsmError("expected register pair")
        if op.val in IDX:
            return [IDX[op.val]], 2
        if op.val not in table:
            raise AsmError("bad register pair '%s'" % op.val)
        return [], table[op.val]

    # -- instructions
    def encode(self, mn, ops):
        n = len(ops)
        if mn in SIMPLE and n == 0:
            return list(SIMPLE[mn])

        if mn == 'ld':
            return self.enc_ld(ops)

        if mn in ('push', 'pop'):
            pre, code = self.rp_code(ops[0], RP2)
            base = 0xC5 if mn == 'push' else 0xC1
            return pre + [base | code << 4]

        if mn in ALU:
            if n == 2:
                if mn in ('add', 'adc', 'sbc') and ops[0].kind == 'rp':
                    return self.enc_alu16(mn, ops)
                if not (ops[0].kind == 'r8' and ops[0].val == 7):
                    raise AsmError("%s: first operand must be a" % mn)
                src = ops[1]
            elif n == 1:
                src = ops[0]
            else:
                raise AsmError("bad operand count")
            y = ALU[mn]
            if self.is_r8(src):
                pre, code, disp = self.r8_encode(src)
                return pre + [0x80 | y << 3 | code] + disp
            return [0xC6 | y << 3, self.imm8(src)]

        if mn in ('inc', 'dec'):
            op = ops[0]
            if op.kind == 'rp':
                pre, code = self.rp_code(op)
                return pre + [(0x03 if mn == 'inc' else 0x0B) | code << 4]
            pre, code, disp = self.r8_encode(op)
            return pre + [(0x04 if mn == 'inc' else 0x05) | code << 3] + disp

        if mn in ROT:
            pre, code, disp = self.r8_encode(ops[0])
            opc = ROT[mn] << 3 | code
            if disp:
                return pre + [0xCB] + disp + [opc]
            return pre + [0xCB, opc]

        if mn in ('bit', 'set', 'res'):
            b = self.eval(ops[0].expr)
            if not 0 <= b <= 7:
                raise AsmError("bit number out of range")
            pre, code, disp = self.r8_encode(ops[1])
            base = {'bit': 0x40, 'res': 0x80, 'set': 0xC0}[mn]
            opc = base | b << 3 | code
            if disp:
                return pre + [0xCB] + disp + [opc]
            return pre + [0xCB, opc]

        if mn == 'jp':
            if n == 1:
                op = ops[0]
                if op.kind == 'r8' and op.val == 6:
                    return [0xE9]
                if op.kind == 'idx' or (op.kind == 'mem' and op.expr.strip().lower() in IDX):
                    p = op.prefix if op.kind == 'idx' else IDX[op.expr.strip().lower()]
                    return [p, 0xE9]
                return [0xC3] + self.imm16(op)
            cc = self.cond(ops[0])
            return [0xC2 | cc << 3] + self.imm16(ops[1])

        if mn == 'jr':
            if n == 1:
                return [0x18, self.rel(ops[0], 2)]
            cc = self.cond(ops[0])
            if cc > 3:
                raise AsmError("jr: invalid condition")
            return [0x20 | cc << 3, self.rel(ops[1], 2)]

        if mn == 'djnz':
            return [0x10, self.rel(ops[0], 2)]

        if mn == 'call':
            if n == 1:
                return [0xCD] + self.imm16(ops[0])
            cc = self.cond(ops[0])
            return [0xC4 | cc << 3] + self.imm16(ops[1])

        if mn == 'ret':
            if n == 0:
                return [0xC9]
            return [0xC0 | self.cond(ops[0]) << 3]

        if mn == 'rst':
            v = self.eval(ops[0].expr)
            if v & ~0x38:
                raise AsmError("bad rst vector")
            return [0xC7 | v]

        if mn == 'im':
            v = self.eval(ops[0].expr)
            return [0xED, {0: 0x46, 1: 0x56, 2: 0x5E}[v]]

        if mn == 'ex':
            a, b = ops
            if a.kind == 'rp' and a.val == 'de' and b.kind == 'rp' and b.val == 'hl':
                return [0xEB]
            if a.kind == 'rp' and a.val == 'af' and b.kind == 'rp' and b.val == "af'":
                return [0x08]
            if a.kind == 'ind' and a.val == 'sp':
                pre = self.hl_like(b)
                if pre is not None:
                    return pre + [0xE3]
            raise AsmError("bad ex operands")

        if mn == 'in':
            if n == 2 and ops[1].kind == 'mem':
                if not (ops[0].kind == 'r8' and ops[0].val == 7):
                    raise AsmError("in r,(n) only valid for a")
                return [0xDB, self.byte(self.eval(ops[1].expr))]
            if n == 2 and ops[1].kind == 'ind' and ops[1].val == 'c':
                return [0xED, 0x40 | ops[0].val << 3]
            raise AsmError("bad in operands")

        if mn == 'out':
            if ops[0].kind == 'mem':
                if not (ops[1].kind == 'r8' and ops[1].val == 7):
                    raise AsmError("out (n),r only valid for a")
                return [0xD3, self.byte(self.eval(ops[0].expr))]
            if ops[0].kind == 'ind' and ops[0].val == 'c':
                return [0xED, 0x41 | ops[1].val << 3]
            raise AsmError("bad out operands")

        raise AsmError("unknown instruction '%s'" % mn)

    def cond(self, op):
        name = None
        if op.kind == 'r8' and op.val == 1:
            name = 'c'
        elif op.kind == 'imm':
            name = op.expr.strip().lower()
        if name not in CC:
            raise AsmError("bad condition")
        return CC[name]

    def enc_alu16(self, mn, ops):
        dst, src = ops
        if mn == 'add':
            pre = self.hl_like(dst)
            if pre is None:
                raise AsmError("add: bad 16-bit destination")
            if src.kind != 'rp':
                raise AsmError("add: bad source")
            if src.val in IDX:
                if not pre or IDX[src.val] != pre[0]:
                    raise AsmError("add: bad index source")
                code = 2
            elif src.val == 'hl':
                if pre:
                    raise AsmError("add ix,hl invalid")
                code = 2
            else:
                code = RP[src.val]
            return pre + [0x09 | code << 4]
        if not (dst.kind == 'rp' and dst.val == 'hl'):
            raise AsmError("%s: destination must be hl" % mn)
        code = RP[src.val]
        return [0xED, (0x4A if mn == 'adc' else 0x42) | code << 4]

    def enc_ld(self, ops):
        if len(ops) != 2:
            raise AsmError("ld needs two operands")
        d, s = ops
        # 8-bit register moves
        if self.is_r8(d) and self.is_r8(s):
            if d.kind == 'idx' and s.kind == 'idx':
                raise AsmError("invalid ld")
            if d.kind == 'r8' and d.val == 6 and s.kind == 'r8' and s.val == 6:
                raise AsmError("invalid ld (hl),(hl)")
            pd, cd, dd = self.r8_encode(d)
            ps, cs, ds = self.r8_encode(s)
            pre = pd or ps
            if pd and ps and pd != ps:
                raise AsmError("mixed index registers")
            return pre[:1] + [0x40 | cd << 3 | cs] + (dd or ds)
        if self.is_r8(d) and s.kind == 'imm':
            pre, code, disp = self.r8_encode(d)
            return pre + [0x06 | code << 3] + disp + [self.imm8(s)]
        # a <-> (bc)/(de)/(nn)
        if d.kind == 'r8' and d.val == 7 and not d.prefix:
            if s.kind == 'ind' and s.val == 'bc':
                return [0x0A]
            if s.kind == 'ind' and s.val == 'de':
                return [0x1A]
            if s.kind == 'mem':
                return [0x3A] + self.imm16(s)
            if s.kind == 'special':
                return [0xED, 0x57 if s.val == 'i' else 0x5F]
        if s.kind == 'r8' and s.val == 7 and not s.prefix:
            if d.kind == 'ind' and d.val == 'bc':
                return [0x02]
            if d.kind == 'ind' and d.val == 'de':
                return [0x12]
            if d.kind == 'mem':
                return [0x32] + self.imm16(d)
            if d.kind == 'special':
                return [0xED, 0x47 if d.val == 'i' else 0x4F]
        # 16-bit
        if d.kind == 'rp':
            if d.val == 'sp' and s.kind == 'rp':
                pre = self.hl_like(s)
                if pre is None:
                    raise AsmError("ld sp,rr: only hl/ix/iy")
                return pre + [0xF9]
            if s.kind == 'imm':
                pre, code = self.rp_code(d)
                return pre + [0x01 | code << 4] + self.imm16(s)
            if s.kind == 'mem':
                pre = self.hl_like(d)
                if pre is not None:
                    return pre + [0x2A] + self.imm16(s)
                return [0xED, 0x4B | RP[d.val] << 4] + self.imm16(s)
        if d.kind == 'mem' and s.kind == 'rp':
            pre = self.hl_like(s)
            if pre is not None:
                return pre + [0x22] + self.imm16(d)
            return [0xED, 0x43 | RP[s.val] << 4] + self.imm16(d)
        raise AsmError("unsupported ld form")

    # -- line processing
    def process_line(self, raw, filename, lineno, depth):
        line = strip_comment(raw)
        if not line.strip():
            return
        # label
        m = re.match(r'^\s*(\.?[A-Za-z_][A-Za-z0-9_.]*):', line)
        rest = line
        label = None
        if m:
            label = m.group(1)
            rest = line[m.end():]
        else:
            m2 = re.match(r'^(\.?[A-Za-z_][A-Za-z0-9_.]*)\s+(equ|=)\s+(.*)$', line.strip(), re.I)
            if m2:
                self.define(m2.group(1), self.eval(m2.group(3)))
                return
        rest = rest.strip()
        if label:
            m3 = re.match(r'^(equ|=)\s+(.*)$', rest, re.I)
            if m3:
                self.define(label, self.eval(m3.group(2)))
                return
            self.define(label, self.pc)
        if not rest:
            return
        parts = rest.split(None, 1)
        mn = parts[0].lower()
        argstr = parts[1] if len(parts) > 1 else ''
        ops_text = split_operands(argstr) if argstr else []

        if mn == 'org':
            self.pc = self.eval(ops_text[0])
            return
        if mn in ('db', 'defb', 'byte', 'dm', 'defm'):
            data = []
            for t in ops_text:
                if t.startswith('"'):
                    s = bytes(t[1:-1], 'latin-1').decode('unicode_escape')
                    data.extend(ord(c) for c in s)
                else:
                    data.append(self.byte(self.eval(t)))
            self.emit(data)
            return
        if mn in ('dw', 'defw', 'word'):
            data = []
            for t in ops_text:
                data.extend(self.word(self.eval(t)))
            self.emit(data)
            return
        if mn in ('ds', 'defs', 'block'):
            cnt = self.eval(ops_text[0])
            fill = self.eval(ops_text[1]) if len(ops_text) > 1 else 0
            self.emit([fill & 0xFF] * cnt)
            return
        if mn == 'align':
            a = self.eval(ops_text[0])
            fill = self.eval(ops_text[1]) if len(ops_text) > 1 else 0
            pad = (-self.pc) % a
            self.emit([fill & 0xFF] * pad)
            return
        if mn == 'include':
            path = ops_text[0].strip().strip('"')
            self.assemble_file(os.path.join(os.path.dirname(filename), path), depth + 1)
            return
        if mn == 'incbin':
            path = ops_text[0].strip().strip('"')
            with open(os.path.join(os.path.dirname(filename), path), 'rb') as f:
                self.emit(f.read())
            return
        if mn in ('end', 'device', 'output'):
            return
        if mn == 'assert':
            if self.final and not self.eval(ops_text[0]):
                raise AsmError("assertion failed: %s" % ops_text[0])
            return
        ops = [self.parse_operand(t) for t in ops_text]
        self.emit(self.encode(mn, ops))

    def assemble_file(self, filename, depth=0):
        if depth > 16:
            raise AsmError("include nesting too deep")
        with open(filename, encoding='utf-8') as f:
            lines = f.read().split('\n')
        for i, raw in enumerate(lines, 1):
            try:
                self.process_line(raw, filename, i, depth)
            except AsmError as e:
                raise AsmError("%s:%d: %s\n    %s" % (filename, i, e, raw.strip())) from None

    def run(self, filename):
        for final in (False, True):
            self.final = final
            self.pc = 0
            self.last_global = ''
            self.pass_defined = {}
            self.assemble_file(filename)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--sym')
    ap.add_argument('--size', type=lambda s: int(s, 0), default=None)
    ap.add_argument('--fill', type=lambda s: int(s, 0), default=0xFF)
    args = ap.parse_args()

    asm = Assembler()
    try:
        asm.run(args.input)
    except AsmError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)

    if not asm.out:
        print("error: no output", file=sys.stderr)
        sys.exit(1)
    top = max(asm.out) + 1
    size = args.size if args.size else top
    if top > size:
        print("error: output (%d bytes) exceeds --size %d" % (top, size), file=sys.stderr)
        sys.exit(1)
    img = bytearray([args.fill]) * size
    for a, b in asm.out.items():
        img[a] = b
    with open(args.output, 'wb') as f:
        f.write(img)
    if args.sym:
        with open(args.sym, 'w') as f:
            for name, val in sorted(asm.symbols.items(), key=lambda kv: (kv[1], kv[0])):
                f.write("%04X %s\n" % (val & 0xFFFF, name))
    print("%s: %d bytes used, image %d bytes" % (args.output, len(asm.out), size))


if __name__ == '__main__':
    main()
