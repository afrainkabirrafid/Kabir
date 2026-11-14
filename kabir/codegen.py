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
              values, closures (7.0.0: captured variables live in
              arena slots shared with the defining function, so a
              returned / stored / passed closure keeps working and
              `let mut` captures stay mutable), all builtins (print,
              input, len, str, num, type, range, push, pop, sqrt,
              pow, abs, keys, int, float) and all stdlib functions
              (randint, randrange, choice, shuffle, seed, floor,
              ceil, sin, cos, tan, log, exp, read_file, write_file,
              file_exists, json_parse, json_stringify, http_get,
              http_post, now, sleep)
  modules     import "path.kbr" (7.0.0): the imported module is
              transpiled too, its top-level code runs once at the
              first import, and its bindings are copied into the
              importer (re-imports only copy). Bare-name package
              imports (`import greet`, 7.0.0) resolve the same way
              as the VM, via the shared resolver in kabir/imports.py
              (project Kabir.toml + Kabir.lock ->
              vendor/<name>/<version>/src/main.kbr). Import cycles
              and missing files are clear compile-time errors.
  NOT supported (clear error at transpile time):
              - async/await and spawn/chan/send/recv (VM-only)

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
    check_program first, exactly like the VM path does. The CLI
    passes that checked program in (``transpile(..., program=...)``)
    so checker annotations on the AST survive into codegen.

Public API:
  transpile(source, filename="<stdin>", program=None) -> str  (C source)
  compile_native(source, filename, out_path, program=None) -> path (binary path)
  run_native(source, filename="<stdin>", input_text="") ->
      (exit_code, stdout, stderr)
"""

import hashlib
import os
import shutil
import subprocess
import tempfile

from . import ast_nodes as ast
from .builtins import BUILTINS
from .errors import KabirError, error_at
from .imports import resolve_import_spec
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
# Module loading: imports resolve to real files at transpile time.
# ----------------------------------------------------------------------

class _ModuleInfo:
    """One loaded module; the main program is index 0."""

    def __init__(self, index, filename, source, program):
        self.index = index
        self.filename = filename
        self.source = source
        self.program = program
        self.lines = source.split("\n")
        # Direct top-level `let` / named `func` names, in order.
        self.own_names = []
        for stmt in program.statements:
            if isinstance(stmt, ast.LetStmt):
                self.own_names.append(stmt.name)
            elif isinstance(stmt, ast.FuncDecl) and stmt.name is not None:
                self.own_names.append(stmt.name)
        # Kabir name -> C static (own names + names this module
        # imported; one static per (module, name) -- an import copies
        # into it, a later `let` of the same name overwrites it,
        # exactly like the VM's define-into-one-env).
        self.global_c = {}
        # Exported names, in order: own names first, then names this
        # module re-exports from its own imports.
        self.exports = []
        # (ImportStmt, module index) in first-encounter order.
        self.imports = []


def _iter_import_stmts(program):
    """Every ImportStmt in *program*, in source order (including
    imports inside functions and blocks -- they are real imports)."""
    out = []

    def walk(node):
        if node is None:
            return
        if isinstance(node, ast.ImportStmt):
            out.append(node)
            return
        if isinstance(node, ast.Node):
            for value in vars(node).values():
                if isinstance(value, ast.Node):
                    walk(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, ast.Node):
                            walk(item)
                        elif isinstance(item, tuple):
                            for part in item:
                                if isinstance(part, ast.Node):
                                    walk(part)

    for stmt in program.statements:
        walk(stmt)
    return out


def _resolve_bare_import(name, stmt, lines, filename):
    """Resolve a bare-name package import (``import greet``).

    The ONE wiring point for native package imports (7.0.0):
    the shared resolver in kabir/imports.py turns the name into
    the vendored entry file
    (``vendor/<name>/<version>/src/main.kbr``) via the project's
    Kabir.toml [dependencies] + Kabir.lock. Do not build a second
    resolver here. Returns the canonical absolute path. A
    resolution failure is reported at the ``import`` statement.
    """
    try:
        return resolve_import_spec(name, filename)
    except KabirError as exc:
        # exc.message, not str(exc): str renders with a default
        # "<stdin>" location that would leak into this error.
        raise error_at(exc.message, filename, lines, stmt.line, stmt.col)


def _load_module_tree(source, filename, program=None):
    """Parse *source* and every module it (transitively) imports.

    Returns a list of _ModuleInfo, main program first (index 0),
    each import in first-encounter order. Raises KabirError at the
    import statement for a missing file, an import cycle, or an
    unreadable module -- the same messages the VM path reports.

    *program* (7.0.0) is an already-parsed -- and already-checked --
    program for the top module: it is used as-is instead of being
    re-parsed, so checker annotations on the AST (a deref-write's
    resolved_target) survive into codegen. Imported modules are
    always parsed fresh (they were not checked).
    """
    modules = []
    by_canonical = {}
    visiting = []

    def fail_import(stmt, lines, fname, message):
        raise error_at(message, fname, lines, stmt.line, stmt.col)

    def load(src, fname):
        if program is not None and fname == filename:
            parsed = program
        else:
            parsed = _parse_source(src, fname)
        info = _ModuleInfo(len(modules), fname, src, parsed)
        modules.append(info)
        canonical = None
        if not fname.startswith("<"):
            canonical = os.path.realpath(os.path.abspath(fname))
            by_canonical[canonical] = info.index
            visiting.append(canonical)
        try:
            for stmt in _iter_import_stmts(parsed):
                path = stmt.path
                if not path.endswith(".kbr"):
                    # Bare-name package import: the shared resolver
                    # (kabir/imports.py) turns the name into the
                    # vendored entry file; use its result, not a
                    # file-relative path.
                    resolved = _resolve_bare_import(
                        path, stmt, info.lines, fname)
                else:
                    if fname.startswith("<"):
                        base = os.getcwd()
                    else:
                        base = os.path.dirname(os.path.abspath(fname))
                    resolved = os.path.realpath(
                        os.path.abspath(os.path.join(base, path)))
                if resolved in visiting:
                    chain = [os.path.basename(p) for p in visiting]
                    chain.append(os.path.basename(resolved))
                    fail_import(stmt, info.lines, fname,
                                "Import cycle detected: %s"
                                % " -> ".join(chain))
                if resolved in by_canonical:
                    info.imports.append((stmt, by_canonical[resolved]))
                    continue
                try:
                    with open(resolved, "r", encoding="utf-8") as handle:
                        sub_src = handle.read()
                except FileNotFoundError:
                    fail_import(stmt, info.lines, fname,
                                'Cannot import "%s" -- no such file'
                                % path)
                except UnicodeDecodeError:
                    fail_import(stmt, info.lines, fname,
                                'Cannot import "%s" -- the file is not '
                                'valid UTF-8' % path)
                except OSError as exc:
                    fail_import(stmt, info.lines, fname,
                                'Cannot import "%s" -- %s' % (path, exc))
                sub_idx = load(sub_src, resolved)
                info.imports.append((stmt, sub_idx))
        finally:
            if canonical is not None:
                visiting.pop()
        return info.index

    load(source, filename)
    # Exports + globals, in dependency order: a module's exports are
    # its own names plus everything its imports export, so compute
    # each module only after its imports (memoized post-order --
    # plain reverse index order is wrong for diamond imports, where
    # a later importer can have a HIGHER index than the module it
    # shares with an earlier importer). Cycles cannot occur here:
    # load() already rejected them.
    done = set()

    def compute_exports(idx):
        info = modules[idx]
        if idx in done:
            return info.exports
        done.add(idx)
        names = list(info.own_names)
        for _stmt, sub_idx in info.imports:
            for name in compute_exports(sub_idx):
                if name not in names:
                    names.append(name)
        info.exports = names
        for name in names:
            info.global_c[name] = "g_m%d_k_%s" % (
                info.index, _c_ident(name))
        return names

    for info in modules:
        compute_exports(info.index)
    return modules


# ----------------------------------------------------------------------
# Static analysis: free variables, captured slots, undefined names.
#
# A "scope key" names one lexical scope, mirroring the scopes the
# code generator pushes: ("params", id(func)) for parameters,
# ("block", id(block)) for a block, ("for", id(for)) for a for-loop
# variable + body, ("arm", id(arm)) for a match arm, ("top", module)
# for a module top level. A captured definition is identified by
# (scope key, name): two `let`s of the same name in different blocks
# get different slots (each loop iteration / block execution gets a
# fresh cell, like the VM's fresh env), while two `let`s of the same
# name in the SAME block share one slot (the VM's define overwrites
# the same env entry). A slot is declared at function entry, so a
# closure created before its captured `let` executes still holds the
# right slot -- calling it that early is a clean runtime error,
# exactly like the VM's undefined-variable error.
# ----------------------------------------------------------------------

def _collect_node(node, scope_key, ctx, import_exports):
    """Walk *node* without descending into nested FuncDecl bodies.

    ctx: {"refs": set of names read/assigned, "defs": {scope_key:
    set(names)}, "nested": [FuncDecl directly contained],
    "filename": str}. String interpolations are parsed and walked.
    """
    if node is None:
        return
    if isinstance(node, ast.FuncDecl):
        if node.name is not None:
            ctx["defs"].setdefault(scope_key, set()).add(node.name)
        ctx["nested"].append(node)
        return
    if isinstance(node, ast.LetStmt):
        _collect_node(node.value, scope_key, ctx, import_exports)
        ctx["defs"].setdefault(scope_key, set()).add(node.name)
        return
    if isinstance(node, ast.ForStmt):
        _collect_node(node.iterable, scope_key, ctx, import_exports)
        for_key = ("for", id(node))
        ctx["defs"].setdefault(for_key, set()).add(node.var)
        for stmt in node.body.statements:
            _collect_node(stmt, for_key, ctx, import_exports)
        return
    if isinstance(node, ast.MatchExpr):
        _collect_node(node.scrutinee, scope_key, ctx, import_exports)
        for arm in node.arms:
            arm_key = ("arm", id(arm))
            if isinstance(arm.pattern, ast.Var):
                ctx["defs"].setdefault(arm_key, set()).add(
                    arm.pattern.name)
            if arm.guard is not None:
                _collect_node(arm.guard, arm_key, ctx, import_exports)
            _collect_node(arm.body, arm_key, ctx, import_exports)
        return
    if isinstance(node, ast.Block):
        block_key = ("block", id(node))
        for stmt in node.statements:
            _collect_node(stmt, block_key, ctx, import_exports)
        return
    if isinstance(node, ast.StringLiteral):
        for kind, text in node.parts:
            if kind != "expr":
                continue
            try:
                inner = _parse_source(text, ctx["filename"])
            except Exception:
                continue
            for stmt in inner.statements:
                _collect_node(stmt, scope_key, ctx, import_exports)
        return
    if isinstance(node, ast.Var):
        ctx["refs"].add(node.name)
        return
    if isinstance(node, ast.AssignStmt):
        if isinstance(node.target, ast.Var):
            ctx["refs"].add(node.target.name)
        else:
            _collect_node(node.target, scope_key, ctx, import_exports)
        _collect_node(node.value, scope_key, ctx, import_exports)
        return
    if isinstance(node, ast.ImportStmt):
        for name in import_exports.get(id(node), ()):
            ctx["defs"].setdefault(scope_key, set()).add(name)
        return
    if isinstance(node, ast.Node):
        for value in vars(node).values():
            if isinstance(value, ast.Node):
                _collect_node(value, scope_key, ctx, import_exports)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, ast.Node):
                        _collect_node(item, scope_key, ctx, import_exports)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, ast.Node):
                                _collect_node(part, scope_key, ctx,
                                              import_exports)


def _analyze_function(func, statements, module_globals, enclosing_cells,
                      filename, lines, import_exports, all_infos,
                      top_key=None):
    """Analyse one function (or a module top level, func=None).

    *enclosing_cells* is the set of names bound in enclosing lexical
    scopes (capturable); *module_globals* are read directly, never
    captured. Returns (info, env_free): env_free is the set of names
    this function reads from enclosing scopes (the closure
    environment, in sorted order in info["free"]); info["slots"] is
    the set of (scope key, name) definitions this function must keep
    in arena slots because a nested function reads them. Raises
    KabirError for an undefined variable, with the same message the
    old checker-style check used.
    """
    ctx = {"refs": set(), "defs": {}, "nested": [], "filename": filename}
    if func is not None:
        pkey = ("params", id(func))
        ctx["defs"].setdefault(pkey, set()).update(func.params)
        for stmt in func.body.statements:
            _collect_node(stmt, ("block", id(func.body)), ctx,
                          import_exports)
    else:
        for stmt in statements:
            _collect_node(stmt, top_key, ctx, import_exports)
    own_bound = set()
    for names in ctx["defs"].values():
        own_bound |= names
    nested_free = set()
    # Names a nested function may capture are the ones bound in an
    # enclosing lexical scope. A module top level binds its globals
    # (read directly, never captured) plus its block-level names
    # (capturable, like any function's locals).
    if func is None:
        child_cells = enclosing_cells | (own_bound - module_globals)
    else:
        child_cells = enclosing_cells | own_bound
    for child in ctx["nested"]:
        _child_info, child_free = _analyze_function(
            child, None, module_globals, child_cells,
            filename, lines, import_exports, all_infos)
        nested_free |= child_free
    # Names read here or by a nested function, minus this function's
    # own bindings and builtins. A name bound in an enclosing scope
    # is a capture (even when a module global of the same name
    # exists -- the local shadows it); a name that is only a module
    # global is read directly; anything else is undefined.
    free_raw = ((ctx["refs"] | nested_free) - own_bound
                - _BUILTIN_NAMES)
    env_free = free_raw & enclosing_cells
    undefined = sorted(free_raw - enclosing_cells - module_globals)
    # Undefined names are only raised here inside functions. At a
    # module top level they are left for code generation, which
    # reports the same "Undefined variable" error when it resolves
    # the name -- and which must see the statement first: a top-level
    # `spawn(f())` is a VM-only error, not an undefined-`f` error,
    # and the old pass never checked top-level statements either.
    if undefined and func is not None:
        raise error_at(
            "Undefined variable '%s' -- it was never defined with "
            "'let'. Check the spelling" % undefined[0],
            filename, lines,
            func.line if func is not None else 1,
            func.col if func is not None else 1)
    cell_names = nested_free & own_bound
    slots = set()
    for scope_key, names in ctx["defs"].items():
        for name in names:
            if name in cell_names:
                slots.add((scope_key, name))
    info = {"free": sorted(env_free), "slots": slots}
    if func is not None:
        all_infos[id(func)] = info
    return info, env_free


def _analyze_modules(modules):
    """Analyse every module. Returns (all_infos, top_infos):
    all_infos maps id(FuncDecl) -> info, top_infos[i] is module i's
    top-level info. Import bindings count as defined names."""
    all_infos = {}
    top_infos = []
    import_exports = {}
    for module in modules:
        for stmt, sub_idx in module.imports:
            import_exports[id(stmt)] = modules[sub_idx].exports
    for module in modules:
        top_info, _free = _analyze_function(
            None, module.program.statements, set(module.global_c),
            set(), module.filename, module.lines, import_exports,
            all_infos, top_key=("top", module.index))
        top_infos.append(top_info)
    return all_infos, top_infos


class _CodeGen:
    """Transpiles one parsed program to C99."""

    def __init__(self, modules, all_infos, top_infos):
        self.modules = modules
        self._infos_by_id = all_infos
        self._top_infos = top_infos
        self._import_map = {}
        for module in modules:
            for stmt, sub_idx in module.imports:
                self._import_map[id(stmt)] = sub_idx
        # Hoist every function (anywhere, in any module) to a
        # top-level C function.
        self.funcs = []  # (FuncDecl node, c name, module index)
        self.func_c = {}
        self.func_module = {}
        for module in modules:
            # Interpolation parsing during collection needs the
            # module's own filename.
            self.filename = module.filename
            self.lines = module.lines
            self._collect_funcs(module.program.statements, module.index)
        self._scopes = [{}]      # name -> C variable, innermost last
        self._cell_scopes = [{}]  # name -> C slot var, same indexing
        # (scope key, name) -> C slot var for the function / module
        # top currently being generated.
        self._slot_vars = {}
        self._func_info = None
        self._env_index = {}     # free name -> env index, current fn
        self._cur_module = 0
        self._top_level = False
        self._top_depth = 0
        self._counter = 0
        self._out = []            # lines of the current C function
        self._loop_depth = 0
        self._function_depth = 0

    # -- collection ------------------------------------------------------
    def _collect_funcs(self, statements, module_index):
        for stmt in statements:
            self._collect_from_node(stmt, module_index)

    def _collect_from_node(self, node, module_index):
        if node is None:
            return
        if isinstance(node, ast.FuncDecl):
            cname = "kabir_fn_%d" % len(self.funcs)
            self.funcs.append((node, cname, module_index))
            self.func_c[id(node)] = cname
            self.func_module[id(node)] = module_index
            # Do not descend into the body here: nested functions are
            # found when their own containing function is generated.
            # (They are still collected: generate() walks every
            # hoisted function body for further nested FuncDecls.)
            self._collect_from_node_body(node, module_index)
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
                    self._collect_from_node(stmt, module_index)
            return
        if isinstance(node, ast.Node):
            for value in vars(node).values():
                if isinstance(value, ast.Node):
                    self._collect_from_node(value, module_index)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, ast.Node):
                            self._collect_from_node(item, module_index)
                        elif isinstance(item, tuple):
                            for part in item:
                                if isinstance(part, ast.Node):
                                    self._collect_from_node(part,
                                                            module_index)

    def _collect_from_node_body(self, func, module_index):
        """Collect FuncDecls nested inside *func*'s body."""
        for stmt in func.body.statements:
            self._collect_from_node(stmt, module_index)

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
        kind, value = self._lookup(node, name)
        if kind == "cell":
            return "k_slot_get(%s, %s)" % (value, _c_str(name))
        if kind == "env":
            return "k_env_get(%d, %s)" % (value, _c_str(name))
        if kind == "builtin":
            return "k_builtin(%s)" % _c_str(name)
        return value

    def _lookup(self, node, name):
        """(kind, value) for *name*: ("plain", C var), ("cell", slot
        var), ("env", env index), ("global", C static), ("builtin",
        name). Lexical order: innermost scope (cell before plain),
        then the enclosing functions' captures, then the current
        module's globals, then builtins."""
        for i in range(len(self._scopes) - 1, -1, -1):
            if name in self._cell_scopes[i]:
                return ("cell", self._cell_scopes[i][name])
            if name in self._scopes[i]:
                return ("plain", self._scopes[i][name])
        if name in self._env_index:
            return ("env", self._env_index[name])
        module = self.modules[self._cur_module]
        if name in module.global_c:
            return ("global", module.global_c[name])
        if name in _BUILTIN_NAMES:
            return ("builtin", name)
        self.fail(node, "Undefined variable '%s' -- it was never defined "
                  "with 'let'. Check the spelling" % name)

    def _cell_expr(self, node, name):
        """C expression for the slot holding captured *name* (for a
        closure value being created here): the slot itself when the
        definition is a local of this function, k_env_slot(i) when
        this function itself captured it from further out."""
        for i in range(len(self._scopes) - 1, -1, -1):
            if name in self._cell_scopes[i]:
                return self._cell_scopes[i][name]
            if name in self._scopes[i]:
                # A plain local can never be captured: the analysis
                # makes every captured definition a cell.
                self.fail(node, "Undefined variable '%s' -- it was never "
                          "defined with 'let'. Check the spelling" % name)
        if name in self._env_index:
            return "k_env_slot(%d)" % self._env_index[name]
        self.fail(node, "Undefined variable '%s' -- it was never defined "
                  "with 'let'. Check the spelling" % name)

    def _func_value(self, node, indent):
        """C expression for a FuncDecl's value: plain function, or a
        closure carrying its captured slots (7.0.0)."""
        # 4.0.0 async is VM-only (see _stmt_FuncDecl).
        if getattr(node, "is_async", False):
            self.fail(node, "async func is VM-only -- native mode "
                      "cannot run async code (run with `kabir run` "
                      "instead)")
        cname = self.func_c[id(node)]
        info = self._infos_by_id[id(node)]
        display = node.name or "<anonymous>"
        if not info["free"]:
            return "k_make_func(%s, %s, %d)" % (
                cname, _c_str(display), len(node.params))
        cells = [self._cell_expr(node, name) for name in info["free"]]
        return "k_make_closure(%s, %s, %d, (KSlot[]){%s}, %d)" % (
            cname, _c_str(display), len(node.params),
            ", ".join(cells), len(cells))

    def _define_local(self, name, value, indent):
        """Define *name* in the current scope from C expr *value*:
        a captured definition (re)creates its slot's cell; any other
        definition is a plain C local."""
        if name in self._cell_scopes[-1]:
            slot = self._cell_scopes[-1][name]
            self._emit("%s*%s = k_new_cell(%s);" % (indent, slot, value))
        else:
            var = self._fresh("v")
            self._scopes[-1][name] = var
            self._emit("%sKVal %s = %s;" % (indent, var, value))

    def _store_var(self, node, name, value, op, indent):
        """Emit a store to variable *name* (plain local, module
        global, captured cell slot, or enclosing capture). *value*
        is an already-generated C expression; *op* is "=" | "+=" |
        "-=". Used for plain assignment and for deref writes
        (`*s = v`, where the borrow IS the value at runtime)."""
        kind, target = self._lookup(node, name)
        if kind in ("plain", "global"):
            if op == "=":
                self._emit("%s%s = %s;" % (indent, target, value))
            else:
                fn = "k_add" if op == "+=" else "k_sub"
                self._emit("%s%s = %s(%s, %s);"
                           % (indent, target, fn, target, value))
        elif kind == "cell":
            if op == "=":
                self._emit("%sk_slot_set(%s, %s, %s);"
                           % (indent, target, _c_str(name), value))
            else:
                fn = "k_add" if op == "+=" else "k_sub"
                self._emit("%sk_slot_set(%s, %s, %s(%s, %s));"
                           % (indent, target, _c_str(name), fn,
                             "k_slot_get(%s, %s)"
                             % (target, _c_str(name)), value))
        elif kind == "env":
            if op == "=":
                self._emit("%sk_env_set(%d, %s, %s);"
                           % (indent, target, _c_str(name), value))
            else:
                fn = "k_add" if op == "+=" else "k_sub"
                self._emit("%sk_env_set(%d, %s, %s(%s, %s));"
                           % (indent, target, _c_str(name), fn,
                             "k_env_get(%d, %s)"
                             % (target, _c_str(name)), value))
        else:  # builtin: assignment to a builtin is an error
            self.fail(node, "Cannot assign to '%s' -- it was never "
                      "defined. Use 'let %s = ...' first" % (name, name))

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
    def _push_scope(self, key):
        """Push a lexical scope (plain vars + captured slots).
        Returns the scope's cell dict (name -> slot var). The caller
        emits `KSlot <var> = k_new_slot();` for each slot right
        where the scope is entered, so every execution of the scope
        (each loop iteration, each call) gets fresh slots -- a
        closure created in one iteration never sees the next
        iteration's cell, exactly like the VM's fresh env."""
        cell = {}
        for (scope_key, name), var in self._slot_vars.items():
            if scope_key == key:
                cell[name] = var
        self._scopes.append({})
        self._cell_scopes.append(cell)
        return cell

    def _emit_slot_decls(self, cell, indent):
        for var in cell.values():
            self._emit("%sKSlot %s = k_new_slot();" % (indent, var))

    def _pop_scope(self):
        self._scopes.pop()
        self._cell_scopes.pop()

    def generate(self):
        parts = []
        parts.append("/* Generated by Kabir native codegen (3.0.0 Wave 1).")
        parts.append("   Memory model: native (arena) -- see rt.h. */")
        parts.append('#include "rt.h"')
        parts.append("")
        # Forward declarations for every hoisted function, and for
        # every imported module's init function (called at import
        # points, which may sit in a function defined above it).
        for _node, cname, _mod in self.funcs:
            parts.append("static KVal %s(KVal *args, int nargs);" % cname)
        for module in self.modules[1:]:
            parts.append("static void kabir_mod_%d_init(void);"
                         % module.index)
        if self.funcs or len(self.modules) > 1:
            parts.append("")
        # Globals, namespaced per module (g_m<module>_k_<name>).
        any_globals = False
        for module in self.modules:
            for _name, cvar in module.global_c.items():
                parts.append("static KVal %s;" % cvar)
                any_globals = True
        if any_globals:
            parts.append("")
        # Function definitions.
        for node, cname, _mod in self.funcs:
            parts.append(self._gen_function(node, cname))
            parts.append("")
        # Imported modules' init functions (guarded, run once).
        for module in self.modules[1:]:
            parts.append(self._gen_module_init(module.index))
            parts.append("")
        # main().
        self._cur_module = 0
        self._func_info = self._top_infos[0]
        self._env_index = {}
        self.filename = self.modules[0].filename
        self.lines = self.modules[0].lines
        self._scopes = [{}]
        self._cell_scopes = [{}]
        self._counter = 0
        self._out = []
        self._loop_depth = 0
        self._function_depth = 0
        self._top_level = False
        self._emit("int main(void) {")
        self._emit("    k_rt_init();")
        self._gen_top_level_stmts(
            0, self.modules[0].program.statements, "    ")
        self._emit("    k_rt_cleanup();")
        self._emit("    return 0;")
        self._emit("}")
        parts.append("\n".join(self._out))
        return "\n".join(parts) + "\n"

    def _gen_module_init(self, module_idx):
        """Generate `kabir_mod_<idx>_init()`: the module's top-level
        code as a C function, guarded so it runs once no matter how
        many times the module is imported. Called at every import
        point, so the top-level code runs in execution order (an
        import inside a function runs it when the function runs, not
        when the program starts)."""
        module = self.modules[module_idx]
        self._cur_module = module_idx
        self._func_info = self._top_infos[module_idx]
        self._env_index = {}
        self.filename = module.filename
        self.lines = module.lines
        self._scopes = [{}]
        self._cell_scopes = [{}]
        self._counter = 0
        self._out = []
        self._slot_vars = {}
        for (scope_key, name) in sorted(
                self._top_infos[module_idx]["slots"], key=repr):
            self._slot_vars[(scope_key, name)] = self._fresh("slot")
        self._loop_depth = 0
        self._function_depth = 0
        self._top_level = True
        self._top_depth = 1
        self._emit("static void kabir_mod_%d_init(void) {" % module_idx)
        self._emit("    static int done = 0;")
        self._emit("    if (done) return;")
        self._emit("    done = 1;")
        for stmt in module.program.statements:
            self._gen_stmt(stmt, "    ")
        self._emit("}")
        return "\n".join(self._out)

    def _gen_top_level_stmts(self, module_idx, statements, indent):
        """Emit the main module's top-level statements inline in
        main(). Direct lets/funcs assign the module's globals;
        function/loop depth is reset, so `return` / `break` at this
        level fail exactly as they do at the real top level.
        (Imported modules use _gen_module_init instead.)"""
        module = self.modules[module_idx]
        saved = (self._cur_module, self._func_info, self._env_index,
                 self._slot_vars, self._top_level, self._top_depth,
                 self._function_depth, self._loop_depth,
                 self.filename, self.lines)
        self._cur_module = module_idx
        self._func_info = self._top_infos[module_idx]
        self._env_index = {}
        self.filename = module.filename
        self.lines = module.lines
        self._slot_vars = {}
        for (scope_key, name) in sorted(
                self._top_infos[module_idx]["slots"], key=repr):
            self._slot_vars[(scope_key, name)] = self._fresh("slot")
        self._top_level = True
        self._top_depth = len(self._scopes)
        self._function_depth = 0
        self._loop_depth = 0
        for stmt in statements:
            self._gen_stmt(stmt, indent)
        (self._cur_module, self._func_info, self._env_index,
         self._slot_vars, self._top_level, self._top_depth,
         self._function_depth, self._loop_depth,
         self.filename, self.lines) = saved

    def _gen_function(self, node, cname):
        self._cur_module = self.func_module[id(node)]
        module = self.modules[self._cur_module]
        self.filename = module.filename
        self.lines = module.lines
        info = self._infos_by_id[id(node)]
        self._func_info = info
        self._env_index = {name: i for i, name in enumerate(info["free"])}
        self._scopes = [{}]
        self._cell_scopes = [{}]
        self._counter = 0
        self._out = []
        self._slot_vars = {}
        for (scope_key, name) in sorted(info["slots"], key=repr):
            self._slot_vars[(scope_key, name)] = self._fresh("slot")
        self._loop_depth = 0
        self._function_depth = 1
        self._top_level = False
        self._emit("static KVal %s(KVal *args, int nargs) {" % cname)
        self._emit("    (void)nargs; /* arity is checked in k_call */")
        for (scope_key, _name), var in self._slot_vars.items():
            if scope_key[0] == "params":
                self._emit("    KSlot %s = k_new_slot();" % var)
        for i, param in enumerate(node.params):
            pkey = ("params", id(node))
            if (pkey, param) in self._slot_vars:
                slot = self._slot_vars[(pkey, param)]
                self._cell_scopes[0][param] = slot
                self._emit("    *%s = k_new_cell(args[%d]);" % (slot, i))
            else:
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
        cell = self._push_scope(("block", id(block)))
        self._emit_slot_decls(cell, indent)
        for stmt in block.statements:
            self._gen_stmt(stmt, indent)
        self._pop_scope()

    def _gen_stmt(self, stmt, indent):
        method = getattr(self, "_stmt_" + type(stmt).__name__, None)
        if method is None:
            self.fail(stmt, "Cannot compile this statement in native "
                      "mode yet")
        method(stmt, indent)

    def _stmt_LetStmt(self, stmt, indent):
        value = self._gen_expr(stmt.value, indent)
        if self._top_level and len(self._scopes) == self._top_depth:
            # Top-level let: assign the module global (declared up front).
            module = self.modules[self._cur_module]
            self._emit("%s%s = %s;" % (indent, module.global_c[stmt.name],
                                       value))
        else:
            self._define_local(stmt.name, value, indent)

    def _stmt_AssignStmt(self, stmt, indent):
        # 7.0.0: writing through a deref (`*s = 10`) stores to the
        # place the borrow actually borrows (resolved_target, set by
        # the checker) -- or, without it, to the variable holding
        # the borrow. A borrow IS the value at runtime, so either
        # way this is a plain store.
        if isinstance(stmt.target, ast.DerefExpr):
            if not isinstance(stmt.target.operand, ast.Var):
                self.fail(stmt.target,
                          "Can only write through a borrow held in "
                          "a variable")
            value = self._gen_expr(stmt.value, indent)
            resolved = getattr(stmt, "resolved_target", None)
            if isinstance(resolved, ast.Var):
                self._store_var(stmt.target, resolved.name,
                                value, stmt.op, indent)
                return
            if isinstance(resolved, ast.Index):
                container = self._gen_expr(resolved.target, indent)
                index = self._gen_expr(resolved.index, indent)
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
                container2 = self._gen_expr(resolved.target, indent)
                index2 = self._gen_expr(resolved.index, indent)
                self._emit("%sk_set_index(%s, %s, %s);"
                           % (indent, container2, index2, new))
                return
            self._store_var(stmt.target, stmt.target.operand.name,
                            value, stmt.op, indent)
            return
        if isinstance(stmt.target, ast.Var):
            value = self._gen_expr(stmt.value, indent)
            self._store_var(stmt.target, stmt.target.name,
                            value, stmt.op, indent)
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
        value = self._func_value(stmt, indent)
        if stmt.name is None:
            self._emit("%s(void)(%s);" % (indent, value))
            return
        if self._top_level and len(self._scopes) == self._top_depth:
            module = self.modules[self._cur_module]
            self._emit("%s%s = %s;" % (indent, module.global_c[stmt.name],
                                       value))
        else:
            self._define_local(stmt.name, value, indent)

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
        cell = self._push_scope(("for", id(stmt)))
        self._emit_slot_decls(cell, indent + "    ")
        if stmt.var in self._cell_scopes[-1]:
            slot = self._cell_scopes[-1][stmt.var]
            self._emit("%s    *%s = k_new_cell(k_array_get(%s, %s));"
                       % (indent, slot, items, idx))
        else:
            var = self._fresh("v")
            self._scopes[-1][stmt.var] = var
            self._emit("%s    KVal %s = k_array_get(%s, %s);"
                       % (indent, var, items, idx))
        self._loop_depth += 1
        for inner in stmt.body.statements:
            self._gen_stmt(inner, indent + "    ")
        self._loop_depth -= 1
        self._pop_scope()
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
        sub_idx = self._import_map[id(stmt)]
        src = self.modules[sub_idx]
        importer = self.modules[self._cur_module]
        # Run the module's top-level code once, in execution order:
        # it lives in a guarded init function (see _gen_module_init),
        # because the importing code may be inside a function whose
        # body is generated -- but not run -- before main(). The
        # guard makes re-imports a no-op for the top-level code.
        self._emit("%skabir_mod_%d_init();" % (indent, sub_idx))
        # Copy the module's bindings into the importer (every import
        # copies, like the VM's cache-hit path). At module top level
        # the copy target is the importer's own global; anywhere
        # else it is a fresh local in the current scope, so it stays
        # scoped like the VM.
        for name in src.exports:
            value = src.global_c[name]
            if self._top_level and len(self._scopes) == self._top_depth:
                self._emit("%s%s = %s;"
                           % (indent, importer.global_c[name], value))
            else:
                self._define_local(name, value, indent)

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

    def _expr_DerefExpr(self, expr, indent):
        # 7.0.0 deref: a borrow IS the value at runtime -- `*r`
        # generates just `r`, exactly like `&x`. Zero runtime cost.
        # The checker resolves the read to the place the borrow
        # actually borrows (node.resolved), so a write through a
        # reborrow is visible here; without it the read goes to the
        # variable holding the borrow.
        resolved = getattr(expr, "resolved", None)
        if resolved is not None:
            return self._gen_expr(resolved, indent)
        return self._gen_expr(expr.operand, indent)

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
            cell = self._push_scope(("arm", id(arm)))
            self._emit_slot_decls(cell, indent)
            guard_ok = True
            if isinstance(arm.pattern, ast.Literal):
                lit = self._gen_expr(arm.pattern, indent)
                self._emit("%sif (!%s && k_eq(%s, %s)) {"
                           % (indent, matched, scrut, lit))
            elif isinstance(arm.pattern, ast.Var):
                if arm.pattern.name in self._cell_scopes[-1]:
                    slot = self._cell_scopes[-1][arm.pattern.name]
                    self._emit("%sif (!%s) {" % (indent, matched))
                    self._emit("%s    *%s = k_new_cell(%s);"
                               % (indent, slot, scrut))
                else:
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
            self._pop_scope()
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
        return self._func_value(expr, indent)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def transpile(source, filename="<stdin>", program=None):
    """Transpile Kabir *source* to C99 source text.

    Loads every module *source* imports, analyses closures and
    undefined names across all of them, then transpiles the whole
    tree to one C file. Raises KabirError (with file/line) for parse
    errors and for constructs outside the native subset (async,
    spawn/chan, bare-name package imports, ...).

    *program* is an already-parsed -- and already-checked -- program
    for the top module (see _load_module_tree); the CLI passes the
    program it checked so checker annotations survive codegen.
    """
    modules = _load_module_tree(source, filename, program=program)
    all_infos, top_infos = _analyze_modules(modules)
    gen = _CodeGen(modules, all_infos, top_infos)
    return gen.generate()


def _find_cc():
    for candidate in ("cc", "gcc", "clang"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def compile_native(source, filename="<stdin>", out_path=None,
                   program=None):
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
    c_source = transpile(source, filename, program=program)
    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix="kabir-native-")
        os.close(fd)
    return _compile_c(c_source, cc, out_path)


def _compile_c(c_source, cc, out_path):
    """Compile already-generated C source *c_source* to *out_path*.

    The runtime (``kabir/native/rt.c``) is compiled once per
    (rt.c content, compiler) and the object file reused from the
    cache (see _cached_rt_object): compiling rt.c took ~1.25 s of
    every ~1.3 s native compile, while the generated program code
    itself compiles in ~0.1 s.
    """
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kabir-native-") as tmp:
        c_path = os.path.join(tmp, "main.c")
        with open(c_path, "w", encoding="utf-8") as handle:
            handle.write(c_source)
        cmd = [cc, "-std=c99", "-O2", "-I" + _NATIVE_DIR,
               c_path, _cached_rt_object(cc), "-lm", "-o", out_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise KabirError(
                "Native compilation failed:\n%s"
                % (proc.stderr.strip() or proc.stdout.strip()
                   or "unknown cc error"))
    return out_path


# ----------------------------------------------------------------------
# Native binary cache (5.0.0 Leap 3)
# ----------------------------------------------------------------------
#
# `kabir run --native` used to pay the full ~1.3 s C compile on
# every run, so it lost ~11x to the VM on short programs. The cache
# below stores the compiled binary under ~/.kabir/cache/native
# (or $KABIR_CACHE_DIR/native) keyed by a fingerprint of EVERYTHING
# that can change the binary: the generated C source (which covers
# the Kabir source and the transpiler), kabir/native/rt.c and rt.h,
# the Kabir version, and the C compiler's identity. A source change,
# a runtime change, or a compiler change all miss the cache and
# recompile; --no-cache (use_cache=False) always recompiles.
# Binaries are written atomically (temp file + os.replace), so a
# crash mid-compile can never leave a half-written binary behind,
# and a stale binary can never be served: the key IS the content.

def _native_cache_dir():
    """Directory holding cached native binaries and rt.o files."""
    override = os.environ.get("KABIR_CACHE_DIR")
    if override:
        return os.path.join(override, "native")
    return os.path.join(os.path.expanduser("~"), ".kabir", "cache",
                        "native")


def _cc_fingerprint(cc):
    """Identify the C compiler: resolved path + version line."""
    try:
        proc = subprocess.run([cc, "--version"], capture_output=True,
                              text=True, timeout=10)
        first = proc.stdout.split("\n")[0].strip() if proc.stdout else ""
    except Exception:
        first = ""
    return "%s|%s" % (cc, first)


def _native_cache_key(c_source, cc):
    """Fingerprint of everything that determines the binary."""
    from . import __version__ as kabir_version
    digest = hashlib.sha256()
    digest.update(b"kabir-native-cache-v1\n")
    digest.update(kabir_version.encode("utf-8"))
    digest.update(b"\n")
    digest.update(_cc_fingerprint(cc).encode("utf-8"))
    digest.update(b"\n")
    for path in (_RT_C, _RT_H):
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError as exc:
            raise KabirError("Cannot compile native code -- cannot read "
                             "%s: %s" % (path, exc))
    digest.update(b"\n")
    digest.update(c_source.encode("utf-8"))
    return digest.hexdigest()


def _cached_rt_object(cc):
    """Compile rt.c once per (rt.c content, compiler); reuse the .o.

    The object file lives in the native cache dir, named by a hash
    of rt.c + the compiler fingerprint, so a runtime or compiler
    change rebuilds it automatically. Written atomically.
    """
    try:
        with open(_RT_C, "rb") as handle:
            rt_hash = hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError as exc:
        raise KabirError("Cannot compile native code -- cannot read "
                         "%s: %s" % (_RT_C, exc))
    cc_hash = hashlib.sha256(
        _cc_fingerprint(cc).encode("utf-8")).hexdigest()[:8]
    cache_dir = _native_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    obj_path = os.path.join(cache_dir, "rt-%s-%s.o" % (rt_hash, cc_hash))
    if os.path.isfile(obj_path):
        return obj_path
    tmp_path = "%s.tmp-%d" % (obj_path, os.getpid())
    proc = subprocess.run([cc, "-std=c99", "-O2", "-c", _RT_C,
                           "-o", tmp_path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise KabirError(
            "Native compilation failed:\n%s"
            % (proc.stderr.strip() or proc.stdout.strip()
               or "unknown cc error"))
    os.replace(tmp_path, obj_path)
    return obj_path


def _cached_binary_ok(cached_path):
    """True if the cached binary matches its stored sha256 sidecar.

    The sidecar is written at compile time; a cached binary whose
    bytes no longer match it (disk corruption, a half-written file
    from a killed older build, tampering) is treated as a miss and
    recompiled instead of ever being executed.
    """
    try:
        with open(cached_path + ".sha256", "r",
                  encoding="utf-8") as handle:
            expected = handle.read().strip()
        digest = hashlib.sha256()
        with open(cached_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected
    except OSError:
        return False


def _store_binary_sidecar(cached_path):
    """Write the sha256 sidecar for a freshly compiled binary."""
    digest = hashlib.sha256()
    with open(cached_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    sidecar = cached_path + ".sha256"
    tmp_sidecar = "%s.tmp-%d" % (sidecar, os.getpid())
    with open(tmp_sidecar, "w", encoding="utf-8") as handle:
        handle.write(digest.hexdigest() + "\n")
    os.replace(tmp_sidecar, sidecar)


def cached_compile_native(source, filename="<stdin>", out_path=None,
                          use_cache=True, program=None):
    """Like compile_native, but reuse a cached binary on repeat runs.

    On a cache hit with no *out_path* (``kabir run --native``), the
    cached binary's path is returned directly -- nothing is copied
    and nothing recompiles. With an *out_path* (``kabir build
    --native``), the cached binary is copied there. On a miss the
    program is compiled (with the cached rt.o) and the result is
    stored in the cache before being returned/copied. A cached
    binary is only trusted when its sha256 sidecar matches its
    bytes; a damaged entry is recompiled, never executed.
    With use_cache=False the cache is bypassed entirely (the
    ``--no-cache`` flag) and compile_native runs instead.
    *program* is an already-parsed, already-checked top-module
    program (see transpile); the cache key comes from the generated
    C, so a program with checker annotations caches separately.
    """
    if not use_cache:
        return compile_native(source, filename, out_path,
                              program=program)
    cc = _find_cc()
    if cc is None:
        raise KabirError(
            "Cannot compile native code -- no C compiler found "
            "('cc', 'gcc' or 'clang'). Install gcc to use native mode.")
    c_source = transpile(source, filename, program=program)
    key = _native_cache_key(c_source, cc)
    cache_dir = _native_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    cached_path = os.path.join(cache_dir, "prog-%s" % key)
    if os.path.isfile(cached_path) and _cached_binary_ok(cached_path):
        if out_path is None:
            return cached_path
        return _copy_cached_binary(cached_path, out_path)
    tmp_path = os.path.join(cache_dir,
                            ".tmp-prog-%s-%d" % (key[:12], os.getpid()))
    try:
        _compile_c(c_source, cc, tmp_path)
        os.replace(tmp_path, cached_path)
        os.chmod(cached_path, 0o755)
        _store_binary_sidecar(cached_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    if out_path is None:
        return cached_path
    return _copy_cached_binary(cached_path, out_path)


def _copy_cached_binary(cached_path, out_path):
    """Copy a cached binary to *out_path* (build --native)."""
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copyfile(cached_path, out_path)
    os.chmod(out_path, 0o755)
    return out_path


def run_native(source, filename="<stdin>", input_text="",
               use_cache=True, program=None):
    """Compile *source* to a native binary (cached), run it, return the result.

    Returns (exit_code, stdout, stderr). A non-zero exit code means
    the program hit a runtime error (message in stderr); a KabirError
    is raised only for transpile/compile failures (no compiler, bad
    C) -- exactly like compile_native. *program* is an
    already-parsed, already-checked top-module program (see
    transpile); passing it keeps checker annotations in the build.
    """
    binary = cached_compile_native(source, filename,
                                   use_cache=use_cache, program=program)
    proc = subprocess.run([binary], input=input_text,
                          capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr
