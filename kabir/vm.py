"""Stack VM for Kabir 2.0.0.

Executes the bytecode produced by ``kabir/compile.py``. Frames hold
an env chain; the value stack is one list shared across frames, each
frame recording its base so CALL/RETURN slice correctly. Builtins run
through the ``(interp, node, args)`` protocol unchanged: the VM
exposes ``.error(node, msg)``, ``.format_value(v)`` and ``._random``.
"""

import math
import os
import random

from . import ast_nodes as ast  # noqa: F401  (node shapes, for reference)
from . import semantics as _semantics
from .builtins import BuiltinFunction, _type_name
from .compile import compile_program, compile_source
from .errors import KabirError, error_at
from .interpreter import Environment
from .parser import parse as _parse_source
from .stdlib import STRING_METHODS, build_stdlib
from .builtins import build_builtins


def _article(word):
    return "an" if word[:1] in "aeiou" else "a"


class _CallNode:
    """Tiny node shim for builtin calls: carries line + filename."""

    def __init__(self, line, filename):
        self.line = line
        self.col = None
        self.filename = filename


class VMFunction:
    """A user-defined Kabir function value (a closure over bytecode)."""

    def __init__(self, name, params, body, closure_env):
        self.name = name or "<anonymous>"
        self.params = params
        self.body = body  # a code dict
        self.closure_env = closure_env

    def __repr__(self):
        return "<function %s>" % self.name


class _Frame:
    __slots__ = ("code", "ip", "stack_base", "env", "module_info")

    def __init__(self, code, stack_base, env, module_info=None):
        self.code = code
        self.ip = 0
        self.stack_base = stack_base
        self.env = env
        # module_info: (canonical_path, module_env, importer_env) for a
        # frame running an imported module; None otherwise.
        self.module_info = module_info


class VM:
    MAX_CALL_DEPTH = 500

    def __init__(self, source="", filename="<stdin>"):
        self.source = source
        self.filename = filename
        self.lines = source.split("\n") if source else []
        self.globals = Environment()
        self.modules = {}    # canonical path -> exported-bindings dict
        self._importing = []  # canonical paths currently being executed
        for name, fn in build_builtins().items():
            self.globals.define(name, fn)
        self._random = random.Random()
        for name, fn in build_stdlib().items():
            self.globals.define(name, fn)
        self.globals.define("pi", math.pi)
        self.globals.define("e", math.e)
        self._file_lines = {}
        if filename:
            self._file_lines[filename] = self.lines
        self._frames = []
        self._stack = []

    # -- errors --------------------------------------------------------
    def error(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        filename = getattr(node, "filename", None) \
            or self._current_filename()
        lines = self._file_lines.get(filename) or self.lines
        raise error_at(message, filename, lines, line, col)

    def _current_filename(self):
        if self._frames:
            return self._frames[-1].code.get("filename") or self.filename
        return self.filename

    def _instr_error(self, frame, line, message):
        filename = frame.code.get("filename") or self.filename
        lines = self._file_lines.get(filename) or self.lines
        raise error_at(message, filename, lines, line, None)

    # -- shared semantics ----------------------------------------------
    def format_value(self, value):
        return _semantics.format_value(value)

    @staticmethod
    def is_truthy(value):
        return _semantics.is_truthy(value)

    # -- running -------------------------------------------------------
    def run_source(self, source, filename="<stdin>"):
        """Parse, compile and run source text."""
        self.source = source
        self.filename = filename
        self.lines = source.split("\n")
        self._file_lines[filename] = self.lines
        code = compile_source(source, filename)
        return self.run_code(code, filename=filename, lines=self.lines)

    def run_code(self, code, filename=None, lines=None):
        """Run an already-compiled code dict. Returns the main value."""
        filename = filename or code.get("filename") or self.filename
        if lines is not None:
            self._file_lines[filename] = lines
        elif filename in self._file_lines:
            lines = self._file_lines[filename]
        self._stack = []
        self._frames = [_Frame(code, 0, self.globals)]
        self._importing = []
        try:
            return self._loop()
        except KabirError:
            self._importing = []
            raise

    # -- main loop -----------------------------------------------------
    def _loop(self):
        stack = self._stack
        frames = self._frames
        # Dispatch table, built once per VM: looking up "_op_" + op with
        # getattr on every instruction was the single hottest cost.
        dispatch = self.__dict__.get("_dispatch")
        if dispatch is None:
            dispatch = self._dispatch = {
                name[4:]: getattr(self, name)
                for name in dir(self) if name.startswith("_op_")
            }
        frame = None
        instrs = instr_lines = None
        while frames:
            cur = frames[-1]
            if cur is not frame:
                # Frame changed (call / return): re-read its code.
                frame = cur
                code = frame.code
                instrs = code["instrs"]
                instr_lines = code["lines"]
            if frame.ip >= len(instrs):
                value = self._finish_frame(frame, None)
                if value is _MAIN_DONE:
                    return None
                frame = None
                continue
            op, arg = instrs[frame.ip]
            line = instr_lines[frame.ip]
            frame.ip += 1
            handler = dispatch.get(op)
            if handler is None:
                self._instr_error(frame, line, "Unknown instruction %r" % op)
            result = handler(frame, arg, line, stack, frames)
            if isinstance(result, tuple) and result[0] is _MAIN_DONE:
                return result[1]
        return None

    def _finish_frame(self, frame, value):
        """Pop a frame; return _MAIN_DONE if that was the main frame."""
        stack = self._stack
        frames = self._frames
        del stack[frame.stack_base:]
        frames.pop()
        if frame.module_info is not None:
            canonical, module_env, importer_env = frame.module_info
            if canonical in self._importing:
                self._importing.remove(canonical)
            exports = dict(module_env.vars)
            self.modules[canonical] = exports
            for name, val in exports.items():
                importer_env.define(name, val)
            return None  # module return value is discarded
        if not frames:
            return _MAIN_DONE
        stack.append(value)
        return None

    # -- helpers -------------------------------------------------------
    def _call_value(self, callee, args, frame, line, frames):
        """Call a value; pushes the result or a new frame. Returns True
        when a frame was pushed (caller must not push a result)."""
        stack = self._stack
        if isinstance(callee, BuiltinFunction):
            node = _CallNode(line, frame.code.get("filename")
                             or self.filename)
            stack.append(callee.impl(self, node, args))
            return False
        if isinstance(callee, VMFunction):
            if len(args) != len(callee.params):
                self._instr_error(
                    frame, line,
                    "Function '%s' takes %d argument%s, but got %d"
                    % (callee.name, len(callee.params),
                       "" if len(callee.params) == 1 else "s", len(args)))
            if len(frames) - 1 >= self.MAX_CALL_DEPTH:
                self._instr_error(
                    frame, line,
                    "Too many nested function calls -- check for "
                    "infinite recursion")
            env = Environment(callee.closure_env)
            for name, value in zip(callee.params, args):
                env.define(name, value)
            frames.append(_Frame(callee.body, len(stack), env))
            return True
        if isinstance(callee, str):
            self._instr_error(
                frame, line,
                "'%s' is a string, not a function -- check that you "
                "are calling the right name" % callee)
        self._instr_error(
            frame, line,
            "This value (%s) is not a function, so it cannot be called"
            % _type_name(callee))

    def _binary(self, frame, line, op, stack):
        right = stack.pop()
        left = stack.pop()
        try:
            stack.append(_semantics.binary_op(op, left, right))
        except _semantics.KabirRuntimeError as exc:
            self._instr_error(frame, line,
                              exc.args[0] if exc.args else str(exc))

    # -- imports -------------------------------------------------------
    def _load_module(self, path, frame, line, frames):
        cur_filename = frame.code.get("filename") or self.filename
        if cur_filename.startswith("<"):
            base = os.getcwd()
        else:
            base = os.path.dirname(os.path.abspath(cur_filename))
        resolved = os.path.join(base, path)
        canonical = os.path.realpath(os.path.abspath(resolved))
        if canonical in self._importing:
            chain = [os.path.basename(p) for p in self._importing]
            chain.append(os.path.basename(canonical))
            self._instr_error(frame, line, "Import cycle detected: %s"
                              % " -> ".join(chain))
        if canonical in self.modules:
            for name, value in self.modules[canonical].items():
                frame.env.define(name, value)
            return
        try:
            with open(canonical, "r", encoding="utf-8") as handle:
                source = handle.read()
        except FileNotFoundError:
            self._instr_error(frame, line,
                              'Cannot import "%s" -- no such file' % path)
        except UnicodeDecodeError:
            self._instr_error(frame, line,
                              'Cannot import "%s" -- the file is not '
                              'valid UTF-8' % path)
        except OSError as exc:
            self._instr_error(frame, line,
                              'Cannot import "%s" -- %s' % (path, exc))
        program = _parse_source(source, canonical)
        module_lines = source.split("\n")
        self._file_lines[canonical] = module_lines
        code = compile_program(program, canonical, module_lines)
        module_env = Environment(self.globals)
        self._importing.append(canonical)
        frames.append(_Frame(code, len(self._stack), module_env,
                             module_info=(canonical, module_env,
                                          frame.env)))

    # -- instructions --------------------------------------------------
    # Each _op_* takes (frame, arg, line, stack, frames). RETURN and
    # module completion are handled through _finish_frame; _MAIN_DONE
    # (a sentinel) ends the whole run.

    def _op_LOAD_CONST(self, frame, arg, line, stack, frames):
        stack.append(frame.code["consts"][arg])

    def _op_LOAD_NULL(self, frame, arg, line, stack, frames):
        stack.append(None)

    def _op_LOAD_TRUE(self, frame, arg, line, stack, frames):
        stack.append(True)

    def _op_LOAD_FALSE(self, frame, arg, line, stack, frames):
        stack.append(False)

    def _op_LOAD_VAR(self, frame, arg, line, stack, frames):
        if frame.env.has(arg):
            stack.append(frame.env.lookup(arg))
        else:
            self._instr_error(
                frame, line,
                "Undefined variable '%s' -- it was never defined "
                "with 'let'. Check the spelling" % arg)

    def _op_DEFINE_VAR(self, frame, arg, line, stack, frames):
        frame.env.define(arg, stack.pop())

    def _op_STORE_VAR(self, frame, arg, line, stack, frames):
        value = stack.pop()
        if not frame.env.assign(arg, value):
            self._instr_error(
                frame, line,
                "Cannot assign to '%s' -- it was never defined. "
                "Use 'let %s = ...' first" % (arg, arg))

    def _op_POP(self, frame, arg, line, stack, frames):
        stack.pop()

    def _op_DUP(self, frame, arg, line, stack, frames):
        stack.append(stack[-1])

    def _op_BUILD_LIST(self, frame, arg, line, stack, frames):
        if arg:
            items = stack[-arg:]
            del stack[-arg:]
        else:
            items = []
        stack.append(list(items))

    def _op_BUILD_DICT(self, frame, arg, line, stack, frames):
        result = {}
        if arg:
            flat = stack[-2 * arg:]
            del stack[-2 * arg:]
            for i in range(arg):
                result[flat[2 * i]] = flat[2 * i + 1]
        stack.append(result)

    def _op_BUILD_STRING(self, frame, arg, line, stack, frames):
        parts = stack[-arg:] if arg else []
        if arg:
            del stack[-arg:]
        out = []
        for part in parts:
            out.append(part if isinstance(part, str)
                       else self.format_value(part))
        stack.append("".join(out))

    def _op_ADD(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "+", stack)

    def _op_SUB(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "-", stack)

    def _op_MUL(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "*", stack)

    def _op_DIV(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "/", stack)

    def _op_MOD(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "%", stack)

    def _op_POW(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "**", stack)

    def _op_NEG(self, frame, arg, line, stack, frames):
        value = stack.pop()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._instr_error(frame, line,
                              "Cannot negate %s -- '-' needs a number"
                              % _type_name(value))
        stack.append(-value)

    def _op_NOT(self, frame, arg, line, stack, frames):
        stack.append(not _semantics.is_truthy(stack.pop()))

    def _op_EQ(self, frame, arg, line, stack, frames):
        right = stack.pop()
        left = stack.pop()
        stack.append(_semantics.kabir_equal(left, right))

    def _op_NE(self, frame, arg, line, stack, frames):
        right = stack.pop()
        left = stack.pop()
        stack.append(not _semantics.kabir_equal(left, right))

    def _op_LT(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "<", stack)

    def _op_LE(self, frame, arg, line, stack, frames):
        self._binary(frame, line, "<=", stack)

    def _op_GT(self, frame, arg, line, stack, frames):
        self._binary(frame, line, ">", stack)

    def _op_GE(self, frame, arg, line, stack, frames):
        self._binary(frame, line, ">=", stack)

    def _op_JUMP(self, frame, arg, line, stack, frames):
        frame.ip = arg

    def _op_JUMP_IF_FALSE(self, frame, arg, line, stack, frames):
        if not _semantics.is_truthy(stack.pop()):
            frame.ip = arg

    def _op_JUMP_IF_TRUE(self, frame, arg, line, stack, frames):
        if _semantics.is_truthy(stack.pop()):
            frame.ip = arg

    def _op_CALL(self, frame, arg, line, stack, frames):
        if arg:
            args = stack[-arg:]
            del stack[-arg:]
        else:
            args = []
        callee = stack.pop()
        self._call_value(callee, args, frame, line, frames)

    def _op_RETURN(self, frame, arg, line, stack, frames):
        value = stack.pop() if len(stack) > frame.stack_base else None
        result = self._finish_frame(frame, value)
        if result is _MAIN_DONE:
            # Signal the loop to stop with the main return value.
            return (_MAIN_DONE, value)
        return None

    def _op_MAKE_FUNCTION(self, frame, arg, line, stack, frames):
        const = frame.code["consts"][arg]
        code = const["__code__"]
        stack.append(VMFunction(code["name"], code["params"], code,
                                frame.env))

    def _op_IMPORT(self, frame, arg, line, stack, frames):
        self._load_module(arg, frame, line, frames)

    def _op_GET_INDEX(self, frame, arg, line, stack, frames):
        index = stack.pop()
        target = stack.pop()
        if isinstance(target, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line,
                    "An array index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self._instr_error(
                    frame, line,
                    "Array index %d is out of range -- the array "
                    "has %d item%s"
                    % (index, len(target),
                       "" if len(target) == 1 else "s"))
            stack.append(target[idx])
        elif isinstance(target, str):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line,
                    "A string index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self._instr_error(
                    frame, line,
                    "String index %d is out of range -- the string "
                    "has %d character%s"
                    % (index, len(target),
                       "" if len(target) == 1 else "s"))
            stack.append(target[idx])
        elif isinstance(target, dict):
            if not isinstance(index, str):
                self._instr_error(
                    frame, line,
                    "A dict key must be a string, not %s"
                    % _type_name(index))
            if index not in target:
                self._instr_error(
                    frame, line,
                    "No key %r in this dict -- available keys: %s"
                    % (index, ", ".join('"%s"' % k for k in target.keys())
                       or "none"))
            stack.append(target[index])
        else:
            self._instr_error(
                frame, line,
                "Cannot index %s -- only arrays, strings and dicts "
                "support [ ... ]" % _type_name(target))

    def _op_SET_INDEX(self, frame, arg, line, stack, frames):
        value = stack.pop()
        index = stack.pop()
        container = stack.pop()
        if isinstance(container, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line,
                    "An array index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(container) if index < 0 else index
            if idx < 0 or idx >= len(container):
                self._instr_error(
                    frame, line,
                    "Array index %d is out of range -- the array "
                    "has %d item%s"
                    % (index, len(container),
                       "" if len(container) == 1 else "s"))
            container[idx] = value
        elif isinstance(container, dict):
            if not isinstance(index, str):
                self._instr_error(
                    frame, line,
                    "A dict key must be a string, not %s"
                    % _type_name(index))
            container[index] = value
        else:
            self._instr_error(
                frame, line,
                "Cannot assign into %s -- only arrays and dicts "
                "support index assignment" % _type_name(container))
        stack.append(None)

    def _op_GET_MEMBER(self, frame, arg, line, stack, frames):
        receiver = stack.pop()
        if isinstance(receiver, str):
            if arg in STRING_METHODS:
                def bound(interp, node, args,
                          _name=arg, _recv=receiver):
                    return STRING_METHODS[_name](interp, node, _recv, args)
                stack.append(BuiltinFunction(arg, bound))
                return
            self._instr_error(
                frame, line,
                "No method '.%s' on a string -- strings have no "
                "method named '%s'" % (arg, arg))
        if isinstance(receiver, dict):
            self._instr_error(
                frame, line,
                "Cannot use .%s on a dict -- write dict[\"%s\"] "
                "instead (for example, user[\"name\"])" % (arg, arg))
        self._instr_error(
            frame, line,
            "Cannot use .%s on %s %s -- only strings have methods"
            % (arg, _article(_type_name(receiver)), _type_name(receiver)))

    def _op_ENTER_SCOPE(self, frame, arg, line, stack, frames):
        frame.env = Environment(frame.env)

    def _op_EXIT_SCOPE(self, frame, arg, line, stack, frames):
        if frame.env.parent is not None:
            frame.env = frame.env.parent

    def _op_GET_ITER(self, frame, arg, line, stack, frames):
        iterable = stack.pop()
        if isinstance(iterable, str):
            items = list(iterable)
        elif isinstance(iterable, list):
            items = list(iterable)
        elif isinstance(iterable, dict):
            items = list(iterable.keys())
        else:
            self._instr_error(
                frame, line,
                "Cannot loop over %s -- 'for' needs an array, a "
                "string, or a range" % _type_name(iterable))
        stack.append(iter(items))

    def _op_FOR_ITER(self, frame, arg, line, stack, frames):
        iterator = stack[-1]
        try:
            stack.append(next(iterator))
        except StopIteration:
            stack.pop()
            frame.ip = arg

    def _op_MATCH_FAIL(self, frame, arg, line, stack, frames):
        value = stack.pop()
        self._instr_error(frame, line,
                          "No pattern matched the value %s"
                          % self.format_value(value))


class _MainDoneType:
    pass


_MAIN_DONE = _MainDoneType()
