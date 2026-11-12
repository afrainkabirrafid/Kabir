"""Native code generation for Kabir (3.0.0 Wave 1): AST -> C99 -> binary.

Transpiles Kabir source to C99, compiles it with the system ``cc``
(gcc 13.3.0 here) against ``kabir/native/rt.c``, and runs the result.
No new dependencies.

Memory model: native (arena). All heap values are arena-allocated by
the runtime (see kabir/native/rt.h) and freed in one pass at exit.
This is NOT refcounting -- it is honestly reported as "native
(arena)" everywhere. Refcounting was not attempted: correct
retain/release emission across mutation (push/pop/index assignment),
early returns, and first-class functions needs a borrow/ownership
analysis that does not exist yet, and a half-correct refcounting
backend would be worse than a correct arena one.

Supported subset (anything else is a compile-time KabirError, never
silently wrong code):

  values      numbers (as C doubles -- integers beyond 2**53 lose
              precision, and a whole-valued number such as 1.0 is
              accepted as an array index), strings, bools, null,
              arrays, dicts
  statements  let (incl. `let mut`, which has no runtime effect --
              it is a checker-level feature), assignment (=, +=, -=,
              incl. index targets), if/elif/else, while, for (over
              arrays, strings, dicts), break, continue, return,
              blocks, expression statements
  expressions binary ops (+ - * / % ** == != < <= > >= and or),
              unary - / not, calls, indexing, string methods
              (upper, lower, strip, split, join, contains,
              starts_with, ends_with, replace), string interpolation,
              match (literal / binding / wildcard patterns, guards)
  functions   named and anonymous, recursion, mutual recursion
              between top-level functions, first-class function
              values, all builtins (print, input, len, str, num,
              type, range, push, pop, sqrt, pow, abs, keys, int,
              float) and all stdlib functions (randint, randrange,
              choice, shuffle, seed, floor, ceil, sin, cos, tan, log,
              exp, read_file, write_file, file_exists, json_parse,
              json_stringify, http_get, http_post, now, sleep)
  NOT supported (clear error at transpile time):
              - closures: a function that reads a variable bound in
                an enclosing function ("closures not supported in
                native mode yet")
              - import ("import not supported in native mode yet")
              - async/await (not in the AST this build targets)

Known divergences from the VM (documented, not hidden):
  - Numbers are doubles: `print(9007199254740993)` may print a
    rounded value; `a[1.0]` works instead of erroring.
  - Random functions use C rand(), not Python's Mersenne Twister:
    same ranges, different sequence (seed() still seeds it).
  - String methods are byte/ASCII-based: upper/lower/strip and
    indexing work on bytes, so non-ASCII text may differ.
  - `http_get`/`http_post` support http:// only; https is a clear
    runtime error. Timeouts are 10 seconds.
  - json_stringify leaves non-ASCII characters as UTF-8 instead of
    escaping them as \\uXXXX (Python's default).
  - Native runtime errors print "Kabir runtime error: <message>"
    and exit(1); they do not carry file:line:col positions.
  - The checker is NOT run by transpile(): the CLI wiring must run
    check_program first, exactly like the VM path does.

Public API:
  transpile(source, filename="<stdin>") -> str  (C source)
  compile_native(source, filename, out_path) -> path (binary path)
  run_native(source, filename="<stdin>", input_text="") ->
      (exit_code, stdout, stderr)
"""

import os
import shutil
import subprocess
import tempfile

from . import ast_nodes as ast
from .builtins import BUILTINS
from .errors import KabirError, error_at
from .parser import parse as _parse_source
from .stdlib import STDLIB, STRING_METHODS

MEMORY_MODEL = "arena"  # "native (arena)" -- see the module docstring

_NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "native")
_RT_H = os.path.join(_NATIVE_DIR, "rt.h")
_RT_C = os.path.join(_NATIVE_DIR, "rt.c")

_BUILTIN_NAMES = set(BUILTINS) | set(STDLIB)
_METHOD_NAMES = set(STRING_METHODS)

_BINOP_FN = {
    "+": "k_add", "-": "k_sub", "*": "k_mul", "/": "k_div",
    "%": "k_mod", "**": "k_pow",
}


def _c_str(value):
    """Return *value* (a Python str) as a C string literal (ASCII)."""
    out = ['"']
    for byte in value.encode("utf-8"):
        if byte == 0x22:        # "
            out.append('\\"')
        elif byte == 0x5C:      # backslash
            out.append("\\\\")
        elif byte == 0x0A:
            out.append("\\n")
        elif byte == 0x09:
            out.append("\\t")
        elif byte == 0x0D:
            out.append("\\r")
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append("\\%03o" % byte)
    out.append('"')
    return "".join(out)


def _c_num(value):
    """Return a Kabir number literal as a C double literal."""
    if isinstance(value, bool):
        raise ValueError("bool is not a number literal")
    if isinstance(value, int):
        return repr(float(value)) if abs(value) >= 1 << 53 else repr(value)
    return repr(float(value))


def _c_ident(name):
    """Make a Kabir identifier safe as a C identifier fragment."""
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                   for ch in name)
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return safe


# ----------------------------------------------------------------------
# Static analysis: closures + undefined names inside functions.
# ----------------------------------------------------------------------

def _walk_names(node, bound, refs, nested, filename):
    """Collect names bound / referenced by *node*.

    Does NOT descend into nested FuncDecl bodies (their names belong
    to them); records each nested FuncDecl in *nested* and its name
    (if any) in *bound*. String interpolations are parsed and walked.
    """
    if node is None:
        return
    if isinstance(node, ast.FuncDecl):
        if node.name is not None:
            bound.add(node.name)
        nested.append(node)
        return
    if isinstance(node, ast.LetStmt):
        bound.add(node.name)
        _walk_names(node.value, bound, refs, nested, filename)
        return
    if isinstance(node, ast.ForStmt):
        bound.add(node.var)
        _walk_names(node.iterable, bound, refs, nested, filename)
        _walk_names(node.body, bound, refs, nested, filename)
        return
    if isinstance(node, ast.MatchExpr):
        _walk_names(node.scrutinee, bound, refs, nested, filename)
        for arm in node.arms:
            if isinstance(arm.pattern, ast.Var):
                bound.add(arm.pattern.name)
            if arm.guard is not None:
                _walk_names(arm.guard, bound, refs, nested, filename)
            _walk_names(arm.body, bound, refs, nested, filename)
        return
    if isinstance(node, ast.StringLiteral):
        for kind, text in node.parts:
            if kind != "expr":
                continue
            try:
                inner = _parse_source(text, filename)
            except Exception:
                continue
            for stmt in inner.statements:
                _walk_names(stmt, bound, refs, nested, filename)
        return
    if isinstance(node, ast.Var):
        refs.add(node.name)
        return
    if isinstance(node, ast.AssignStmt):
        if isinstance(node.target, ast.Var):
            refs.add(node.target.name)
        else:
            _walk_names(node.target, bound, refs, nested, filename)
        _walk_names(node.value, bound, refs, nested, filename)
        return
    if isinstance(node, ast.Node):
        for value in vars(node).values():
            if isinstance(value, ast.Node):
                _walk_names(value, bound, refs, nested, filename)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, ast.Node):
                        _walk_names(item, bound, refs, nested, filename)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, ast.Node):
                                _walk_names(part, bound, refs, nested,
                                            filename)


def _check_function(func, enclosing_bound, global_names, filename, lines):
    """Reject closures and undefined names inside one function."""
    bound = set(func.params)
    refs = set()
    nested = []
    for stmt in func.body.statements:
        _walk_names(stmt, bound, refs, nested, filename)
    free = refs - bound - global_names - _BUILTIN_NAMES
    enclosing = set()
    for scope in enclosing_bound:
        enclosing |= scope
    closure_names = sorted(free & enclosing)
    if closure_names:
        raise error_at(
            "closures not supported in native mode yet -- function "
            "'%s' reads '%s', which belongs to an enclosing function"
            % (func.name or "<anonymous>", closure_names[0]),
            filename, lines, func.line, func.col)
    undefined = sorted(free - enclosing)
    if undefined:
        raise error_at(
            "Undefined variable '%s' -- it was never defined with "
            "'let'. Check the spelling" % undefined[0],
            filename, lines, func.line, func.col)
    for child in nested:
        _check_function(child, enclosing_bound + [bound], global_names,
                        filename, lines)


def _check_functions(program, global_names, filename, lines):
    """Walk every function in the program for closures/undefined names."""
    nested = []
    for stmt in program.statements:
        if isinstance(stmt, ast.FuncDecl):
            _check_function(stmt, [], global_names, filename, lines)
        else:
            bound, refs = set(), set()
            _walk_names(stmt, bound, refs, nested, filename)
    # Functions nested inside non-function top-level statements
    # (blocks, ifs) are checked via the same rule.
    for func in nested:
        _check_function(func, [], global_names, filename, lines)


class _CodeGen:
    """Transpiles one parsed program to C99."""

    def __init__(self, program, filename, lines):
        self.program = program
        self.filename = filename
        self.lines = lines
        # Top-level globals: direct `let` / named `func` statements.
        self.global_names = []
        for stmt in program.statements:
            if isinstance(stmt, ast.LetStmt):
                self.global_names.append(stmt.name)
            elif isinstance(stmt, ast.FuncDecl) and stmt.name is not None:
                self.global_names.append(stmt.name)
        self.global_set = set(self.global_names)
        self.global_c = {
            name: "g_k_" + _c_ident(name) for name in self.global_names}
        # Hoist every function (anywhere) to a top-level C function.
        self.funcs = []  # (FuncDecl node, c name) in first-seen order
        self.func_c = {}
        self._collect_funcs(program.statements)
        self._scopes = [{}]      # name -> C variable, innermost last
        self._counter = 0
        self._out = []            # lines of the current C function
        self._loop_depth = 0
        self._function_depth = 0

    # -- collection ------------------------------------------------------
    def _collect_funcs(self, statements):
        for stmt in statements:
            self._collect_from_node(stmt)

    def _collect_from_node(self, node):
        if node is None:
            return
        if isinstance(node, ast.FuncDecl):
            cname = "kabir_fn_%d" % len(self.funcs)
            self.funcs.append((node, cname))
            self.func_c[id(node)] = cname
            # Do not descend into the body here: nested functions are
            # found when their own containing function is generated.
            # (They are still collected: generate() walks every
            # hoisted function body for further nested FuncDecls.)
            self._collect_from_node_body(node)
            return
        if isinstance(node, ast.StringLiteral):
            for kind, text in node.parts:
                if kind != "expr":
                    continue
                try:
                    inner = _parse_source(text, self.filename)
                except Exception:
                    continue
                for stmt in inner.statements:
                    self._collect_from_node(stmt)
            return
        if isinstance(node, ast.Node):
            for value in vars(node).values():
                if isinstance(value, ast.Node):
                    self._collect_from_node(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, ast.Node):
                            self._collect_from_node(item)
                        elif isinstance(item, tuple):
                            for part in item:
                                if isinstance(part, ast.Node):
                                    self._collect_from_node(part)

    def _collect_from_node_body(self, func):
        """Collect FuncDecls nested inside *func*'s body."""
        for stmt in func.body.statements:
            self._collect_from_node(stmt)

    # -- helpers ---------------------------------------------------------
    def fail(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        raise error_at(message, self.filename, self.lines, line, col)

    def _fresh(self, prefix):
        self._counter += 1
        return "%s%d" % (prefix, self._counter)

    def _emit(self, text):
        self._out.append(text)

    def _resolve(self, node, name):
        """C expression for reading variable *name*."""
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        if name in self.global_set:
            return self.global_c[name]
        if name in _BUILTIN_NAMES:
            return 'k_builtin(%s)' % _c_str(name)
        self.fail(node, "Undefined variable '%s' -- it was never defined "
                  "with 'let'. Check the spelling" % name)

    def _assign_target(self, node, name):
        """C lvalue for assigning to variable *name* (locals/globals)."""
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        if name in self.global_set:
            return self.global_c[name]
        self.fail(node, "Cannot assign to '%s' -- it was never defined. "
                  "Use 'let %s = ...' first" % (name, name))

    def _parse_interpolation(self, text, node):
        try:
            inner = _parse_source(text, self.filename)
        except KabirError as err:
            raise KabirError(
                err.message, filename=self.filename,
                line=node.line, col=node.col,
                source_line=self.lines[node.line - 1]
                if self.lines and node.line
                and node.line <= len(self.lines) else None)
        if len(inner.statements) != 1 \
                or not isinstance(inner.statements[0], ast.ExprStmt):
            self.fail(node, "Only one expression is allowed inside "
                      "{ ... }")
        return inner.statements[0].expr

    # -- top level -------------------------------------------------------
    def generate(self):
        parts = []
        parts.append("/* Generated by Kabir native codegen (3.0.0 Wave 1).")
        parts.append("   Memory model: native (arena) -- see rt.h. */")
        parts.append('#include "rt.h"')
        parts.append("")
        # Forward declarations for every hoisted function.
        for _node, cname in self.funcs:
            parts.append("static KVal %s(KVal *args, int nargs);" % cname)
        if self.funcs:
            parts.append("")
        # Globals.
        for name in self.global_names:
            parts.append("static KVal %s;" % self.global_c[name])
        if self.global_names:
            parts.append("")
        # Function definitions.
        for node, cname in self.funcs:
            parts.append(self._gen_function(node, cname))
            parts.append("")
        # main().
        self._scopes = [{}]
        self._counter = 0
        self._out = []
        self._loop_depth = 0
        self._function_depth = 0
        self._emit("int main(void) {")
        self._emit("    k_rt_init();")
        for stmt in self.program.statements:
            self._gen_stmt(stmt, indent="    ")
        self._emit("    k_rt_cleanup();")
        self._emit("    return 0;")
        self._emit("}")
        parts.append("\n".join(self._out))
        return "\n".join(parts) + "\n"

    def _gen_function(self, node, cname):
        self._scopes = [{}]
        self._counter = 0
        self._out = []
        self._loop_depth = 0
        self._function_depth = 1
        self._emit("static KVal %s(KVal *args, int nargs) {" % cname)
        self._emit("    (void)nargs; /* arity is checked in k_call */")
        for i, param in enumerate(node.params):
            var = self._fresh("v")
            self._scopes[0][param] = var
            self._emit("    KVal %s = args[%d];" % (var, i))
        self._gen_block_stmts(node.body, indent="    ")
        self._emit("    return k_null();")
        self._emit("}")
        return "\n".join(self._out)

    # -- statements ------------------------------------------------------
    def _gen_block(self, block, indent):
        self._emit(indent + "{")
        self._gen_block_stmts(block, indent + "    ")
        self._emit(indent + "}")

    def _gen_block_stmts(self, block, indent):
        self._scopes.append({})
        for stmt in block.statements:
            self._gen_stmt(stmt, indent)
        self._scopes.pop()

    def _gen_stmt(self, stmt, indent):
        method = getattr(self, "_stmt_" + type(stmt).__name__, None)
        if method is None:
            self.fail(stmt, "Cannot compile this statement in native "
                      "mode yet")
        method(stmt, indent)

    def _stmt_LetStmt(self, stmt, indent):
        value = self._gen_expr(stmt.value, indent)
        if self._function_depth == 0 and len(self._scopes) == 1:
            # Top-level let: assign the global (declared up front).
            self._emit("%s%s = %s;" % (indent, self.global_c[stmt.name],
                                       value))
        else:
            var = self._fresh("v")
            self._scopes[-1][stmt.name] = var
            self._emit("%sKVal %s = %s;" % (indent, var, value))

    def _stmt_AssignStmt(self, stmt, indent):
        if isinstance(stmt.target, ast.Var):
            value = self._gen_expr(stmt.value, indent)
            target = self._assign_target(stmt.target, stmt.target.name)
            if stmt.op == "=":
                self._emit("%s%s = %s;" % (indent, target, value))
            else:
                fn = "k_add" if stmt.op == "+=" else "k_sub"
                self._emit("%s%s = %s(%s, %s);"
                           % (indent, target, fn, target, value))
            return
        # Index target.
        target = stmt.target
        value = self._gen_expr(stmt.value, indent)
        container = self._gen_expr(target.target, indent)
        index = self._gen_expr(target.index, indent)
        if stmt.op == "=":
            self._emit("%sk_set_index(%s, %s, %s);"
                       % (indent, container, index, value))
            return
        fn = "k_add" if stmt.op == "+=" else "k_sub"
        cur = self._fresh("t")
        new = self._fresh("t")
        self._emit("%sKVal %s = k_get_index(%s, %s);"
                   % (indent, cur, container, index))
        self._emit("%sKVal %s = %s(%s, %s);"
                   % (indent, new, fn, cur, value))
        container2 = self._gen_expr(target.target, indent)
        index2 = self._gen_expr(target.index, indent)
        self._emit("%sk_set_index(%s, %s, %s);"
                   % (indent, container2, index2, new))

    def _stmt_ExprStmt(self, stmt, indent):
        value = self._gen_expr(stmt.expr, indent)
        self._emit("%s(void)(%s);" % (indent, value))

    def _stmt_FuncDecl(self, stmt, indent):
        # 4.0.0 async is VM-only: an async function would silently
        # run synchronously in native mode, so refuse it outright.
        if getattr(stmt, "is_async", False):
            self.fail(stmt, "async func is VM-only -- native mode "
                      "cannot run async code (run with `kabir run` "
                      "instead)")
        cname = self.func_c[id(stmt)]
        display = stmt.name or "<anonymous>"
        value = "k_make_func(%s, %s, %d)" % (
            cname, _c_str(display), len(stmt.params))
        if stmt.name is None:
            self._emit("%s(void)(%s);" % (indent, value))
            return
        if self._function_depth == 0 and len(self._scopes) == 1:
            self._emit("%s%s = %s;" % (indent, self.global_c[stmt.name],
                                       value))
        else:
            var = self._fresh("v")
            self._scopes[-1][stmt.name] = var
            self._emit("%sKVal %s = %s;" % (indent, var, value))

    def _stmt_ReturnStmt(self, stmt, indent):
        if self._function_depth == 0:
            self.fail(stmt, "'return' can only be used inside a function")
        if stmt.value is not None:
            value = self._gen_expr(stmt.value, indent)
            self._emit("%sreturn %s;" % (indent, value))
        else:
            self._emit("%sreturn k_null();" % indent)

    def _stmt_IfStmt(self, stmt, indent):
        # NOTE: conditions are NOT emitted inline in `if (...)` -- a
        # condition may need preparatory statements (temps, and/or
        # short-circuiting), and those statements between a `}` and
        # an `else if` would detach the chain. So every branch is a
        # guarded block driven by a `done` flag: conditions are
        # evaluated in order and stop at the first match, exactly
        # like the interpreter.
        done = self._fresh("m")
        self._emit("%sint %s = 0;" % (indent, done))
        for condition, body in stmt.branches:
            self._emit("%sif (!%s) {" % (indent, done))
            cond = self._gen_expr(condition, indent + "    ")
            self._emit("%s    if (k_is_truthy(%s)) {" % (indent, cond))
            self._emit("%s        %s = 1;" % (indent, done))
            self._gen_block_stmts(body, indent + "        ")
            self._emit("%s    }" % indent)
            self._emit("%s}" % indent)
        if stmt.else_body is not None:
            self._emit("%sif (!%s) {" % (indent, done))
            self._gen_block_stmts(stmt.else_body, indent + "    ")
            self._emit("%s}" % indent)

    def _stmt_WhileStmt(self, stmt, indent):
        self._emit("%swhile (1) {" % indent)
        cond = self._gen_expr(stmt.condition, indent + "    ")
        self._emit("%s    if (!k_is_truthy(%s)) break;" % (indent, cond))
        self._loop_depth += 1
        self._gen_block(stmt.body, indent + "    ")
        self._loop_depth -= 1
        self._emit("%s}" % indent)

    def _stmt_ForStmt(self, stmt, indent):
        iterable = self._gen_expr(stmt.iterable, indent)
        items = self._fresh("t")
        idx = self._fresh("i")
        self._emit("%sKVal %s = k_iter_items(%s);"
                   % (indent, items, iterable))
        self._emit("%sfor (int %s = 0; %s < k_array_len(%s); %s++) {"
                   % (indent, idx, idx, items, idx))
        self._scopes.append({})
        var = self._fresh("v")
        self._scopes[-1][stmt.var] = var
        self._emit("%s    KVal %s = k_array_get(%s, %s);"
                   % (indent, var, items, idx))
        self._loop_depth += 1
        for inner in stmt.body.statements:
            self._gen_stmt(inner, indent + "    ")
        self._loop_depth -= 1
        self._scopes.pop()
        self._emit("%s}" % indent)

    def _stmt_BreakStmt(self, stmt, indent):
        if self._loop_depth == 0:
            self.fail(stmt, "'break' can only be used inside a loop")
        self._emit("%sbreak;" % indent)

    def _stmt_ContinueStmt(self, stmt, indent):
        if self._loop_depth == 0:
            self.fail(stmt, "'continue' can only be used inside a loop")
        self._emit("%scontinue;" % indent)

    def _stmt_ImportStmt(self, stmt, indent):
        self.fail(stmt, "import not supported in native mode yet")

    def _stmt_Block(self, stmt, indent):
        self._gen_block(stmt, indent)

    # -- expressions -----------------------------------------------------
    # gen_expr returns a C expression string; it may append statements
    # to self._out first (temps are emitted in evaluation order).
    def _gen_expr(self, expr, indent):
        method = getattr(self, "_expr_" + type(expr).__name__, None)
        if method is None:
            self.fail(expr, "Cannot compile this expression in native "
                      "mode yet")
        return method(expr, indent)

    def _temp(self, expr_text, indent):
        var = self._fresh("t")
        self._emit("%sKVal %s = %s;" % (indent, var, expr_text))
        return var

    def _expr_Literal(self, expr, indent):
        value = expr.value
        if value is None:
            return "k_null()"
        if value is True:
            return "k_bool(1)"
        if value is False:
            return "k_bool(0)"
        if isinstance(value, str):
            return "k_str(%s)" % _c_str(value)
        return "k_num(%s)" % _c_num(value)

    def _expr_StringLiteral(self, expr, indent):
        if all(kind == "lit" for kind, _t in expr.parts):
            return "k_str(%s)" % _c_str("".join(t for _k, t in expr.parts))
        parts = []
        for kind, text in expr.parts:
            if kind == "lit":
                parts.append(_c_str(text))
            else:
                inner = self._parse_interpolation(text, expr)
                value = self._gen_expr(inner, indent)
                tmp = self._fresh("t")
                self._emit("%sconst char *%s = k_format(%s);"
                           % (indent, tmp, value))
                parts.append(tmp)
        return "k_build_string(%d, (const char *[]){%s})" % (
            len(parts), ", ".join(parts))

    def _expr_Var(self, expr, indent):
        return self._resolve(expr, expr.name)

    def _expr_BorrowExpr(self, expr, indent):
        # 4.0.0 borrow checker: a borrow IS the value at runtime --
        # `&x` generates just `x`. Zero runtime cost.
        return self._gen_expr(expr.expr, indent)

    def _expr_ArrayLiteral(self, expr, indent):
        temps = [self._gen_expr(el, indent) for el in expr.elements]
        if not temps:
            return "k_new_array()"
        return "k_array_of(%d, (KVal[]){%s})" % (
            len(temps), ", ".join(temps))

    def _expr_DictLiteral(self, expr, indent):
        result = self._fresh("t")
        self._emit("%sKVal %s = k_new_dict();" % (indent, result))
        for key, value_node in expr.pairs:
            value = self._gen_expr(value_node, indent)
            # Dict keys are plain strings; set via a temp to keep the
            # call order obvious. k_dict_set is internal: emulate
            # with a one-element array trick? No -- use k_set_index
            # with a string key (dicts accept string keys).
            key_tmp = self._temp("k_str(%s)" % _c_str(key), indent)
            self._emit("%sk_set_index(%s, %s, %s);"
                       % (indent, result, key_tmp, value))
        return result

    def _expr_BinaryOp(self, expr, indent):
        if expr.op in ("and", "or"):
            left = self._gen_expr(expr.left, indent)
            result = self._temp(left, indent)
            right = self._gen_expr(expr.right, indent)
            if expr.op == "and":
                self._emit("%sif (k_is_truthy(%s)) { %s = %s; }"
                           % (indent, result, result, right))
            else:
                self._emit("%sif (!k_is_truthy(%s)) { %s = %s; }"
                           % (indent, result, result, right))
            return result
        left = self._gen_expr(expr.left, indent)
        right = self._gen_expr(expr.right, indent)
        if expr.op == "==":
            return "k_bool(k_eq(%s, %s))" % (left, right)
        if expr.op == "!=":
            return "k_bool(!k_eq(%s, %s))" % (left, right)
        if expr.op in ("<", "<=", ">", ">="):
            return "k_cmp(%s, %s, %s)" % (_c_str(expr.op), left, right)
        return "%s(%s, %s)" % (_BINOP_FN[expr.op], left, right)

    def _expr_UnaryOp(self, expr, indent):
        value = self._gen_expr(expr.operand, indent)
        if expr.op == "-":
            return "k_neg(%s)" % value
        return "k_bool(!k_is_truthy(%s))" % value

    def _expr_Call(self, expr, indent):
        # 4.0.0 async is VM-only: spawn/chan/send/recv have no
        # native form, so refuse them here with a plain message
        # instead of an "undefined variable" or C error.
        if (isinstance(expr.callee, ast.Var)
                and expr.callee.name in ("spawn", "chan", "send", "recv")):
            self.fail(expr, "%s() is VM-only -- native mode cannot "
                      "run tasks or channels (run with `kabir run` "
                      "instead)" % expr.callee.name)
        callee = self._gen_expr(expr.callee, indent)
        callee_tmp = self._temp(callee, indent)
        arg_tmps = [self._temp(self._gen_expr(a, indent), indent)
                    for a in expr.args]
        if not arg_tmps:
            return "k_call(%s, NULL, 0)" % callee_tmp
        return "k_call(%s, (KVal[]){%s}, %d)" % (
            callee_tmp, ", ".join(arg_tmps), len(arg_tmps))

    def _expr_Index(self, expr, indent):
        target = self._gen_expr(expr.target, indent)
        index = self._gen_expr(expr.index, indent)
        return "k_get_index(%s, %s)" % (target, index)

    def _expr_Member(self, expr, indent):
        receiver = self._gen_expr(expr.object, indent)
        if expr.name not in _METHOD_NAMES:
            # Still emit the runtime check so the error matches the
            # interpreter ("No method '.x' on a string ...").
            return "k_method(%s, %s)" % (receiver, _c_str(expr.name))
        return "k_method(%s, %s)" % (receiver, _c_str(expr.name))

    def _expr_MatchExpr(self, expr, indent):
        scrut = self._temp(self._gen_expr(expr.scrutinee, indent), indent)
        result = self._fresh("t")
        matched = self._fresh("m")
        self._emit("%sKVal %s;" % (indent, result))
        self._emit("%sint %s = 0;" % (indent, matched))
        for arm in expr.arms:
            self._scopes.append({})
            guard_ok = True
            if isinstance(arm.pattern, ast.Literal):
                lit = self._gen_expr(arm.pattern, indent)
                self._emit("%sif (!%s && k_eq(%s, %s)) {"
                           % (indent, matched, scrut, lit))
            elif isinstance(arm.pattern, ast.Var):
                var = self._fresh("v")
                self._scopes[-1][arm.pattern.name] = var
                self._emit("%sif (!%s) {" % (indent, matched))
                self._emit("%s    KVal %s = %s;"
                           % (indent, var, scrut))
            elif isinstance(arm.pattern, ast.Wildcard):
                self._emit("%sif (!%s) {" % (indent, matched))
            else:
                self.fail(arm.pattern, "Invalid pattern in 'match'")
            if arm.guard is not None:
                guard = self._gen_expr(arm.guard, indent + "    ")
                self._emit("%s    if (k_is_truthy(%s)) {"
                           % (indent, guard))
                body = self._gen_expr(arm.body, indent + "        ")
                self._emit("%s        %s = %s;" % (indent, result, body))
                self._emit("%s        %s = 1;" % (indent, matched))
                self._emit("%s    }" % indent)
            else:
                body = self._gen_expr(arm.body, indent + "    ")
                self._emit("%s    %s = %s;" % (indent, result, body))
                self._emit("%s    %s = 1;" % (indent, matched))
            self._emit("%s}" % indent)
            self._scopes.pop()
            guard_ok = guard_ok  # (kept for clarity; no-op)
        self._emit("%sif (!%s) k_error(\"No pattern matched the value "
                   "%%s\", k_format(%s));" % (indent, matched, scrut))
        return result

    def _expr_AwaitExpr(self, expr, indent):
        # 4.0.0 async is VM-only (see _stmt_FuncDecl). Without this
        # the generic "cannot compile" fallback would fire; this
        # message says what to do instead.
        self.fail(expr, "'await' is VM-only -- native mode cannot run "
                  "async code (run with `kabir run` instead)")

    def _expr_FuncDecl(self, expr, indent):
        # 4.0.0 async is VM-only (see _stmt_FuncDecl).
        if getattr(expr, "is_async", False):
            self.fail(expr, "async func is VM-only -- native mode "
                      "cannot run async code (run with `kabir run` "
                      "instead)")
        cname = self.func_c[id(expr)]
        display = expr.name or "<anonymous>"
        return "k_make_func(%s, %s, %d)" % (
            cname, _c_str(display), len(expr.params))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def transpile(source, filename="<stdin>"):
    """Transpile Kabir *source* to C99 source text.

    Raises KabirError (with file/line) for parse errors and for
    constructs outside the native subset (closures, import, ...).
    """
    lines = source.split("\n")
    program = _parse_source(source, filename)
    global_names = []
    for stmt in program.statements:
        if isinstance(stmt, ast.LetStmt):
            global_names.append(stmt.name)
        elif isinstance(stmt, ast.FuncDecl) and stmt.name is not None:
            global_names.append(stmt.name)
    _check_functions(program, set(global_names), filename, lines)
    gen = _CodeGen(program, filename, lines)
    return gen.generate()


def _find_cc():
    for candidate in ("cc", "gcc", "clang"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def compile_native(source, filename="<stdin>", out_path=None):
    """Transpile *source* and compile it to a native binary.

    Returns the binary path (str). Raises KabirError if no C compiler
    is found or if compilation fails (message includes cc's stderr).
    The generated C is kept in a temporary directory and removed
    afterwards; use transpile() to inspect it.
    """
    cc = _find_cc()
    if cc is None:
        raise KabirError(
            "Cannot compile native code -- no C compiler found "
            "('cc', 'gcc' or 'clang'). Install gcc to use native mode.")
    c_source = transpile(source, filename)
    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix="kabir-native-")
        os.close(fd)
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kabir-native-") as tmp:
        c_path = os.path.join(tmp, "main.c")
        with open(c_path, "w", encoding="utf-8") as handle:
            handle.write(c_source)
        cmd = [cc, "-std=c99", "-O2", "-I" + _NATIVE_DIR,
               c_path, _RT_C, "-lm", "-o", out_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise KabirError(
                "Native compilation failed:\n%s"
                % (proc.stderr.strip() or proc.stdout.strip()
                   or "unknown cc error"))
    return out_path


def run_native(source, filename="<stdin>", input_text=""):
    """Compile *source* to a temp binary, run it, return the result.

    Returns (exit_code, stdout, stderr). A non-zero exit code means
    the program hit a runtime error (message in stderr); a KabirError
    is raised only for transpile/compile failures (no compiler, bad
    C) -- exactly like compile_native.
    """
    with tempfile.TemporaryDirectory(prefix="kabir-native-run-") as tmp:
        binary = os.path.join(tmp, "program")
        compile_native(source, filename, binary)
        proc = subprocess.run([binary], input=input_text,
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout, proc.stderr
