"""Direct WASM backend for Kabir (4.0.0 Wave 3).

Takes a compiled Kabir code dict (v2, slot-based -- see
``kabir/compile.py``) and emits a real ``.wasm`` binary, hand-built
with Python stdlib only (LEB128 + section encoding, no third-party
packages, no Emscripten -- ``emcc`` is absent in this environment).

Design (see ``.planning/DESIGN-3.0.0-4.0.0.md`` Wave 3):

* Value model: every Kabir value is a tagged 16-byte entry in linear
  memory -- ``tag:i32, len:i32, payload:f64`` at ``entry*16``. Tags:
  0 null, 1 number, 2 bool, 3 string (payload = data/heap pointer,
  len = byte length), 4 array (payload = handle pointer), 5 function
  (payload = table index), 6 iterator (payload = iterator pointer),
  7 unset (never-observed; a load of an unset slot traps).
  Numbers are f64, so integers are exact only up to 2**53.
* The value stack lives in linear memory (2048 entries x 16 bytes),
  with a mutable global ``$sp``. A fixed scratch area holds call
  arguments: the CALL case pops arguments into the scratch area, then
  ``call_indirect`` invokes the callee, whose prologue copies the
  arguments out of scratch into its own locals before any nested call
  can overwrite them.
* Every Kabir function (and every builtin) shares one WASM function
  type, ``(i32) -> ()`` -- the single parameter is ``argc``; arguments
  travel via the scratch area and the result is pushed on the shared
  value stack. All of them live in one function table, so function
  values (including recursion and first-class functions) work through
  ``call_indirect``.
* Control flow: bytecode jumps are arbitrary, WASM control flow is
  structured, so each Kabir function body is a single
  ``block $exit { loop $dispatch { br_table on $pc } }`` -- a
  pc-dispatch loop. Each bytecode instruction is one ``br_table``
  case; jumps set ``$pc`` and branch back to the dispatch loop,
  ``RETURN`` pops the return value into ``$ret`` and branches to
  ``$exit``.
* Slots map to WASM locals (one ``(tag, payload, len)`` triple per
  slot). Name-based variables inside a function (``$tmp`` temps,
  captured-by-nothing names) map to per-``DEFINE_VAR`` WASM locals,
  resolved by a scope simulation that mirrors ``ENTER_SCOPE`` /
  ``EXIT_SCOPE``. Top-level names live in a globals area in linear
  memory (one 16-byte slot per ``DEFINE_VAR`` occurrence).
* Host imports (module ``env``): ``print_num(f64)``,
  ``print_str(ptr, len)``, ``print_bool(i32)``, ``print_nl()``,
  ``format_num(f64, buf_ptr) -> len`` (host formats a number per
  Kabir's ``format_value`` rules and writes UTF-8 into WASM memory),
  ``pow(f64, f64) -> f64`` and ``parse_num(ptr, len) -> f64``.
  The Node harness in :data:`HARNESS_JS` implements them.

Supported subset: numbers, bools, null, strings (constants,
concatenation, indexing, interpolation via ``BUILD_STRING``),
arrays (literals, indexing, ``push``/``pop``, ``len``, ``for`` over
arrays/strings/``range()``), arithmetic, comparisons, ``if`` /
``while`` / ``for``, ``break`` / ``continue``, ``and`` / ``or``,
slot locals, functions including recursion and first-class function
values, ``match`` (literal/var/wildcard arms), builtins ``print``,
``range``, ``len``, ``abs``, ``sqrt``, ``pow``, ``int``, ``float``,
``num``, ``type``, ``str``, ``push``, ``pop``, and the constants
``pi`` / ``e``.

NOT supported (a clear ``KabirError`` at build time): ``import``,
dicts (``BUILD_DICT``), string methods (``GET_MEMBER``), closures
(a nested function referencing a name defined in an enclosing
function), ``input`` and the file/JSON/HTTP/time stdlib functions.
Runtime type errors (wrong operand types, bad index, division by
zero, arity mismatch, reading an undefined variable) trap the WASM
module (``unreachable``) instead of raising a Kabir error with a
message -- the failure is real, the message is not.

Public API: :func:`compile_wasm`, :func:`compile_wasm_code`,
:func:`run_wasm`, :func:`run_wasm_source`, :func:`html_harness`.
"""

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

from .checker import check_program
from .compile import compile_program, compile_source
from .errors import KabirError
from .parser import parse as _parse_source

# ----------------------------------------------------------------------
# Value tags (see module docstring).
# ----------------------------------------------------------------------
TAG_NULL = 0
TAG_NUM = 1
TAG_BOOL = 2
TAG_STR = 3
TAG_ARR = 4
TAG_FUNC = 5
TAG_ITER = 6
TAG_UNSET = 7

# ----------------------------------------------------------------------
# Linear-memory layout.
# ----------------------------------------------------------------------
ENTRY_SIZE = 16            # one tagged value: tag:i32, len:i32, pay:f64
STACK_ENTRIES = 2048
STACK_BYTES = STACK_ENTRIES * ENTRY_SIZE          # 0 .. 32768
SCRATCH_ENTRIES = 32       # call arguments, 32 max per call
SCRATCH_BASE = STACK_BYTES                        # 32768
SCRATCH_BYTES = SCRATCH_ENTRIES * ENTRY_SIZE      # 512
FORMAT_BUF = SCRATCH_BASE + SCRATCH_BYTES         # 33280
FORMAT_BUF_SIZE = 4096
GLOBALS_BASE = FORMAT_BUF + FORMAT_BUF_SIZE       # 37376
MAX_CALL_ARGS = SCRATCH_ENTRIES

# Builtins implemented as WASM functions (table order = list order).
BUILTINS = ["print", "range", "len", "abs", "sqrt", "pow", "int",
            "float", "num", "type", "str", "push", "pop"]

# WASM value types.
I32 = 0x7F
I64 = 0x7E
F32 = 0x7D
F64 = 0x7C
FUNCREF = 0x70

# WASM opcodes used.
OP_UNREACHABLE = 0x00
OP_BLOCK = 0x02
OP_LOOP = 0x03
OP_IF = 0x04
OP_ELSE = 0x05
OP_END = 0x0B
OP_BR = 0x0C
OP_BR_IF = 0x0D
OP_BR_TABLE = 0x0E
OP_RETURN = 0x0F
OP_CALL = 0x10
OP_CALL_INDIRECT = 0x11
OP_DROP = 0x1A
OP_LOCAL_GET = 0x20
OP_LOCAL_SET = 0x21
OP_GLOBAL_GET = 0x23
OP_GLOBAL_SET = 0x24
OP_I32_LOAD = 0x28
OP_F64_LOAD = 0x2B
OP_I32_LOAD8_U = 0x2D
OP_I32_STORE = 0x36
OP_F64_STORE = 0x39
OP_I32_STORE8 = 0x3A
OP_MEMORY_SIZE = 0x3F
OP_MEMORY_GROW = 0x40
OP_I32_CONST = 0x41
OP_F64_CONST = 0x44
OP_I32_EQZ = 0x45
OP_I32_EQ = 0x46
OP_I32_NE = 0x47
OP_I32_LT_S = 0x48
OP_I32_GT_S = 0x4A
OP_I32_LE_S = 0x4C
OP_I32_GE_S = 0x4E
OP_I32_LT_U = 0x49
OP_I32_GE_U = 0x4F
OP_F64_EQ = 0x61
OP_F64_NE = 0x62
OP_F64_LT = 0x63
OP_F64_GT = 0x64
OP_F64_LE = 0x65
OP_F64_GE = 0x66
OP_I32_ADD = 0x6A
OP_I32_SUB = 0x6B
OP_I32_MUL = 0x6C
OP_I32_DIV_S = 0x6D
OP_I32_AND = 0x71
OP_I32_OR = 0x72
OP_F64_FLOOR = 0x9C
OP_F64_TRUNC = 0x9D
OP_F64_SQRT = 0x9F
OP_F64_ADD = 0xA0
OP_F64_SUB = 0xA1
OP_F64_MUL = 0xA2
OP_F64_DIV = 0xA3
OP_I32_TRUNC_F64_S = 0xAA
OP_F64_CONVERT_I32_S = 0xB7
OP_F64_NEG = 0x9A
OP_F64_ABS = 0x99
OP_MEMORY_COPY = 0xFC
OP_MEMORY_COPY_SUB = 0x0A


def _uleb(n):
    """Encode a non-negative integer as unsigned LEB128."""
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _sleb(n):
    """Encode an integer as signed LEB128."""
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if (n == 0 and not byte & 0x40) or (n == -1 and byte & 0x40):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _name(s):
    data = s.encode("utf-8")
    return _uleb(len(data)) + data


def _vec(items):
    """Encode a vector of already-encoded items."""
    return _uleb(len(items)) + b"".join(items)


def _section(sec_id, payload):
    return bytes([sec_id]) + _uleb(len(payload)) + payload


# ----------------------------------------------------------------------
# Node harness: implements the ``env`` imports and runs ``main``.
# ----------------------------------------------------------------------
HARNESS_JS = r"""// Kabir WASM harness: implements the env imports, runs main(0),
// and writes everything the program printed to stdout.
const fs = require('fs');
const path = process.argv[2];
const bytes = fs.readFileSync(path);
let mem = null;
let out = '';
function formatNum(n) {
  if (Object.is(n, -0)) return '0';
  if (Number.isNaN(n)) return 'nan';
  if (!Number.isFinite(n)) return n > 0 ? 'inf' : '-inf';
  if (Number.isInteger(n)) return String(n);
  return String(n);
}
function writeStr(s, ptr) {
  const b = Buffer.from(s, 'utf8');
  new Uint8Array(mem.buffer).set(b, ptr);
  return b.length;
}
function readStr(ptr, len) {
  return Buffer.from(new Uint8Array(mem.buffer, ptr, len)).toString('utf8');
}
const imports = { env: {
  print_num(n) { out += formatNum(n); },
  print_str(ptr, len) { out += readStr(ptr, len); },
  print_bool(b) { out += b ? 'true' : 'false'; },
  print_nl() { out += '\n'; },
  format_num(n, ptr) { return writeStr(formatNum(n), ptr); },
  pow(a, b) { return Math.pow(a, b); },
  parse_num(ptr, len) { return Number(readStr(ptr, len).trim()); },
} };
WebAssembly.instantiate(bytes, imports).then(({ instance }) => {
  mem = instance.exports.memory;
  instance.exports.main(0);
  process.stdout.write(out);
}).catch((e) => {
  console.error('WASM ERROR: ' + (e && e.message ? e.message : e));
  process.exit(1);
});
"""


def html_harness(wasm_filename, title="Kabir program"):
    """Return a tiny HTML page that loads and runs a ``.wasm`` file.

    The page wires the same ``env`` imports as :data:`HARNESS_JS`
    (``print`` output goes to a ``<pre>``) and calls ``main(0)``.
    ``wasm_filename`` is the file name the page fetches, relative to
    the page itself.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>%s</title>
<style>body{font-family:monospace;margin:2em}pre{background:#111;color:#eee;padding:1em}</style>
</head>
<body>
<h1>%s</h1>
<pre id="out"></pre>
<script>
const pre = document.getElementById('out');
let mem = null;
function formatNum(n) {
  if (Object.is(n, -0)) return '0';
  if (Number.isNaN(n)) return 'nan';
  if (!Number.isFinite(n)) return n > 0 ? 'inf' : '-inf';
  if (Number.isInteger(n)) return String(n);
  return String(n);
}
function writeStr(s, ptr) {
  const b = new TextEncoder().encode(s);
  new Uint8Array(mem.buffer).set(b, ptr);
  return b.length;
}
function readStr(ptr, len) {
  return new TextDecoder().decode(new Uint8Array(mem.buffer, ptr, len));
}
const imports = { env: {
  print_num(n) { pre.textContent += formatNum(n); },
  print_str(ptr, len) { pre.textContent += readStr(ptr, len); },
  print_bool(b) { pre.textContent += b ? 'true' : 'false'; },
  print_nl() { pre.textContent += '\\n'; },
  format_num(n, ptr) { return writeStr(formatNum(n), ptr); },
  pow(a, b) { return Math.pow(a, b); },
  parse_num(ptr, len) { return Number(readStr(ptr, len).trim()); },
} };
WebAssembly.instantiateStreaming(fetch(%s), imports).then(({ instance }) => {
  mem = instance.exports.memory;
  instance.exports.main(0);
}).catch((e) => { pre.textContent += '\\nWASM ERROR: ' + e.message; });
</script>
</body>
</html>
""" % (title, title, _js_string(wasm_filename))


def _js_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _E:
    """Minimal WASM bytecode emitter over a bytearray."""

    def __init__(self):
        self.b = bytearray()

    def bytes(self):
        return bytes(self.b)

    def u8(self, v):
        self.b.append(v & 0xFF)

    def raw(self, data):
        self.b += data

    def uleb(self, n):
        self.b += _uleb(n)

    def sleb(self, n):
        self.b += _sleb(n)

    def name(self, s):
        self.b += _name(s)

    # -- instructions --------------------------------------------------
    def local_get(self, i):
        self.u8(OP_LOCAL_GET)
        self.uleb(i)

    def local_set(self, i):
        self.u8(OP_LOCAL_SET)
        self.uleb(i)

    def global_get(self, i):
        self.u8(OP_GLOBAL_GET)
        self.uleb(i)

    def global_set(self, i):
        self.u8(OP_GLOBAL_SET)
        self.uleb(i)

    def i32_const(self, v):
        self.u8(OP_I32_CONST)
        self.sleb(v)

    def f64_const(self, v):
        self.u8(OP_F64_CONST)
        self.raw(struct.pack("<d", v))

    def call(self, i):
        self.u8(OP_CALL)
        self.uleb(i)

    def unreachable(self):
        self.u8(OP_UNREACHABLE)

    def end(self):
        self.u8(OP_END)

    def block(self):
        self.u8(OP_BLOCK)
        self.u8(0x40)

    def loop(self):
        self.u8(OP_LOOP)
        self.u8(0x40)

    def if_(self):
        self.u8(OP_IF)
        self.u8(0x40)

    def else_(self):
        self.u8(OP_ELSE)

    def br(self, d):
        self.u8(OP_BR)
        self.uleb(d)

    def br_if(self, d):
        self.u8(OP_BR_IF)
        self.uleb(d)

    def drop(self):
        self.u8(OP_DROP)

    def ret(self):
        self.u8(OP_RETURN)

    def _memarg(self, align, offset):
        self.uleb(align)
        self.uleb(offset)

    def i32_load(self, offset=0):
        self.u8(OP_I32_LOAD)
        self._memarg(2, offset)

    def f64_load(self, offset=0):
        self.u8(OP_F64_LOAD)
        self._memarg(3, offset)

    def i32_load8_u(self, offset=0):
        self.u8(OP_I32_LOAD8_U)
        self._memarg(0, offset)

    def i32_store(self, offset=0):
        self.u8(OP_I32_STORE)
        self._memarg(2, offset)

    def f64_store(self, offset=0):
        self.u8(OP_F64_STORE)
        self._memarg(3, offset)

    def i32_store8(self, offset=0):
        self.u8(OP_I32_STORE8)
        self._memarg(0, offset)

    def memory_copy(self):
        self.u8(OP_MEMORY_COPY)
        self.u8(OP_MEMORY_COPY_SUB)
        self.u8(0x00)
        self.u8(0x00)

    def op(self, opcode):
        self.u8(opcode)


# ----------------------------------------------------------------------
# Translator
# ----------------------------------------------------------------------

_UNSUPPORTED_OPS = {
    "IMPORT": "import is not supported in the WASM target yet",
    "GET_MEMBER": "string methods are not supported in the WASM "
                  "target yet",
    "BUILD_DICT": "dicts are not supported in the WASM target yet",
}

_BUILTIN_CONSTS = {"pi": math.pi, "e": math.e}


def _walk_code(code, funcs, seen):
    """Collect *code* and every nested code dict, in encounter order."""
    key = id(code)
    if key in seen:
        return
    seen.add(key)
    funcs.append(code)
    for const in code["consts"]:
        if isinstance(const, dict) and "__code__" in const:
            _walk_code(const["__code__"], funcs, seen)


def _collect_strings(code, out, seen):
    """Collect every string constant in *code* (recursively)."""
    for const in code["consts"]:
        if isinstance(const, str):
            if const not in seen:
                seen.add(const)
                out.append(const)
        elif isinstance(const, dict) and "__code__" in const:
            _collect_strings(const["__code__"], out, seen)


class _FuncInfo:
    """Analysis + local allocation for one Kabir function."""

    def __init__(self, code, is_main):
        self.code = code
        self.is_main = is_main
        self.name = code["name"]
        self.params = list(code["params"])
        self.nslots = len(code.get("locals") or [])
        # Local indices, allocated lazily by _Translator._new_local.
        self.slot_triples = [None] * self.nslots
        # resolve[i] describes instruction i's name access:
        #   ("slot-triple", (t, p, l)) | ("global", slot_no) |
        #   ("builtin", name) | ("const", value) | ("trap",)
        # Only set for LOAD_VAR / STORE_VAR / DEFINE_VAR.
        self.resolve = {}
        # Names this function defines as name-locals (incl. params).
        self.defined_names = set()
        # DEFINE_VAR occurrence index -> triple, for diagnostics.
        self.define_triples = {}


class _Translator:
    def __init__(self, code):
        self.main_code = code
        # --- collect functions and strings ---------------------------
        self.funcs = []
        _walk_code(code, self.funcs, set())
        self.func_index = {id(c): i for i, c in enumerate(self.funcs)}
        strings = []
        _collect_strings(code, strings, set())
        for extra in ("", " ", "true", "false", "null", "number",
                      "boolean", "string", "array", "function",
                      "<function>", "[", "]", "[]", ", "):
            if extra not in strings:
                strings.append(extra)
        self.strings = strings
        # --- per-function analysis -----------------------------------
        self.infos = [_FuncInfo(c, i == 0)
                      for i, c in enumerate(self.funcs)]
        self.info_of = {id(c): info
                        for c, info in zip(self.funcs, self.infos)}
        # Parent chain: id(code) -> parent code dict (None for main).
        self.parent = {id(code): None for code in self.funcs}
        for code in self.funcs:
            for const in code["consts"]:
                if isinstance(const, dict) and "__code__" in const:
                    self.parent[id(const["__code__"])] = code
        self._analyze_main()
        for info in self.infos[1:]:
            self._analyze_func(info)
        self._check_closures()
        # --- memory layout -------------------------------------------
        self.nglobals = self._main_info.nglobals
        self.string_base = GLOBALS_BASE + ENTRY_SIZE * self.nglobals
        self.str_addr = {}
        addr = self.string_base
        for s in self.strings:
            self.str_addr[s] = addr
            addr += len(s.encode("utf-8"))
        self.heap_start = (addr + 7) & ~7
        self.min_pages = max(1, (self.heap_start + 65536 + 65535) // 65536)
        # Function indices: imports 0..6, helpers, builtins, kabir.
        self.n_imports = 7
        self.helper_names = ["alloc", "truthy", "str_cmp",
                             "values_equal", "format_value", "str_add",
                             "arr_concat"]
        self.helper_index = {name: self.n_imports + i
                             for i, name in enumerate(self.helper_names)}
        self.builtin_func_index = {
            name: self.n_imports + len(self.helper_names) + i
            for i, name in enumerate(BUILTINS)}
        self.kabir_func_index = {
            i: self.n_imports + len(self.helper_names) + len(BUILTINS) + i
            for i in range(len(self.funcs))}
        # Table indices: builtins first, then Kabir functions.
        self.builtin_table_index = {name: i
                                    for i, name in enumerate(BUILTINS)}
        self.kabir_table_index = {i: len(BUILTINS) + i
                                  for i in range(len(self.funcs))}
        # WASM global indices: 0=$sp, 1=$heap, 2=$fmt_len.
        self.G_SP = 0
        self.G_HEAP = 1
        self.G_FMT_LEN = 2
        # Type indices (filled in _build_types).
        self.type_index = {}

    # -- errors --------------------------------------------------------
    def _fail(self, info, index, message):
        code = info.code
        lines = code.get("lines") or []
        cols = code.get("cols") or []
        line = lines[index] if index < len(lines) else None
        col = cols[index] if index < len(cols) else None
        raise KabirError("WASM: " + message,
                         filename=code.get("filename") or "<stdin>",
                         line=line, col=col)

    # -- analysis ------------------------------------------------------
    @staticmethod
    def _loop_ranges(code):
        """[start, end] ranges of loops, from backward JUMPs.

        A loop body is the span from a backward jump's target to the
        jump itself (while/for back-edges and continue jumps).
        """
        ranges = []
        for index, (op, arg) in enumerate(code["instrs"]):
            if op == "JUMP" and isinstance(arg, int) and arg < index:
                ranges.append((arg, index))
        return ranges

    @staticmethod
    def _in_common_loop(ranges, define_index, load_index):
        """True if *define_index* and *load_index* share a loop range."""
        for start, end in ranges:
            if start <= define_index <= end \
                    and start <= load_index <= end:
                return True
        return False

    def _resolve_name(self, scopes, popped, name, index, ranges):
        """Resolve *name* to a binding, or None.

        Live scopes first (innermost out). Then scopes popped by
        EXIT_SCOPE, most recently popped first -- but only if the
        binding's DEFINE and this use share a loop range. The pool
        exists because break/continue emit extra EXIT_SCOPEs that a
        linear pass pops, while at runtime the loop re-enters those
        scopes on the next iteration. Without the loop-range check a
        name defined in an exited block would wrongly stay visible
        after the block (the VM raises Undefined variable there).
        """
        for scope in reversed(scopes):
            if name in scope:
                return scope[name]
        for scope, _pop_index in reversed(popped):
            if name in scope:
                binding = scope[name]
                define_index = binding[1] if binding[0] == "define" \
                    else binding[2] if len(binding) > 2 else -1
                if define_index >= 0 and self._in_common_loop(
                        ranges, define_index, index):
                    return binding
        return None

    def _analyze_main(self):
        info = self.infos[0]
        info.nglobals = 0
        info.root_names = {}   # name -> globals slot (root scope only)
        info.block_names = set()
        ranges = self._loop_ranges(info.code)
        scopes = [{}]          # scope dicts: name -> ("global", slot, def)
        popped = []            # (scope dict, pop index), see _resolve_name
        for index, (op, arg) in enumerate(info.code["instrs"]):
            if op in _UNSUPPORTED_OPS:
                self._fail(info, index, _UNSUPPORTED_OPS[op])
            elif op == "ENTER_SCOPE":
                scopes.append({})
            elif op == "EXIT_SCOPE":
                if len(scopes) > 1:
                    popped.append((scopes.pop(), index))
            elif op == "DEFINE_VAR":
                slot = info.nglobals
                info.nglobals += 1
                scopes[-1][arg] = ("global", slot, index)
                info.resolve[index] = ("global", slot)
                if len(scopes) == 1:
                    info.root_names[arg] = slot
                else:
                    info.block_names.add(arg)
            elif op in ("LOAD_VAR", "STORE_VAR"):
                found = self._resolve_name(scopes, popped, arg, index,
                                           ranges)
                if found is not None:
                    info.resolve[index] = ("global", found[1])
                elif arg in BUILTINS:
                    info.resolve[index] = ("builtin", arg)
                elif arg in _BUILTIN_CONSTS:
                    info.resolve[index] = ("const", _BUILTIN_CONSTS[arg])
                else:
                    info.resolve[index] = ("trap",)
        self._main_info = info

    def _analyze_func(self, info):
        code = info.code
        scopes = [{}]
        popped = []            # (scope dict, pop index), see _resolve_name
        ranges = self._loop_ranges(code)
        # Parameters: slot params stay slots; captured params become
        # root-scope name-locals (their triples are allocated by the
        # emitter; record the intent here).
        locals_list = code.get("locals") or []
        for i, param in enumerate(info.params):
            if i < len(locals_list) and locals_list[i] == param:
                info.resolve[("param", i)] = ("slot", i)
            else:
                scopes[0][param] = ("param-local", param)
                info.defined_names.add(param)
                info.resolve[("param", i)] = ("param-local", param)
        for index, (op, arg) in enumerate(code["instrs"]):
            if op in _UNSUPPORTED_OPS:
                self._fail(info, index, _UNSUPPORTED_OPS[op])
            elif op == "ENTER_SCOPE":
                scopes.append({})
            elif op == "EXIT_SCOPE":
                if len(scopes) > 1:
                    popped.append((scopes.pop(), index))
            elif op == "DEFINE_VAR":
                scopes[-1][arg] = ("define", index)
                info.defined_names.add(arg)
                info.resolve[index] = ("define", index)
            elif op in ("LOAD_VAR", "STORE_VAR"):
                found = self._resolve_name(scopes, popped, arg, index,
                                           ranges)
                if found is not None:
                    if found[0] == "define":
                        info.resolve[index] = ("local", found[1])
                    elif found[0] == "param-local":
                        info.resolve[index] = ("param-local", found[1])
                    else:
                        info.resolve[index] = found
                elif arg in self._main_info.root_names:
                    info.resolve[index] = (
                        "global", self._main_info.root_names[arg])
                elif arg in BUILTINS:
                    info.resolve[index] = ("builtin", arg)
                elif arg in _BUILTIN_CONSTS:
                    info.resolve[index] = ("const", _BUILTIN_CONSTS[arg])
                elif arg in self._main_info.block_names:
                    self._fail(
                        info, index,
                        "function %r reads %r, which is defined inside "
                        "a top-level block -- that is a closure over a "
                        "block variable, not supported in the WASM "
                        "target yet" % (info.name, arg))
                else:
                    info.resolve[index] = ("trap",)
        info._scopes = scopes

    def _check_closures(self):
        """A nested function may not reference a name defined in any
        enclosing function (other than main, whose names are globals).
        Such a reference is a closure; the WASM backend has no env
        chain, so it is a build-time error, never a silent trap.
        Free variables are computed properly, so a nested function
        that shadows an outer name with its own ``let`` is fine."""
        children = {id(c): [] for c in self.funcs}
        for code in self.funcs:
            parent = self.parent[id(code)]
            if parent is not None:
                children[id(parent)].append(code)

        def direct_refs(code):
            names = set()
            for op, arg in code["instrs"]:
                if op in ("LOAD_VAR", "STORE_VAR"):
                    names.add(arg)
            return names

        free_cache = {}

        def free_vars(code):
            key = id(code)
            if key in free_cache:
                return free_cache[key]
            info = self.info_of[key]
            result = direct_refs(code) - info.defined_names
            for child in children[key]:
                result |= free_vars(child) - info.defined_names
            free_cache[key] = result
            return result

        for info in self.infos[1:]:
            ancestors = []
            parent = self.parent[id(info.code)]
            while parent is not None:
                ancestors.append(parent)
                parent = self.parent[id(parent)]
            ancestor_defs = set()
            for anc in ancestors:
                if anc is self.main_code:
                    continue
                ancestor_defs |= self.info_of[id(anc)].defined_names
            if not ancestor_defs:
                continue
            bad = free_vars(info.code) & ancestor_defs
            if bad:
                name = sorted(bad)[0]
                raise KabirError(
                    "WASM: function %r uses %r from an enclosing "
                    "function -- closures are not supported in the "
                    "WASM target yet" % (info.name, name),
                    filename=info.code.get("filename") or "<stdin>")

    # -- local allocation ----------------------------------------------
    # Fixed locals (indices 1..19; index 0 is the $argc parameter):
    #   1 pc, 2 addr, 3..11 t0/p0/l0 t1/p1/l1 t2/p2/l2,
    #   12 a0, 13 a1, 14 f0, 15 rt, 16 rp, 17 rl, 18 i, 19 n.
    FIXED_LOCALS = (
        (I32, "pc"), (I32, "addr"),
        (I32, "t0"), (F64, "p0"), (I32, "l0"),
        (I32, "t1"), (F64, "p1"), (I32, "l1"),
        (I32, "t2"), (F64, "p2"), (I32, "l2"),
        (I32, "a0"), (I32, "a1"), (F64, "f0"),
        (I32, "rt"), (F64, "rp"), (I32, "rl"),
        (I32, "i"), (I32, "n"),
    )

    def _alloc_locals(self, info):
        """Assign real WASM local indices for a Kabir function.

        Returns (locals_decl, lookup) where locals_decl is the list of
        valtypes (after the $argc parameter) and lookup maps names /
        keys to local indices or triples."""
        lookup = {}
        types = []
        for vtype, _name in self.FIXED_LOCALS:
            lookup[_name] = 1 + len(types)
            types.append(vtype)
        nxt = 1 + len(types)
        # Slot triples, in slot order.
        for s in range(info.nslots):
            triple = (nxt, nxt + 1, nxt + 2)
            info.slot_triples[s] = triple
            nxt += 3
            types.extend([I32, F64, I32])
        # Name-local triples: one per DEFINE_VAR occurrence, plus one
        # per captured parameter.
        define_triples = {}
        for index, (op, arg) in enumerate(info.code["instrs"]):
            if op == "DEFINE_VAR":
                triple = (nxt, nxt + 1, nxt + 2)
                define_triples[index] = triple
                nxt += 3
                types.extend([I32, F64, I32])
        param_triples = {}
        for i, param in enumerate(info.params):
            res = info.resolve.get(("param", i))
            if res and res[0] == "param-local":
                triple = (nxt, nxt + 1, nxt + 2)
                param_triples[param] = triple
                nxt += 3
                types.extend([I32, F64, I32])
        lookup["define_triples"] = define_triples
        lookup["param_triples"] = param_triples
        return types, lookup


class _Fn:
    """A WASM function under construction: params + locals + body."""

    def __init__(self, param_types):
        self.nparams = len(param_types)
        self.types = list(param_types)
        self.w = _E()

    def local(self, vtype):
        idx = len(self.types)
        self.types.append(vtype)
        return idx

    def body(self):
        """Locals declaration + instructions + end, as a code entry."""
        locals_types = self.types[self.nparams:]
        runs = []
        for vtype in locals_types:
            if runs and runs[-1][1] == vtype:
                runs[-1][0] += 1
            else:
                runs.append([1, vtype])
        decl = bytearray(_uleb(len(runs)))
        for count, vtype in runs:
            decl += _uleb(count)
            decl.append(vtype)
        return bytes(decl) + self.w.bytes() + b"\x0b"


class _BodyEmitter:
    """Stack-machine helpers shared by builtins and Kabir functions.

    Every emitter has scratch triples (t0/p0/l0, t1/p1/l1, t2/p2/l2),
    an address local and a few integer/float scratch locals, plus the
    shared value stack in linear memory (global $sp).
    """

    def __init__(self, translator, param_types):
        self.tr = translator
        self.fn = _Fn(param_types)
        self.w = self.fn.w
        self.L = {}
        for name in ("addr", "t0", "p0", "l0", "t1", "p1", "l1",
                     "t2", "p2", "l2", "a0", "a1", "f0"):
            vtype = F64 if name in ("p0", "p1", "p2", "f0") else I32
            self.L[name] = self.fn.local(vtype)

    def local(self, vtype):
        return self.fn.local(vtype)

    # -- value-stack operations (entries are 16 bytes: tag, len, pay)
    def push(self, t, p, l):
        """Push the triple in locals (t, p, l) onto the value stack."""
        w = self.w
        L = self.L
        w.global_get(self.tr.G_SP)
        w.i32_const(STACK_ENTRIES)
        w.op(OP_I32_GE_U)
        w.if_()
        w.unreachable()
        w.end()
        w.global_get(self.tr.G_SP)
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.local_set(L["addr"])
        w.local_get(L["addr"])
        w.local_get(t)
        w.i32_store(0)
        w.local_get(L["addr"])
        w.local_get(l)
        w.i32_store(4)
        w.local_get(L["addr"])
        w.local_get(p)
        w.f64_store(8)
        w.global_get(self.tr.G_SP)
        w.i32_const(1)
        w.op(OP_I32_ADD)
        w.global_set(self.tr.G_SP)

    def pop(self, t, p, l):
        """Pop the top stack entry into locals (t, p, l)."""
        w = self.w
        L = self.L
        w.global_get(self.tr.G_SP)
        w.i32_const(1)
        w.op(OP_I32_SUB)
        w.global_set(self.tr.G_SP)
        w.global_get(self.tr.G_SP)
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.local_set(L["addr"])
        w.local_get(L["addr"])
        w.i32_load(0)
        w.local_set(t)
        w.local_get(L["addr"])
        w.i32_load(4)
        w.local_set(l)
        w.local_get(L["addr"])
        w.f64_load(8)
        w.local_set(p)

    def peek(self, t, p, l):
        """Read the top stack entry into locals without popping."""
        w = self.w
        L = self.L
        w.global_get(self.tr.G_SP)
        w.i32_const(1)
        w.op(OP_I32_SUB)
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.local_set(L["addr"])
        w.local_get(L["addr"])
        w.i32_load(0)
        w.local_set(t)
        w.local_get(L["addr"])
        w.i32_load(4)
        w.local_set(l)
        w.local_get(L["addr"])
        w.f64_load(8)
        w.local_set(p)

    def drop_top(self):
        w = self.w
        w.global_get(self.tr.G_SP)
        w.i32_const(1)
        w.op(OP_I32_SUB)
        w.global_set(self.tr.G_SP)

    def push_imm(self, tag, pay, length=0):
        """Push a constant triple (pay is a Python float)."""
        w = self.w
        L = self.L
        w.i32_const(tag)
        w.local_set(L["t0"])
        w.f64_const(float(pay))
        w.local_set(L["p0"])
        w.i32_const(length)
        w.local_set(L["l0"])
        self.push(L["t0"], L["p0"], L["l0"])

    def push_null(self):
        self.push_imm(TAG_NULL, 0)

    def push_num(self, value):
        """Push a number held in a local (f64) or a Python float."""
        w = self.w
        L = self.L
        w.i32_const(TAG_NUM)
        w.local_set(L["t0"])
        if isinstance(value, float):
            w.f64_const(value)
        else:
            w.local_get(value)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        self.push(L["t0"], L["p0"], L["l0"])

    def push_bool_local(self, i32_local):
        w = self.w
        L = self.L
        w.i32_const(TAG_BOOL)
        w.local_set(L["t0"])
        w.local_get(i32_local)
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        self.push(L["t0"], L["p0"], L["l0"])

    def check_tag(self, t_local, expected):
        """Trap unless the tag in local *t_local* equals *expected*."""
        w = self.w
        w.local_get(t_local)
        w.i32_const(expected)
        w.op(OP_I32_NE)
        w.if_()
        w.unreachable()
        w.end()

    def check_not_unset(self, t_local):
        """Trap if the tag in local *t_local* is TAG_UNSET."""
        w = self.w
        w.local_get(t_local)
        w.i32_const(TAG_UNSET)
        w.op(OP_I32_EQ)
        w.if_()
        w.unreachable()
        w.end()

    def load_entry(self, addr_local, t, p, l):
        """Load the 16-byte entry at address local *addr_local*."""
        w = self.w
        w.local_get(addr_local)
        w.i32_load(0)
        w.local_set(t)
        w.local_get(addr_local)
        w.i32_load(4)
        w.local_set(l)
        w.local_get(addr_local)
        w.f64_load(8)
        w.local_set(p)

    def store_entry(self, addr_local, t, p, l):
        """Store the triple in locals at address local *addr_local*."""
        w = self.w
        w.local_get(addr_local)
        w.local_get(t)
        w.i32_store(0)
        w.local_get(addr_local)
        w.local_get(l)
        w.i32_store(4)
        w.local_get(addr_local)
        w.local_get(p)
        w.f64_store(8)

    def call_truthy(self, t, p, l, result):
        """result = truthy(triple in locals t/p/l)."""
        w = self.w
        w.local_get(t)
        w.local_get(p)
        w.local_get(l)
        w.call(self.tr.helper_index["truthy"])
        w.local_set(result)

    def scratch_addr(self, i, result):
        """result = address of scratch entry *i* (i is a Python int)."""
        w = self.w
        w.i32_const(SCRATCH_BASE + i * ENTRY_SIZE)
        w.local_set(result)


# ----------------------------------------------------------------------
# Helper WASM functions (not in the table).
# ----------------------------------------------------------------------

def _emit_alloc(tr):
    """$alloc(size) -> ptr: bump allocator, growing memory as needed."""
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    size, ptr, need, pages, grow = (0, em.local(I32), em.local(I32),
                                    em.local(I32), em.local(I32))
    # size = (size + 7) & ~7
    w.local_get(size)
    w.i32_const(7)
    w.op(OP_I32_ADD)
    w.i32_const(-8)
    w.op(OP_I32_AND)
    w.local_set(size)
    w.global_get(tr.G_HEAP)
    w.local_set(ptr)
    w.global_get(tr.G_HEAP)
    w.local_get(size)
    w.op(OP_I32_ADD)
    w.local_set(need)
    w.u8(OP_MEMORY_SIZE)
    w.u8(0x00)
    w.local_set(pages)
    # if need > pages * 65536: grow
    w.local_get(need)
    w.local_get(pages)
    w.i32_const(65536)
    w.op(OP_I32_MUL)
    w.op(OP_I32_GT_S)
    w.if_()
    w.local_get(need)
    w.local_get(pages)
    w.i32_const(65536)
    w.op(OP_I32_MUL)
    w.op(OP_I32_SUB)
    w.i32_const(65535)
    w.op(OP_I32_ADD)
    w.i32_const(65536)
    w.op(OP_I32_DIV_S)
    w.local_set(grow)
    w.local_get(grow)
    w.u8(OP_MEMORY_GROW)
    w.u8(0x00)
    w.i32_const(-1)
    w.op(OP_I32_EQ)
    w.if_()
    w.unreachable()
    w.end()
    w.end()
    w.global_get(tr.G_HEAP)
    w.local_get(size)
    w.op(OP_I32_ADD)
    w.global_set(tr.G_HEAP)
    w.local_get(ptr)
    w.ret()
    return em.fn.body()


def _emit_truthy(tr):
    """$truthy(tag, pay, len) -> i32, per Kabir is_truthy."""
    em = _BodyEmitter(tr, [I32, F64, I32])
    w = em.w
    tag, pay, length = 0, 1, 2
    handle = em.local(I32)
    # null -> false
    w.local_get(tag)
    w.i32_const(TAG_NULL)
    w.op(OP_I32_EQ)
    w.if_()
    w.i32_const(0)
    w.ret()
    w.end()
    # bool / number -> pay != 0
    w.local_get(tag)
    w.i32_const(TAG_BOOL)
    w.op(OP_I32_EQ)
    w.local_get(tag)
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.op(OP_I32_OR)
    w.if_()
    w.local_get(pay)
    w.f64_const(0.0)
    w.op(OP_F64_NE)
    w.ret()
    w.end()
    # string -> len != 0
    w.local_get(tag)
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(length)
    w.i32_const(0)
    w.op(OP_I32_NE)
    w.ret()
    w.end()
    # array -> handle len != 0
    w.local_get(tag)
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(pay)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(handle)
    w.local_get(handle)
    w.i32_load(4)
    w.i32_const(0)
    w.op(OP_I32_NE)
    w.ret()
    w.end()
    # function / iterator -> true
    w.i32_const(1)
    w.ret()
    return em.fn.body()


def _emit_str_cmp(tr):
    """$str_cmp(a_ptr, a_len, b_ptr, b_len) -> -1/0/1 (byte order)."""
    em = _BodyEmitter(tr, [I32, I32, I32, I32])
    w = em.w
    a_ptr, a_len, b_ptr, b_len = 0, 1, 2, 3
    n = em.local(I32)
    i = em.local(I32)
    ca = em.local(I32)
    cb = em.local(I32)
    # n = min(a_len, b_len)
    w.local_get(a_len)
    w.local_get(b_len)
    w.op(OP_I32_LT_S)
    w.if_()
    w.local_get(a_len)
    w.local_set(n)
    w.else_()
    w.local_get(b_len)
    w.local_set(n)
    w.end()
    w.i32_const(0)
    w.local_set(i)
    w.block()  # $done
    w.loop()   # $lp
    w.local_get(i)
    w.local_get(n)
    w.op(OP_I32_GE_S)
    w.br_if(1)  # -> $done
    w.local_get(a_ptr)
    w.local_get(i)
    w.op(OP_I32_ADD)
    w.i32_load8_u(0)
    w.local_set(ca)
    w.local_get(b_ptr)
    w.local_get(i)
    w.op(OP_I32_ADD)
    w.i32_load8_u(0)
    w.local_set(cb)
    w.local_get(ca)
    w.local_get(cb)
    w.op(OP_I32_NE)
    w.if_()
    w.local_get(ca)
    w.local_get(cb)
    w.op(OP_I32_LT_U)
    w.if_()
    w.i32_const(-1)
    w.ret()
    w.else_()
    w.i32_const(1)
    w.ret()
    w.end()
    w.end()
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)  # -> $lp
    w.end()  # loop
    w.end()  # block $done
    w.local_get(a_len)
    w.local_get(b_len)
    w.op(OP_I32_LT_S)
    w.if_()
    w.i32_const(-1)
    w.ret()
    w.end()
    w.local_get(a_len)
    w.local_get(b_len)
    w.op(OP_I32_GT_S)
    w.if_()
    w.i32_const(1)
    w.ret()
    w.end()
    w.i32_const(0)
    w.ret()
    return em.fn.body()


def _emit_values_equal(tr):
    """$values_equal(a_addr, b_addr) -> i32 (deep, Kabir ==)."""
    em = _BodyEmitter(tr, [I32, I32])
    w = em.w
    a_addr, b_addr = 0, 1
    ta = em.local(I32)
    tb = em.local(I32)
    ha = em.local(I32)
    hb = em.local(I32)
    n = em.local(I32)
    i = em.local(I32)
    da = em.local(I32)
    db = em.local(I32)
    self_idx = tr.helper_index["values_equal"]
    w.local_get(a_addr)
    w.i32_load(0)
    w.local_set(ta)
    w.local_get(b_addr)
    w.i32_load(0)
    w.local_set(tb)
    w.local_get(ta)
    w.local_get(tb)
    w.op(OP_I32_NE)
    w.if_()
    w.i32_const(0)
    w.ret()
    w.end()
    # null
    w.local_get(ta)
    w.i32_const(TAG_NULL)
    w.op(OP_I32_EQ)
    w.if_()
    w.i32_const(1)
    w.ret()
    w.end()
    # number / bool: payload equality
    w.local_get(ta)
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.local_get(ta)
    w.i32_const(TAG_BOOL)
    w.op(OP_I32_EQ)
    w.op(OP_I32_OR)
    w.if_()
    w.local_get(a_addr)
    w.f64_load(8)
    w.local_get(b_addr)
    w.f64_load(8)
    w.op(OP_F64_EQ)
    w.ret()
    w.end()
    # string: byte comparison
    w.local_get(ta)
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(a_addr)
    w.f64_load(8)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_get(a_addr)
    w.i32_load(4)
    w.local_get(b_addr)
    w.f64_load(8)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_get(b_addr)
    w.i32_load(4)
    w.call(tr.helper_index["str_cmp"])
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.ret()
    w.end()
    # function / iterator: identity (table index / pointer)
    w.local_get(ta)
    w.i32_const(TAG_FUNC)
    w.op(OP_I32_EQ)
    w.local_get(ta)
    w.i32_const(TAG_ITER)
    w.op(OP_I32_EQ)
    w.op(OP_I32_OR)
    w.if_()
    w.local_get(a_addr)
    w.f64_load(8)
    w.local_get(b_addr)
    w.f64_load(8)
    w.op(OP_F64_EQ)
    w.ret()
    w.end()
    # array: same length and element-wise equality
    w.local_get(ta)
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(a_addr)
    w.f64_load(8)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(ha)
    w.local_get(b_addr)
    w.f64_load(8)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(hb)
    w.local_get(ha)
    w.i32_load(4)
    w.local_get(hb)
    w.i32_load(4)
    w.op(OP_I32_NE)
    w.if_()
    w.i32_const(0)
    w.ret()
    w.end()
    w.local_get(ha)
    w.i32_load(4)
    w.local_set(n)
    w.local_get(ha)
    w.i32_load(0)
    w.local_set(da)
    w.local_get(hb)
    w.i32_load(0)
    w.local_set(db)
    w.i32_const(0)
    w.local_set(i)
    w.block()  # $done
    w.loop()   # $lp
    w.local_get(i)
    w.local_get(n)
    w.op(OP_I32_GE_S)
    w.br_if(1)
    w.local_get(da)
    w.local_get(i)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_get(db)
    w.local_get(i)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.call(self_idx)
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.if_()
    w.i32_const(0)
    w.ret()
    w.end()
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)
    w.end()  # loop
    w.end()  # block
    w.i32_const(1)
    w.ret()
    w.end()
    w.i32_const(0)
    w.ret()
    return em.fn.body()


def _emit_format_value(tr):
    """$format_value(addr) -> ptr; length in global $fmt_len.

    Formats a value exactly like Kabir's format_value: numbers via
    the host format_num import, strings as-is, arrays as
    ``[a, b]``, functions as ``<function>``.
    """
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    addr = 0
    tag = em.local(I32)
    pay = em.local(F64)
    length = em.local(I32)
    ptr = em.local(I32)
    n = em.local(I32)
    i = em.local(I32)
    tmp = em.local(I32)
    total = em.local(I32)
    out = em.local(I32)
    off = em.local(I32)
    elen = em.local(I32)
    eptr = em.local(I32)
    handle = em.local(I32)
    data = em.local(I32)

    def const_str(s):
        w.i32_const(tr.str_addr[s])
        w.local_set(ptr)
        w.i32_const(len(s.encode("utf-8")))
        w.global_set(tr.G_FMT_LEN)
        w.local_get(ptr)
        w.ret()

    w.local_get(addr)
    w.i32_load(0)
    w.local_set(tag)
    w.local_get(addr)
    w.f64_load(8)
    w.local_set(pay)
    w.local_get(addr)
    w.i32_load(4)
    w.local_set(length)
    # null
    w.local_get(tag)
    w.i32_const(TAG_NULL)
    w.op(OP_I32_EQ)
    w.if_()
    const_str("null")
    w.end()
    # bool
    w.local_get(tag)
    w.i32_const(TAG_BOOL)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(pay)
    w.f64_const(0.0)
    w.op(OP_F64_NE)
    w.if_()
    const_str("true")
    w.else_()
    const_str("false")
    w.end()
    w.end()
    # number: host formats into FORMAT_BUF, copy to the heap
    w.local_get(tag)
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(pay)
    w.i32_const(FORMAT_BUF)
    w.call(4)  # import format_num
    w.local_set(length)
    w.local_get(length)
    w.call(tr.helper_index["alloc"])
    w.local_set(ptr)
    w.local_get(ptr)
    w.i32_const(FORMAT_BUF)
    w.local_get(length)
    w.memory_copy()
    w.local_get(length)
    w.global_set(tr.G_FMT_LEN)
    w.local_get(ptr)
    w.ret()
    w.end()
    # string: as-is
    w.local_get(tag)
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(length)
    w.global_set(tr.G_FMT_LEN)
    w.local_get(pay)
    w.op(OP_I32_TRUNC_F64_S)
    w.ret()
    w.end()
    # function / iterator
    w.local_get(tag)
    w.i32_const(TAG_FUNC)
    w.op(OP_I32_EQ)
    w.local_get(tag)
    w.i32_const(TAG_ITER)
    w.op(OP_I32_EQ)
    w.op(OP_I32_OR)
    w.if_()
    const_str("<function>")
    w.end()
    # array: [elem, elem]
    w.local_get(tag)
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(pay)
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(handle)
    w.local_get(handle)
    w.i32_load(4)
    w.local_set(n)
    w.local_get(handle)
    w.i32_load(0)
    w.local_set(data)
    w.local_get(n)
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.if_()
    const_str("[]")
    w.end()
    w.local_get(n)
    w.i32_const(8)
    w.op(OP_I32_MUL)
    w.call(tr.helper_index["alloc"])
    w.local_set(tmp)
    w.i32_const(2)
    w.local_set(total)
    w.i32_const(0)
    w.local_set(i)
    w.block()  # $done
    w.loop()   # $lp
    w.local_get(i)
    w.local_get(n)
    w.op(OP_I32_GE_S)
    w.br_if(1)
    w.local_get(data)
    w.local_get(i)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.call(tr.helper_index["format_value"])
    w.local_set(eptr)
    w.global_get(tr.G_FMT_LEN)
    w.local_set(elen)
    w.local_get(tmp)
    w.local_get(i)
    w.i32_const(8)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_get(eptr)
    w.i32_store(0)
    w.local_get(tmp)
    w.local_get(i)
    w.i32_const(8)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_get(elen)
    w.i32_store(4)
    w.local_get(i)
    w.i32_const(0)
    w.op(OP_I32_NE)
    w.if_()
    w.local_get(total)
    w.i32_const(2)
    w.op(OP_I32_ADD)
    w.local_set(total)
    w.end()
    w.local_get(total)
    w.local_get(elen)
    w.op(OP_I32_ADD)
    w.local_set(total)
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)
    w.end()  # loop
    w.end()  # block
    w.local_get(total)
    w.call(tr.helper_index["alloc"])
    w.local_set(out)
    # "[" at out
    w.local_get(out)
    w.i32_const(tr.str_addr["["])
    w.i32_const(1)
    w.memory_copy()
    w.i32_const(1)
    w.local_set(off)
    w.i32_const(0)
    w.local_set(i)
    w.block()  # $done2
    w.loop()   # $lp2
    w.local_get(i)
    w.local_get(n)
    w.op(OP_I32_GE_S)
    w.br_if(1)
    w.local_get(i)
    w.i32_const(0)
    w.op(OP_I32_NE)
    w.if_()
    w.local_get(out)
    w.local_get(off)
    w.op(OP_I32_ADD)
    w.i32_const(tr.str_addr[", "])
    w.i32_const(2)
    w.memory_copy()
    w.local_get(off)
    w.i32_const(2)
    w.op(OP_I32_ADD)
    w.local_set(off)
    w.end()
    w.local_get(tmp)
    w.local_get(i)
    w.i32_const(8)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.i32_load(0)
    w.local_set(eptr)
    w.local_get(tmp)
    w.local_get(i)
    w.i32_const(8)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.i32_load(4)
    w.local_set(elen)
    w.local_get(out)
    w.local_get(off)
    w.op(OP_I32_ADD)
    w.local_get(eptr)
    w.local_get(elen)
    w.memory_copy()
    w.local_get(off)
    w.local_get(elen)
    w.op(OP_I32_ADD)
    w.local_set(off)
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)
    w.end()  # loop2
    w.end()  # block2
    w.local_get(out)
    w.local_get(off)
    w.op(OP_I32_ADD)
    w.i32_const(tr.str_addr["]"])
    w.i32_const(1)
    w.memory_copy()
    w.local_get(total)
    w.global_set(tr.G_FMT_LEN)
    w.local_get(out)
    w.ret()
    w.end()
    # unset / unknown tag: trap
    w.unreachable()
    return em.fn.body()


def _emit_str_add(tr):
    """$str_add(a_ptr, a_len, b_ptr, b_len) -> ptr (len in $fmt_len)."""
    em = _BodyEmitter(tr, [I32, I32, I32, I32])
    w = em.w
    a_ptr, a_len, b_ptr, b_len = 0, 1, 2, 3
    total = em.local(I32)
    out = em.local(I32)
    w.local_get(a_len)
    w.local_get(b_len)
    w.op(OP_I32_ADD)
    w.local_set(total)
    w.local_get(total)
    w.call(tr.helper_index["alloc"])
    w.local_set(out)
    w.local_get(out)
    w.local_get(a_ptr)
    w.local_get(a_len)
    w.memory_copy()
    w.local_get(out)
    w.local_get(a_len)
    w.op(OP_I32_ADD)
    w.local_get(b_ptr)
    w.local_get(b_len)
    w.memory_copy()
    w.local_get(total)
    w.global_set(tr.G_FMT_LEN)
    w.local_get(out)
    w.ret()
    return em.fn.body()


def _emit_arr_concat(tr):
    """$arr_concat(ha, hb) -> new handle (elements of a then b)."""
    em = _BodyEmitter(tr, [I32, I32])
    w = em.w
    ha, hb = 0, 1
    na = em.local(I32)
    nb = em.local(I32)
    da = em.local(I32)
    db = em.local(I32)
    total = em.local(I32)
    data = em.local(I32)
    h = em.local(I32)
    w.local_get(ha)
    w.i32_load(4)
    w.local_set(na)
    w.local_get(hb)
    w.i32_load(4)
    w.local_set(nb)
    w.local_get(ha)
    w.i32_load(0)
    w.local_set(da)
    w.local_get(hb)
    w.i32_load(0)
    w.local_set(db)
    w.local_get(na)
    w.local_get(nb)
    w.op(OP_I32_ADD)
    w.local_set(total)
    w.local_get(total)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.call(tr.helper_index["alloc"])
    w.local_set(data)
    w.local_get(data)
    w.local_get(da)
    w.local_get(na)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.memory_copy()
    w.local_get(data)
    w.local_get(na)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_get(db)
    w.local_get(nb)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.memory_copy()
    w.i32_const(16)
    w.call(tr.helper_index["alloc"])
    w.local_set(h)
    w.local_get(h)
    w.local_get(data)
    w.i32_store(0)
    w.local_get(h)
    w.local_get(total)
    w.i32_store(4)
    w.local_get(h)
    w.local_get(total)
    w.i32_store(8)
    w.local_get(h)
    w.ret()
    return em.fn.body()


# ----------------------------------------------------------------------
# Builtin WASM functions (in the table, type (i32) -> ()).
# Each reads its arguments from the call scratch area and pushes its
# result on the shared value stack.
# ----------------------------------------------------------------------

def _scratch_triple(em, i, t, p, l):
    """Load scratch entry *i* into locals (t, p, l)."""
    em.scratch_addr(i, em.L["addr"])
    em.load_entry(em.L["addr"], t, p, l)


def _check_num_arg(em, t, p):
    """Trap unless the triple in (t, p) is a number; p stays the value."""
    em.check_tag(t, TAG_NUM)


def _check_integral(em, p, f0):
    """Trap unless the f64 in local *p* is a whole number."""
    w = em.w
    w.local_get(p)
    w.op(OP_F64_TRUNC)
    w.local_set(f0)
    w.local_get(f0)
    w.local_get(p)
    w.op(OP_F64_NE)
    w.if_()
    w.unreachable()
    w.end()


def _emit_b_print(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    argc = 0
    i = em.local(I32)
    w.i32_const(0)
    w.local_set(i)
    w.block()  # $done
    w.loop()   # $lp
    w.local_get(i)
    w.local_get(argc)
    w.op(OP_I32_GE_S)
    w.br_if(1)
    # space separator between arguments
    w.local_get(i)
    w.i32_const(0)
    w.op(OP_I32_NE)
    w.if_()
    w.i32_const(tr.str_addr[" "])
    w.i32_const(1)
    w.call(1)  # print_str
    w.end()
    # Scratch index is dynamic (i): load scratch entry i.
    w.local_get(i)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.i32_const(SCRATCH_BASE)
    w.op(OP_I32_ADD)
    w.local_set(L["addr"])
    em.load_entry(L["addr"], L["t0"], L["p0"], L["l0"])
    # number
    w.local_get(L["t0"])
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.call(0)  # print_num
    w.else_()
    # bool
    w.local_get(L["t0"])
    w.i32_const(TAG_BOOL)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.call(2)  # print_bool
    w.else_()
    # string
    w.local_get(L["t0"])
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_get(L["l0"])
    w.call(1)  # print_str
    w.else_()
    # null
    w.local_get(L["t0"])
    w.i32_const(TAG_NULL)
    w.op(OP_I32_EQ)
    w.if_()
    w.i32_const(tr.str_addr["null"])
    w.i32_const(4)
    w.call(1)
    w.else_()
    # array: format then print
    w.local_get(L["t0"])
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["addr"])
    w.call(tr.helper_index["format_value"])
    w.global_get(tr.G_FMT_LEN)
    w.call(1)
    w.else_()
    # function / iterator
    w.i32_const(tr.str_addr["<function>"])
    w.i32_const(10)
    w.call(1)
    w.end()
    w.end()
    w.end()
    w.end()
    w.end()
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)
    w.end()  # loop
    w.end()  # block
    w.call(3)  # print_nl
    em.push_null()
    w.ret()
    return em.fn.body()


def _emit_b_range(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    argc = 0
    start = em.local(F64)
    end = em.local(F64)
    n = em.local(I32)
    i = em.local(I32)
    handle = em.local(I32)
    data = em.local(I32)
    # argc must be 1 or 2
    w.local_get(argc)
    w.i32_const(1)
    w.op(OP_I32_EQ)
    w.local_get(argc)
    w.i32_const(2)
    w.op(OP_I32_EQ)
    w.op(OP_I32_OR)
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    _check_num_arg(em, L["t0"], L["p0"])
    _check_integral(em, L["p0"], L["f0"])
    w.local_get(argc)
    w.i32_const(1)
    w.op(OP_I32_EQ)
    w.if_()
    w.f64_const(0.0)
    w.local_set(start)
    w.local_get(L["p0"])
    w.local_set(end)
    w.else_()
    w.local_get(L["p0"])
    w.local_set(start)
    _scratch_triple(em, 1, L["t0"], L["p0"], L["l0"])
    _check_num_arg(em, L["t0"], L["p0"])
    _check_integral(em, L["p0"], L["f0"])
    w.local_get(L["p0"])
    w.local_set(end)
    w.end()
    # n = max(0, trunc(end - start))
    w.local_get(end)
    w.local_get(start)
    w.op(OP_F64_SUB)
    w.local_set(L["f0"])
    w.local_get(L["f0"])
    w.f64_const(0.0)
    w.op(OP_F64_LT)
    w.if_()
    w.i32_const(0)
    w.local_set(n)
    w.else_()
    w.local_get(L["f0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(n)
    w.end()
    w.i32_const(16)
    w.call(tr.helper_index["alloc"])
    w.local_set(handle)
    w.local_get(n)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.call(tr.helper_index["alloc"])
    w.local_set(data)
    w.local_get(handle)
    w.local_get(data)
    w.i32_store(0)
    w.local_get(handle)
    w.local_get(n)
    w.i32_store(4)
    w.local_get(handle)
    w.local_get(n)
    w.i32_store(8)
    w.i32_const(0)
    w.local_set(i)
    w.block()
    w.loop()
    w.local_get(i)
    w.local_get(n)
    w.op(OP_I32_GE_S)
    w.br_if(1)
    # element i = start + i
    w.i32_const(TAG_NUM)
    w.local_set(L["t1"])
    w.local_get(start)
    w.local_get(i)
    w.op(OP_F64_CONVERT_I32_S)
    w.op(OP_F64_ADD)
    w.local_set(L["p1"])
    w.i32_const(0)
    w.local_set(L["l1"])
    w.local_get(data)
    w.local_get(i)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_set(L["addr"])
    em.store_entry(L["addr"], L["t1"], L["p1"], L["l1"])
    w.local_get(i)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.local_set(i)
    w.br(0)
    w.end()
    w.end()
    w.i32_const(TAG_ARR)
    w.local_set(L["t0"])
    w.local_get(handle)
    w.op(OP_F64_CONVERT_I32_S)
    w.local_set(L["p0"])
    w.i32_const(0)
    w.local_set(L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_len(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    argc = 0
    handle = em.local(I32)
    w.local_get(argc)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    w.local_get(L["t0"])
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["l0"])
    w.op(OP_F64_CONVERT_I32_S)
    w.local_set(L["p0"])
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(handle)
    w.local_get(handle)
    w.i32_load(4)
    w.op(OP_F64_CONVERT_I32_S)
    w.local_set(L["p0"])
    w.else_()
    w.unreachable()
    w.end()
    w.end()
    w.i32_const(TAG_NUM)
    w.local_set(L["t0"])
    w.i32_const(0)
    w.local_set(L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_abs(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    _check_num_arg(em, L["t0"], L["p0"])
    w.local_get(L["p0"])
    w.op(OP_F64_ABS)
    w.local_set(L["p0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_sqrt(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    _check_num_arg(em, L["t0"], L["p0"])
    w.local_get(L["p0"])
    w.f64_const(0.0)
    w.op(OP_F64_LT)
    w.if_()
    w.unreachable()
    w.end()
    w.local_get(L["p0"])
    w.op(OP_F64_SQRT)
    w.local_set(L["p0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_pow(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    w.local_get(0)
    w.i32_const(2)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    _check_num_arg(em, L["t0"], L["p0"])
    w.local_get(L["p0"])
    w.local_set(L["f0"])
    _scratch_triple(em, 1, L["t1"], L["p1"], L["l1"])
    _check_num_arg(em, L["t1"], L["p1"])
    # 0 ** negative traps (Kabir: "too large to calculate")
    w.local_get(L["f0"])
    w.f64_const(0.0)
    w.op(OP_F64_EQ)
    w.local_get(L["p1"])
    w.f64_const(0.0)
    w.op(OP_F64_LT)
    w.op(OP_I32_AND)
    w.if_()
    w.unreachable()
    w.end()
    w.local_get(L["f0"])
    w.local_get(L["p1"])
    w.call(5)  # import pow
    w.local_set(L["p0"])
    w.i32_const(TAG_NUM)
    w.local_set(L["t0"])
    w.i32_const(0)
    w.local_set(L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_numlike(tr, kind):
    """int / float / num: number passthrough, string via parse_num."""
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    w.local_get(L["t0"])
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.local_set(L["f0"])
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    w.local_get(L["p0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_get(L["l0"])
    w.call(6)  # import parse_num
    w.local_set(L["f0"])
    # NaN -> trap (unparseable string)
    w.local_get(L["f0"])
    w.local_get(L["f0"])
    w.op(OP_F64_NE)
    w.if_()
    w.unreachable()
    w.end()
    w.else_()
    w.unreachable()
    w.end()
    w.end()
    if kind == "int":
        w.local_get(L["f0"])
        w.op(OP_F64_TRUNC)
        w.local_set(L["f0"])
    w.local_get(L["f0"])
    w.local_set(L["p0"])
    w.i32_const(TAG_NUM)
    w.local_set(L["t0"])
    w.i32_const(0)
    w.local_set(L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_type(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    ptr = em.local(I32)
    length = em.local(I32)
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])

    def set_str(s):
        w.i32_const(tr.str_addr[s])
        w.local_set(ptr)
        w.i32_const(len(s.encode("utf-8")))
        w.local_set(length)

    w.local_get(L["t0"])
    w.i32_const(TAG_NUM)
    w.op(OP_I32_EQ)
    w.if_()
    set_str("number")
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_BOOL)
    w.op(OP_I32_EQ)
    w.if_()
    set_str("boolean")
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_STR)
    w.op(OP_I32_EQ)
    w.if_()
    set_str("string")
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_ARR)
    w.op(OP_I32_EQ)
    w.if_()
    set_str("array")
    w.else_()
    w.local_get(L["t0"])
    w.i32_const(TAG_NULL)
    w.op(OP_I32_EQ)
    w.if_()
    set_str("null")
    w.else_()
    set_str("function")
    w.end()
    w.end()
    w.end()
    w.end()
    w.end()
    w.i32_const(TAG_STR)
    w.local_set(L["t0"])
    w.local_get(ptr)
    w.op(OP_F64_CONVERT_I32_S)
    w.local_set(L["p0"])
    w.local_get(length)
    w.local_set(L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_str(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    em.scratch_addr(0, L["addr"])
    w.local_get(L["addr"])
    w.call(tr.helper_index["format_value"])
    w.op(OP_F64_CONVERT_I32_S)
    w.local_set(L["p0"])
    w.global_get(tr.G_FMT_LEN)
    w.local_set(L["l0"])
    w.i32_const(TAG_STR)
    w.local_set(L["t0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


def _emit_b_push(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    handle = em.local(I32)
    length = em.local(I32)
    cap = em.local(I32)
    data = em.local(I32)
    newcap = em.local(I32)
    newdata = em.local(I32)
    w.local_get(0)
    w.i32_const(2)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t2"], L["p2"], L["l2"])  # array
    _scratch_triple(em, 1, L["t0"], L["p0"], L["l0"])  # element
    em.check_tag(L["t2"], TAG_ARR)
    w.local_get(L["p2"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(handle)
    w.local_get(handle)
    w.i32_load(4)
    w.local_set(length)
    w.local_get(handle)
    w.i32_load(8)
    w.local_set(cap)
    w.local_get(handle)
    w.i32_load(0)
    w.local_set(data)
    # grow if full
    w.local_get(length)
    w.local_get(cap)
    w.op(OP_I32_GE_S)
    w.if_()
    w.local_get(cap)
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.if_()
    w.i32_const(4)
    w.local_set(newcap)
    w.else_()
    w.local_get(cap)
    w.i32_const(2)
    w.op(OP_I32_MUL)
    w.local_set(newcap)
    w.end()
    w.local_get(newcap)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.call(tr.helper_index["alloc"])
    w.local_set(newdata)
    w.local_get(newdata)
    w.local_get(data)
    w.local_get(length)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.memory_copy()
    w.local_get(handle)
    w.local_get(newdata)
    w.i32_store(0)
    w.local_get(handle)
    w.local_get(newcap)
    w.i32_store(8)
    w.local_get(newdata)
    w.local_set(data)
    w.end()
    w.local_get(data)
    w.local_get(length)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_set(L["addr"])
    em.store_entry(L["addr"], L["t0"], L["p0"], L["l0"])
    w.local_get(handle)
    w.local_get(length)
    w.i32_const(1)
    w.op(OP_I32_ADD)
    w.i32_store(4)
    em.push(L["t2"], L["p2"], L["l2"])
    w.ret()
    return em.fn.body()


def _emit_b_pop(tr):
    em = _BodyEmitter(tr, [I32])
    w = em.w
    L = em.L
    handle = em.local(I32)
    length = em.local(I32)
    data = em.local(I32)
    w.local_get(0)
    w.i32_const(1)
    w.op(OP_I32_NE)
    w.if_()
    w.unreachable()
    w.end()
    _scratch_triple(em, 0, L["t0"], L["p0"], L["l0"])
    em.check_tag(L["t0"], TAG_ARR)
    w.local_get(L["p0"])
    w.op(OP_I32_TRUNC_F64_S)
    w.local_set(handle)
    w.local_get(handle)
    w.i32_load(4)
    w.local_set(length)
    w.local_get(length)
    w.i32_const(0)
    w.op(OP_I32_EQ)
    w.if_()
    w.unreachable()
    w.end()
    w.local_get(length)
    w.i32_const(1)
    w.op(OP_I32_SUB)
    w.local_set(length)
    w.local_get(handle)
    w.local_get(length)
    w.i32_store(4)
    w.local_get(handle)
    w.i32_load(0)
    w.local_set(data)
    w.local_get(data)
    w.local_get(length)
    w.i32_const(ENTRY_SIZE)
    w.op(OP_I32_MUL)
    w.op(OP_I32_ADD)
    w.local_set(L["addr"])
    em.load_entry(L["addr"], L["t0"], L["p0"], L["l0"])
    em.push(L["t0"], L["p0"], L["l0"])
    w.ret()
    return em.fn.body()


_BUILTIN_EMITTERS = {
    "print": _emit_b_print,
    "range": _emit_b_range,
    "len": _emit_b_len,
    "abs": _emit_b_abs,
    "sqrt": _emit_b_sqrt,
    "pow": _emit_b_pow,
    "int": lambda tr: _emit_b_numlike(tr, "int"),
    "float": lambda tr: _emit_b_numlike(tr, "float"),
    "num": lambda tr: _emit_b_numlike(tr, "num"),
    "type": _emit_b_type,
    "str": _emit_b_str,
    "push": _emit_b_push,
    "pop": _emit_b_pop,
}


# ----------------------------------------------------------------------
# Kabir function emitter (pc-dispatch loop).
# ----------------------------------------------------------------------

class _KabirEmitter:
    """Emits one Kabir function (code dict) as a WASM function."""

    def __init__(self, tr, info):
        self.tr = tr
        self.info = info
        self.code = info.code
        self.instrs = self.code["instrs"]
        self.n = len(self.instrs)
        self.em = _BodyEmitter(tr, [I32])  # param 0 = argc
        self.w = self.em.w
        self.L = dict(self.em.L)
        # Extra locals: pc, return triple, loop index/length, spares.
        self.L["pc"] = self.em.local(I32)
        self.L["rt"] = self.em.local(I32)
        self.L["rp"] = self.em.local(F64)
        self.L["rl"] = self.em.local(I32)
        self.L["i"] = self.em.local(I32)
        self.L["n"] = self.em.local(I32)
        self.L["b0"] = self.em.local(I32)
        self.L["b1"] = self.em.local(I32)
        # Slot triples.
        for s in range(info.nslots):
            info.slot_triples[s] = (self.em.local(I32), self.em.local(F64),
                                    self.em.local(I32))
        # Name-local triples: one per DEFINE_VAR, one per captured param.
        self.define_triples = {}
        for index, (op, _arg) in enumerate(self.instrs):
            if op == "DEFINE_VAR":
                self.define_triples[index] = (
                    self.em.local(I32), self.em.local(F64),
                    self.em.local(I32))
        self.param_triples = {}
        for i, param in enumerate(info.params):
            res = info.resolve.get(("param", i))
            if res and res[0] == "param-local":
                self.param_triples[param] = (
                    self.em.local(I32), self.em.local(F64),
                    self.em.local(I32))

    # -- small helpers -------------------------------------------------
    def set_pc(self, value):
        self.w.i32_const(value)
        self.w.local_set(self.L["pc"])

    def br_dispatch(self, k):
        """Branch back to the dispatch loop from case k."""
        self.w.br(self.n - k)

    def br_exit(self, k):
        """Branch out of the dispatch loop (RETURN) from case k."""
        self.w.br(self.n - k + 1)

    def end_case(self, k, next_pc=True):
        if next_pc:
            self.set_pc(k + 1)
        self.br_dispatch(k)

    def _init_triple_unset(self, triple):
        w = self.w
        w.i32_const(TAG_UNSET)
        w.local_set(triple[0])

    def _load_global(self, slot):
        """Load globals slot into (t0, p0, l0); trap if unset."""
        w = self.w
        L = self.L
        w.i32_const(GLOBALS_BASE + slot * ENTRY_SIZE)
        w.local_set(L["addr"])
        self.em.load_entry(L["addr"], L["t0"], L["p0"], L["l0"])
        self.em.check_not_unset(L["t0"])

    def _store_global(self, slot):
        w = self.w
        L = self.L
        w.i32_const(GLOBALS_BASE + slot * ENTRY_SIZE)
        w.local_set(L["addr"])
        self.em.store_entry(L["addr"], L["t0"], L["p0"], L["l0"])

    def _check_index(self):
        """Validate the index in (t1, p1): number, whole, into a0.

        Uses L['n'] for nothing here; the caller supplies the length
        in L['n'] and this helper only checks the number itself.
        """
        w = self.w
        L = self.L
        self.em.check_tag(L["t1"], TAG_NUM)
        w.local_get(L["p1"])
        w.op(OP_F64_TRUNC)
        w.local_set(L["f0"])
        w.local_get(L["f0"])
        w.local_get(L["p1"])
        w.op(OP_F64_NE)
        w.if_()
        w.unreachable()
        w.end()
        w.local_get(L["p1"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(L["a0"])

    def _check_bounds(self):
        """Trap unless a0 is a valid index into length L['n'].

        Negative indices wrap like Python/Kabir (``-1`` is the last).
        """
        w = self.w
        L = self.L
        w.local_get(L["a0"])
        w.i32_const(0)
        w.op(OP_I32_LT_S)
        w.if_()
        w.local_get(L["a0"])
        w.local_get(L["n"])
        w.op(OP_I32_ADD)
        w.local_set(L["a0"])
        w.end()
        w.local_get(L["a0"])
        w.i32_const(0)
        w.op(OP_I32_LT_S)
        w.local_get(L["a0"])
        w.local_get(L["n"])
        w.op(OP_I32_GE_S)
        w.op(OP_I32_OR)
        w.if_()
        w.unreachable()
        w.end()

    # -- prologue + dispatch -------------------------------------------
    def emit(self):
        w = self.w
        L = self.L
        info = self.info
        # Initialise every slot / name-local to TAG_UNSET.
        for triple in info.slot_triples:
            self._init_triple_unset(triple)
        for triple in self.define_triples.values():
            self._init_triple_unset(triple)
        for triple in self.param_triples.values():
            self._init_triple_unset(triple)
        # Arity check.
        w.local_get(0)
        w.i32_const(len(info.params))
        w.op(OP_I32_NE)
        w.if_()
        w.unreachable()
        w.end()
        # Copy arguments out of the scratch area into params.
        for i, _param in enumerate(info.params):
            res = info.resolve.get(("param", i))
            if res and res[0] == "slot":
                target = info.slot_triples[i]
            else:
                target = self.param_triples[info.params[i]]
            self.em.scratch_addr(i, L["addr"])
            self.em.load_entry(L["addr"], target[0], target[1],
                               target[2])
        # Main initialises the globals area to TAG_UNSET.
        if info.is_main:
            for slot in range(self.tr.nglobals):
                w.i32_const(GLOBALS_BASE + slot * ENTRY_SIZE)
                w.i32_const(TAG_UNSET)
                w.i32_store(0)
        # Dispatch loop: block $exit { loop $dispatch { br_table } }.
        w.block()  # $exit
        w.loop()   # $dispatch
        w.block()  # $default
        for k in range(self.n - 1, -1, -1):
            w.block()  # $case k
        w.local_get(L["pc"])
        # br_table: count, case labels, then the DEFAULT label as a
        # separate immediate after the vector (not part of it).
        w.u8(OP_BR_TABLE)
        w.uleb(self.n)
        for k in range(self.n):
            w.uleb(k)
        w.uleb(self.n)  # default -> $default block
        for k in range(self.n):
            w.end()  # end block $case k
            self._emit_case(k)
        w.end()  # end $default
        w.unreachable()  # pc out of range (cannot happen)
        w.end()  # end loop $dispatch
        w.end()  # end block $exit
        # Push the return value and finish.
        self.em.push(L["rt"], L["rp"], L["rl"])
        w.ret()
        return self.em.fn.body()

    # -- one bytecode instruction --------------------------------------
    def _emit_case(self, k):
        w = self.w
        L = self.L
        em = self.em
        tr = self.tr
        info = self.info
        op, arg = self.instrs[k]
        method = getattr(self, "_case_" + op, None)
        if method is None:
            # Unknown opcode: trap rather than silently misbehave.
            w.unreachable()
            return
        method(k, arg)

    # -- cases ---------------------------------------------------------
    def _case_LOAD_CONST(self, k, arg):
        const = self.code["consts"][arg]
        if const is None:
            self.em.push_null()
        elif isinstance(const, bool):
            self.em.push_imm(TAG_BOOL, 1.0 if const else 0.0)
        elif isinstance(const, (int, float)):
            self.em.push_imm(TAG_NUM, float(const))
        elif isinstance(const, str):
            data = const.encode("utf-8")
            self.em.push_imm(TAG_STR, float(self.tr.str_addr[const]),
                             len(data))
        elif isinstance(const, dict) and "__code__" in const:
            idx = self.tr.func_index[id(const["__code__"])]
            self.em.push_imm(TAG_FUNC,
                             float(self.tr.kabir_table_index[idx]))
        else:  # pragma: no cover - compiler never emits this
            self.w.unreachable()
            return
        self.end_case(k)

    def _case_LOAD_NULL(self, k, arg):
        self.em.push_null()
        self.end_case(k)

    def _case_LOAD_TRUE(self, k, arg):
        self.em.push_imm(TAG_BOOL, 1.0)
        self.end_case(k)

    def _case_LOAD_FALSE(self, k, arg):
        self.em.push_imm(TAG_BOOL, 0.0)
        self.end_case(k)

    def _var_triple(self, res):
        """WASM local triple for a resolved name, or None."""
        if res[0] == "local":
            return self.define_triples[res[1]]
        if res[0] == "param-local":
            return self.param_triples[res[1]]
        return None

    def _case_LOAD_VAR(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        res = self.info.resolve[k]
        if res[0] == "global":
            self._load_global(res[1])
            em.push(L["t0"], L["p0"], L["l0"])
        elif res[0] == "builtin":
            em.push_imm(TAG_FUNC,
                        float(self.tr.builtin_table_index[res[1]]))
        elif res[0] == "const":
            em.push_imm(TAG_NUM, float(res[1]))
        elif res[0] in ("local", "param-local"):
            triple = self._var_triple(res)
            em.check_not_unset(triple[0])
            em.push(triple[0], triple[1], triple[2])
        else:  # ("trap",): undefined variable at runtime
            w.unreachable()
            return
        self.end_case(k)

    def _case_DEFINE_VAR(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        res = self.info.resolve[k]
        if res[0] == "global":
            em.pop(L["t0"], L["p0"], L["l0"])
            self._store_global(res[1])
        elif res[0] == "define":
            triple = self.define_triples[k]
            em.pop(triple[0], triple[1], triple[2])
        else:  # pragma: no cover - analysis never produces this
            w.unreachable()
            return
        self.end_case(k)

    def _case_STORE_VAR(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        res = self.info.resolve[k]
        if res[0] == "global":
            self._load_global(res[1])  # traps if never defined
            em.pop(L["t0"], L["p0"], L["l0"])
            self._store_global(res[1])
        elif res[0] in ("local", "param-local"):
            triple = self._var_triple(res)
            em.check_not_unset(triple[0])
            em.pop(triple[0], triple[1], triple[2])
        else:  # builtin / const / undefined: trap
            w.unreachable()
            return
        self.end_case(k)

    def _case_LOAD_SLOT(self, k, arg):
        triple = self.info.slot_triples[arg]
        self.em.check_not_unset(triple[0])
        self.em.push(triple[0], triple[1], triple[2])
        self.end_case(k)

    def _case_DEFINE_SLOT(self, k, arg):
        triple = self.info.slot_triples[arg]
        self.em.pop(triple[0], triple[1], triple[2])
        self.end_case(k)

    def _case_STORE_SLOT(self, k, arg):
        triple = self.info.slot_triples[arg]
        self.em.check_not_unset(triple[0])
        self.em.pop(triple[0], triple[1], triple[2])
        self.end_case(k)

    def _case_POP(self, k, arg):
        self.em.drop_top()
        self.end_case(k)

    def _case_DUP(self, k, arg):
        L = self.L
        self.em.peek(L["t0"], L["p0"], L["l0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_BUILD_LIST(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        n = arg
        cap = n if n >= 4 else 4
        handle = L["b0"]
        data = L["b1"]
        w.i32_const(16)
        w.call(self.tr.helper_index["alloc"])
        w.local_set(handle)
        w.i32_const(cap * ENTRY_SIZE)
        w.call(self.tr.helper_index["alloc"])
        w.local_set(data)
        w.local_get(handle)
        w.local_get(data)
        w.i32_store(0)
        w.local_get(handle)
        w.i32_const(n)
        w.i32_store(4)
        w.local_get(handle)
        w.i32_const(cap)
        w.i32_store(8)
        for j in range(n):
            em.pop(L["t1"], L["p1"], L["l1"])
            w.local_get(data)
            w.i32_const((n - 1 - j) * ENTRY_SIZE)
            w.op(OP_I32_ADD)
            w.local_set(L["addr"])
            em.store_entry(L["addr"], L["t1"], L["p1"], L["l1"])
        w.i32_const(TAG_ARR)
        w.local_set(L["t0"])
        w.local_get(handle)
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_BUILD_DICT(self, k, arg):
        self.w.unreachable()  # rejected at analysis time

    def _case_BUILD_STRING(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        tr = self.tr
        n = arg
        if n == 0:
            em.push_imm(TAG_STR, float(tr.str_addr[""]), 0)
            self.end_case(k)
            return
        tmp = L["b0"]    # n*16 bytes: popped parts, in order
        tmp2 = L["b1"]   # n*8 bytes: (ptr, len) pairs
        total = em.local(I32)
        out = em.local(I32)
        off = em.local(I32)
        eptr = em.local(I32)
        elen = em.local(I32)
        w.i32_const(n * ENTRY_SIZE)
        w.call(tr.helper_index["alloc"])
        w.local_set(tmp)
        for j in range(n):
            em.pop(L["t1"], L["p1"], L["l1"])
            w.local_get(tmp)
            w.i32_const((n - 1 - j) * ENTRY_SIZE)
            w.op(OP_I32_ADD)
            w.local_set(L["addr"])
            em.store_entry(L["addr"], L["t1"], L["p1"], L["l1"])
        w.i32_const(n * 8)
        w.call(tr.helper_index["alloc"])
        w.local_set(tmp2)
        w.i32_const(0)
        w.local_set(total)
        for i in range(n):
            w.local_get(tmp)
            w.i32_const(i * ENTRY_SIZE)
            w.op(OP_I32_ADD)
            w.call(tr.helper_index["format_value"])
            w.local_set(eptr)
            w.global_get(tr.G_FMT_LEN)
            w.local_set(elen)
            w.local_get(tmp2)
            w.i32_const(i * 8)
            w.op(OP_I32_ADD)
            w.local_get(eptr)
            w.i32_store(0)
            w.local_get(tmp2)
            w.i32_const(i * 8)
            w.op(OP_I32_ADD)
            w.local_get(elen)
            w.i32_store(4)
            w.local_get(total)
            w.local_get(elen)
            w.op(OP_I32_ADD)
            w.local_set(total)
        w.local_get(total)
        w.call(tr.helper_index["alloc"])
        w.local_set(out)
        w.i32_const(0)
        w.local_set(off)
        for i in range(n):
            w.local_get(tmp2)
            w.i32_const(i * 8)
            w.op(OP_I32_ADD)
            w.i32_load(0)
            w.local_set(eptr)
            w.local_get(tmp2)
            w.i32_const(i * 8)
            w.op(OP_I32_ADD)
            w.i32_load(4)
            w.local_set(elen)
            w.local_get(out)
            w.local_get(off)
            w.op(OP_I32_ADD)
            w.local_get(eptr)
            w.local_get(elen)
            w.memory_copy()
            w.local_get(off)
            w.local_get(elen)
            w.op(OP_I32_ADD)
            w.local_set(off)
        w.i32_const(TAG_STR)
        w.local_set(L["t0"])
        w.local_get(out)
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.local_get(total)
        w.local_set(L["l0"])
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _binary_numbers(self, k, op):
        """Shared prologue for numeric binary ops: pops b, a into
        t1/t0; traps unless both are numbers."""
        L = self.L
        em = self.em
        em.pop(L["t1"], L["p1"], L["l1"])
        em.pop(L["t0"], L["p0"], L["l0"])
        em.check_tag(L["t0"], TAG_NUM)
        em.check_tag(L["t1"], TAG_NUM)

    def _case_ADD(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t1"], L["p1"], L["l1"])
        em.pop(L["t0"], L["p0"], L["l0"])
        # numbers
        w.local_get(L["t0"])
        w.i32_const(TAG_NUM)
        w.op(OP_I32_EQ)
        w.local_get(L["t1"])
        w.i32_const(TAG_NUM)
        w.op(OP_I32_EQ)
        w.op(OP_I32_AND)
        w.if_()
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(OP_F64_ADD)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        w.else_()
        # strings
        w.local_get(L["t0"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.local_get(L["t1"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.op(OP_I32_AND)
        w.if_()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["l0"])
        w.local_get(L["p1"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["l1"])
        w.call(self.tr.helper_index["str_add"])
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.global_get(self.tr.G_FMT_LEN)
        w.local_set(L["l0"])
        w.else_()
        # arrays
        w.local_get(L["t0"])
        w.i32_const(TAG_ARR)
        w.op(OP_I32_EQ)
        w.local_get(L["t1"])
        w.i32_const(TAG_ARR)
        w.op(OP_I32_EQ)
        w.op(OP_I32_AND)
        w.if_()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["p1"])
        w.op(OP_I32_TRUNC_F64_S)
        w.call(self.tr.helper_index["arr_concat"])
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        w.else_()
        w.unreachable()
        w.end()
        w.end()
        w.end()
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_SUB(self, k, arg):
        self._binary_numbers(k, "SUB")
        w = self.w
        L = self.L
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(OP_F64_SUB)
        w.local_set(L["p0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_MUL(self, k, arg):
        self._binary_numbers(k, "MUL")
        w = self.w
        L = self.L
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(OP_F64_MUL)
        w.local_set(L["p0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_DIV(self, k, arg):
        self._binary_numbers(k, "DIV")
        w = self.w
        L = self.L
        w.local_get(L["p1"])
        w.f64_const(0.0)
        w.op(OP_F64_EQ)
        w.if_()
        w.unreachable()
        w.end()
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(OP_F64_DIV)
        w.local_set(L["p0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_MOD(self, k, arg):
        self._binary_numbers(k, "MOD")
        w = self.w
        L = self.L
        w.local_get(L["p1"])
        w.f64_const(0.0)
        w.op(OP_F64_EQ)
        w.if_()
        w.unreachable()
        w.end()
        # Python-style modulo: a - floor(a / b) * b
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(OP_F64_DIV)
        w.op(OP_F64_FLOOR)
        w.local_get(L["p1"])
        w.op(OP_F64_MUL)
        w.local_set(L["f0"])
        w.local_get(L["p0"])
        w.local_get(L["f0"])
        w.op(OP_F64_SUB)
        w.local_set(L["p0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_POW(self, k, arg):
        self._binary_numbers(k, "POW")
        w = self.w
        L = self.L
        w.local_get(L["p0"])
        w.f64_const(0.0)
        w.op(OP_F64_EQ)
        w.local_get(L["p1"])
        w.f64_const(0.0)
        w.op(OP_F64_LT)
        w.op(OP_I32_AND)
        w.if_()
        w.unreachable()
        w.end()
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.call(5)  # import pow
        w.local_set(L["p0"])
        self.em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_NEG(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t0"], L["p0"], L["l0"])
        em.check_tag(L["t0"], TAG_NUM)
        w.local_get(L["p0"])
        w.op(OP_F64_NEG)
        w.local_set(L["p0"])
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_NOT(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t0"], L["p0"], L["l0"])
        em.call_truthy(L["t0"], L["p0"], L["l0"], L["a0"])
        w.local_get(L["a0"])
        w.op(OP_I32_EQZ)
        w.local_set(L["a1"])
        em.push_bool_local(L["a1"])
        self.end_case(k)

    def _case_EQ(self, k, arg):
        self._equality(k, negate=False)

    def _case_NE(self, k, arg):
        self._equality(k, negate=True)

    def _equality(self, k, negate):
        w = self.w
        L = self.L
        em = self.em
        # Addresses of the top two entries (b on top).
        w.global_get(self.tr.G_SP)
        w.i32_const(2)
        w.op(OP_I32_SUB)
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.local_set(L["a1"])
        w.global_get(self.tr.G_SP)
        w.i32_const(1)
        w.op(OP_I32_SUB)
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.local_set(L["a0"])
        w.local_get(L["a1"])
        w.local_get(L["a0"])
        w.call(self.tr.helper_index["values_equal"])
        w.local_set(L["a0"])
        em.drop_top()
        em.drop_top()
        if negate:
            w.local_get(L["a0"])
            w.op(OP_I32_EQZ)
            w.local_set(L["a0"])
        em.push_bool_local(L["a0"])
        self.end_case(k)

    def _case_LT(self, k, arg):
        self._comparison(k, "LT")

    def _case_LE(self, k, arg):
        self._comparison(k, "LE")

    def _case_GT(self, k, arg):
        self._comparison(k, "GT")

    def _case_GE(self, k, arg):
        self._comparison(k, "GE")

    def _comparison(self, k, kind):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t1"], L["p1"], L["l1"])
        em.pop(L["t0"], L["p0"], L["l0"])
        f64_op = {"LT": OP_F64_LT, "LE": OP_F64_LE,
                  "GT": OP_F64_GT, "GE": OP_F64_GE}[kind]
        i32_op = {"LT": OP_I32_LT_S, "LE": OP_I32_LE_S,
                  "GT": OP_I32_GT_S, "GE": OP_I32_GE_S}[kind]
        # numbers
        w.local_get(L["t0"])
        w.i32_const(TAG_NUM)
        w.op(OP_I32_EQ)
        w.local_get(L["t1"])
        w.i32_const(TAG_NUM)
        w.op(OP_I32_EQ)
        w.op(OP_I32_AND)
        w.if_()
        w.local_get(L["p0"])
        w.local_get(L["p1"])
        w.op(f64_op)
        w.local_set(L["a0"])
        w.else_()
        # strings
        w.local_get(L["t0"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.local_get(L["t1"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.op(OP_I32_AND)
        w.if_()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["l0"])
        w.local_get(L["p1"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["l1"])
        w.call(self.tr.helper_index["str_cmp"])
        w.local_set(L["a0"])
        w.local_get(L["a0"])
        w.i32_const(0)
        w.op(i32_op)
        w.local_set(L["a0"])
        w.else_()
        w.unreachable()
        w.end()
        w.end()
        em.push_bool_local(L["a0"])
        self.end_case(k)

    def _case_JUMP(self, k, arg):
        self.set_pc(arg)
        self.br_dispatch(k)

    def _case_JUMP_IF_FALSE(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t0"], L["p0"], L["l0"])
        em.call_truthy(L["t0"], L["p0"], L["l0"], L["a0"])
        w.local_get(L["a0"])
        w.op(OP_I32_EQZ)
        w.if_()
        self.set_pc(arg)
        w.else_()
        self.set_pc(k + 1)
        w.end()
        self.br_dispatch(k)

    def _case_JUMP_IF_TRUE(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t0"], L["p0"], L["l0"])
        em.call_truthy(L["t0"], L["p0"], L["l0"], L["a0"])
        w.local_get(L["a0"])
        w.if_()
        self.set_pc(arg)
        w.else_()
        self.set_pc(k + 1)
        w.end()
        self.br_dispatch(k)

    def _case_CALL(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        argc = arg
        if argc > MAX_CALL_ARGS:
            self.tr._fail(
                self.info, k,
                "call with %d arguments exceeds the WASM target's "
                "limit of %d" % (argc, MAX_CALL_ARGS))
        # Pop arguments into the scratch area, in order.
        for j in range(argc):
            em.pop(L["t2"], L["p2"], L["l2"])
            w.i32_const(SCRATCH_BASE + (argc - 1 - j) * ENTRY_SIZE)
            w.local_set(L["addr"])
            em.store_entry(L["addr"], L["t2"], L["p2"], L["l2"])
        # Pop the callee; it must be a function value.
        em.pop(L["t2"], L["p2"], L["l2"])
        em.check_tag(L["t2"], TAG_FUNC)
        w.i32_const(argc)
        w.local_get(L["p2"])
        w.op(OP_I32_TRUNC_F64_S)
        w.u8(OP_CALL_INDIRECT)
        w.uleb(self.tr.type_index[((I32,), ())])
        w.u8(0x00)
        self.end_case(k)

    def _case_RETURN(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["rt"], L["rp"], L["rl"])
        self.br_exit(k)

    def _case_MAKE_FUNCTION(self, k, arg):
        const = self.code["consts"][arg]
        idx = self.tr.func_index[id(const["__code__"])]
        self.em.push_imm(TAG_FUNC,
                         float(self.tr.kabir_table_index[idx]))
        self.end_case(k)

    def _case_IMPORT(self, k, arg):
        self.w.unreachable()  # rejected at analysis time

    def _case_GET_INDEX(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t1"], L["p1"], L["l1"])  # index
        em.pop(L["t0"], L["p0"], L["l0"])  # target
        # array
        w.local_get(L["t0"])
        w.i32_const(TAG_ARR)
        w.op(OP_I32_EQ)
        w.if_()
        self._check_index()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(L["a1"])
        w.local_get(L["a1"])
        w.i32_load(4)
        w.local_set(L["n"])
        self._check_bounds()
        w.local_get(L["a1"])
        w.i32_load(0)
        w.local_set(L["t1"])
        w.local_get(L["t1"])
        w.local_get(L["a0"])
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.op(OP_I32_ADD)
        w.local_set(L["addr"])
        em.load_entry(L["addr"], L["t0"], L["p0"], L["l0"])
        w.else_()
        # string
        w.local_get(L["t0"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.if_()
        self._check_index()
        w.local_get(L["l0"])
        w.local_set(L["n"])
        self._check_bounds()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_get(L["a0"])
        w.op(OP_I32_ADD)
        w.i32_load8_u(0)
        w.local_set(L["t1"])
        w.i32_const(1)
        w.call(self.tr.helper_index["alloc"])
        w.local_set(L["a1"])
        w.local_get(L["a1"])
        w.local_get(L["t1"])
        w.i32_store8(0)
        w.i32_const(TAG_STR)
        w.local_set(L["t0"])
        w.local_get(L["a1"])
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(1)
        w.local_set(L["l0"])
        w.else_()
        w.unreachable()
        w.end()
        w.end()
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_SET_INDEX(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t2"], L["p2"], L["l2"])  # value
        em.pop(L["t1"], L["p1"], L["l1"])  # index
        em.pop(L["t0"], L["p0"], L["l0"])  # container
        em.check_tag(L["t0"], TAG_ARR)
        self._check_index()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(L["a1"])
        w.local_get(L["a1"])
        w.i32_load(4)
        w.local_set(L["n"])
        self._check_bounds()
        w.local_get(L["a1"])
        w.i32_load(0)
        w.local_set(L["t1"])
        w.local_get(L["t1"])
        w.local_get(L["a0"])
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.op(OP_I32_ADD)
        w.local_set(L["addr"])
        em.store_entry(L["addr"], L["t2"], L["p2"], L["l2"])
        em.push_null()
        self.end_case(k)

    def _case_GET_MEMBER(self, k, arg):
        self.w.unreachable()  # rejected at analysis time

    def _case_ENTER_SCOPE(self, k, arg):
        self.end_case(k)

    def _case_EXIT_SCOPE(self, k, arg):
        self.end_case(k)

    def _case_GET_ITER(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        em.pop(L["t0"], L["p0"], L["l0"])
        handle = L["a1"]
        # array
        w.local_get(L["t0"])
        w.i32_const(TAG_ARR)
        w.op(OP_I32_EQ)
        w.if_()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(handle)
        w.i32_const(0)
        w.local_set(L["l0"])
        w.else_()
        # string
        w.local_get(L["t0"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.if_()
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(handle)
        w.else_()
        w.unreachable()
        w.end()
        w.end()
        # iterator = [kind, ptr, len, index] (16 bytes)
        w.i32_const(16)
        w.call(self.tr.helper_index["alloc"])
        w.local_set(L["a0"])
        # kind: 0 for array, 1 for string
        w.local_get(L["a0"])
        w.local_get(L["t0"])
        w.i32_const(TAG_STR)
        w.op(OP_I32_EQ)
        w.i32_store(0)
        w.local_get(L["a0"])
        w.local_get(handle)
        w.i32_store(4)
        w.local_get(L["a0"])
        w.local_get(L["l0"])
        w.i32_store(8)
        w.local_get(L["a0"])
        w.i32_const(0)
        w.i32_store(12)
        w.i32_const(TAG_ITER)
        w.local_set(L["t0"])
        w.local_get(L["a0"])
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(0)
        w.local_set(L["l0"])
        em.push(L["t0"], L["p0"], L["l0"])
        self.end_case(k)

    def _case_FOR_ITER(self, k, arg):
        w = self.w
        L = self.L
        em = self.em
        end_target = arg
        # Peek the iterator (stays on the stack while iterating).
        em.peek(L["t0"], L["p0"], L["l0"])
        em.check_tag(L["t0"], TAG_ITER)
        w.local_get(L["p0"])
        w.op(OP_I32_TRUNC_F64_S)
        w.local_set(L["a0"])  # iter ptr
        # kind == 0 (array) ?
        w.local_get(L["a0"])
        w.i32_load(0)
        w.i32_const(0)
        w.op(OP_I32_EQ)
        w.if_()
        # array: handle, len, index
        w.local_get(L["a0"])
        w.i32_load(4)
        w.local_set(L["a1"])  # handle
        w.local_get(L["a1"])
        w.i32_load(4)
        w.local_set(L["n"])
        w.local_get(L["a0"])
        w.i32_load(12)
        w.local_set(L["i"])
        w.local_get(L["i"])
        w.local_get(L["n"])
        w.op(OP_I32_LT_S)
        w.if_()
        w.local_get(L["a1"])
        w.i32_load(0)
        w.local_set(L["t1"])  # data
        w.local_get(L["t1"])
        w.local_get(L["i"])
        w.i32_const(ENTRY_SIZE)
        w.op(OP_I32_MUL)
        w.op(OP_I32_ADD)
        w.local_set(L["addr"])
        em.load_entry(L["addr"], L["t1"], L["p1"], L["l1"])
        em.push(L["t1"], L["p1"], L["l1"])
        w.local_get(L["a0"])
        w.local_get(L["i"])
        w.i32_const(1)
        w.op(OP_I32_ADD)
        w.i32_store(12)
        self.set_pc(k + 1)
        w.else_()
        em.drop_top()
        self.set_pc(end_target)
        w.end()
        w.else_()
        # string: ptr, len, index
        w.local_get(L["a0"])
        w.i32_load(4)
        w.local_set(L["a1"])  # str ptr
        w.local_get(L["a0"])
        w.i32_load(8)
        w.local_set(L["n"])
        w.local_get(L["a0"])
        w.i32_load(12)
        w.local_set(L["i"])
        w.local_get(L["i"])
        w.local_get(L["n"])
        w.op(OP_I32_LT_S)
        w.if_()
        w.local_get(L["a1"])
        w.local_get(L["i"])
        w.op(OP_I32_ADD)
        w.i32_load8_u(0)
        w.local_set(L["t1"])  # byte
        w.i32_const(1)
        w.call(self.tr.helper_index["alloc"])
        w.local_set(L["b0"])  # char ptr
        w.local_get(L["b0"])
        w.local_get(L["t1"])
        w.i32_store8(0)
        w.i32_const(TAG_STR)
        w.local_set(L["t0"])
        w.local_get(L["b0"])
        w.op(OP_F64_CONVERT_I32_S)
        w.local_set(L["p0"])
        w.i32_const(1)
        w.local_set(L["l0"])
        em.push(L["t0"], L["p0"], L["l0"])
        w.local_get(L["a0"])
        w.local_get(L["i"])
        w.i32_const(1)
        w.op(OP_I32_ADD)
        w.i32_store(12)
        self.set_pc(k + 1)
        w.else_()
        em.drop_top()
        self.set_pc(end_target)
        w.end()
        w.end()
        self.br_dispatch(k)

    def _case_MATCH_FAIL(self, k, arg):
        self.w.unreachable()


# ----------------------------------------------------------------------
# Module assembly + public API
# ----------------------------------------------------------------------

# Import table: (module, name, (params, results)).
_IMPORTS = [
    ("env", "print_num", ((F64,), ())),
    ("env", "print_str", ((I32, I32), ())),
    ("env", "print_bool", ((I32,), ())),
    ("env", "print_nl", ((), ())),
    ("env", "format_num", ((F64, I32), (I32,))),
    ("env", "pow", ((F64, F64), (F64,))),
    ("env", "parse_num", ((I32, I32), (F64,))),
]

_HELPER_EMITTERS = {
    "alloc": _emit_alloc,
    "truthy": _emit_truthy,
    "str_cmp": _emit_str_cmp,
    "values_equal": _emit_values_equal,
    "format_value": _emit_format_value,
    "str_add": _emit_str_add,
    "arr_concat": _emit_arr_concat,
}


def _build_module(tr):
    """Assemble the complete .wasm binary for translator *tr*."""
    # --- types (dedup, in first-use order) ---------------------------
    signatures = []
    for _mod, _nm, sig in _IMPORTS:
        if sig not in signatures:
            signatures.append(sig)
    helper_sigs = {
        "alloc": ((I32,), (I32,)),
        "truthy": ((I32, F64, I32), (I32,)),
        "str_cmp": ((I32, I32, I32, I32), (I32,)),
        "values_equal": ((I32, I32), (I32,)),
        "format_value": ((I32,), (I32,)),
        "str_add": ((I32, I32, I32, I32), (I32,)),
        "arr_concat": ((I32, I32), (I32,)),
    }
    for sig in helper_sigs.values():
        if sig not in signatures:
            signatures.append(sig)
    kabir_sig = ((I32,), ())
    if kabir_sig not in signatures:
        signatures.append(kabir_sig)
    tr.type_index = {sig: i for i, sig in enumerate(signatures)}

    def type_section():
        entries = []
        for params, results in signatures:
            entries.append(b"\x60" + _vec([bytes([p]) for p in params])
                           + _vec([bytes([r]) for r in results]))
        return _section(1, _vec(entries))

    def import_section():
        entries = []
        for mod, nm, sig in _IMPORTS:
            entries.append(_name(mod) + _name(nm) + b"\x00"
                           + _uleb(tr.type_index[sig]))
        return _section(2, _vec(entries))

    # --- function bodies (order = function-index order) --------------
    helper_bodies = [_HELPER_EMITTERS[name](tr)
                     for name in tr.helper_names]
    builtin_bodies = [_BUILTIN_EMITTERS[name](tr) for name in BUILTINS]
    kabir_bodies = [_KabirEmitter(tr, info).emit()
                    for info in tr.infos]
    bodies = helper_bodies + builtin_bodies + kabir_bodies

    def function_section():
        type_ids = []
        for name in tr.helper_names:
            type_ids.append(tr.type_index[helper_sigs[name]])
        for _name in BUILTINS:
            type_ids.append(tr.type_index[kabir_sig])
        for _info in tr.infos:
            type_ids.append(tr.type_index[kabir_sig])
        return _section(3, _vec([_uleb(t) for t in type_ids]))

    def table_section():
        table_size = len(BUILTINS) + len(tr.funcs)
        entry = bytes([FUNCREF, 0x00]) + _uleb(table_size)
        return _section(4, _vec([entry]))

    def memory_section():
        entry = b"\x00" + _uleb(tr.min_pages)
        return _section(5, _vec([entry]))

    def global_section():
        def glob(value):
            return bytes([I32, 0x01]) + bytes([OP_I32_CONST]) \
                + _sleb(value) + bytes([OP_END])
        entries = [glob(0), glob(tr.heap_start), glob(0)]
        return _section(6, _vec(entries))

    def export_section():
        main_idx = tr.kabir_func_index[0]
        entries = [
            _name("main") + b"\x00" + _uleb(main_idx),
            _name("memory") + b"\x02" + _uleb(0),
        ]
        return _section(7, _vec(entries))

    def element_section():
        func_ids = ([tr.builtin_func_index[name] for name in BUILTINS]
                    + [tr.kabir_func_index[i]
                       for i in range(len(tr.funcs))])
        # Flag 0: active segment, table 0 implied, offset expr, then
        # the function-index vector (no table index byte).
        segment = (b"\x00" + bytes([OP_I32_CONST]) + _sleb(0)
                   + bytes([OP_END])
                   + _vec([_uleb(f) for f in func_ids]))
        return _section(9, _vec([segment]))

    def code_section():
        return _section(10, _vec([_uleb(len(b)) + b for b in bodies]))

    def data_section():
        segments = []
        for s in tr.strings:
            data = s.encode("utf-8")
            if not data:
                continue
            segments.append(
                b"\x00" + bytes([OP_I32_CONST])
                + _sleb(tr.str_addr[s]) + bytes([OP_END])
                + _uleb(len(data)) + data)
        return _section(11, _vec(segments))

    module = (b"\x00asm" + b"\x01\x00\x00\x00"
              + type_section()
              + import_section()
              + function_section()
              + table_section()
              + memory_section()
              + global_section()
              + export_section()
              + element_section()
              + code_section()
              + data_section())
    return module


def compile_wasm_code(code):
    """Compile an already-compiled code dict (v2) to .wasm bytes."""
    return _build_module(_Translator(code))


def compile_wasm(source, filename="<stdin>"):
    """Check, compile and translate Kabir *source* to .wasm bytes.

    Runs the same type checker as ``kabir run`` first, so a program
    the VM path rejects is rejected here too. Raises ``KabirError``
    for unsupported constructs (import, dicts, string methods,
    closures, calls with more than 32 arguments).
    """
    program = _parse_source(source, filename)
    lines = source.split("\n")
    check_program(program, filename=filename, lines=lines)
    code = compile_program(program, filename, lines)
    return compile_wasm_code(code)


def run_wasm(wasm_bytes, timeout=10):
    """Run .wasm bytes under Node and return the program's output.

    Node (v24 here) instantiates the module with the ``env`` imports
    from :data:`HARNESS_JS` and calls ``main(0)``. Raises
    ``KabirError`` if Node is missing, the module fails to
    instantiate, or the program traps.
    """
    node = shutil.which("node")
    if node is None:
        raise KabirError("WASM: Node.js is needed to run .wasm files, "
                         "but no 'node' was found on PATH")
    with tempfile.TemporaryDirectory(prefix="kabir-wasm-") as tmp:
        mod_path = os.path.join(tmp, "program.wasm")
        js_path = os.path.join(tmp, "harness.js")
        with open(mod_path, "wb") as handle:
            handle.write(wasm_bytes)
        with open(js_path, "w", encoding="utf-8") as handle:
            handle.write(HARNESS_JS)
        try:
            proc = subprocess.run(
                [node, js_path, mod_path],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise KabirError("WASM: program did not finish within %d "
                             "seconds" % timeout)
        if proc.returncode != 0:
            raise KabirError("WASM: run failed: %s"
                             % proc.stderr.strip())
        return proc.stdout


def run_wasm_source(source, filename="<stdin>", timeout=10):
    """Compile *source* to WASM and run it under Node; return output."""
    return run_wasm(compile_wasm(source, filename), timeout=timeout)
