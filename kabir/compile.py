"""Bytecode compiler for Kabir 2.0.0 (code dict v2, Kabir 3.0.0 Wave 0).

Parses Kabir source (or takes an AST program) and emits the code-dict
format from ``.planning/DESIGN-2.0.0.md`` section 4, extended by
``.planning/DESIGN-3.0.0-4.0.0.md`` Wave 0::

    {"name": str, "params": [str],
     "instrs": [[op, arg], ...], "consts": [...],
     "lines": [int, ...], "cols": [int, ...],
     "locals": [name-or-None, ...], "filename": str,
     "is_async": bool}

``instrs[i]`` is ``[op, arg]``; ``lines[i]`` / ``cols[i]`` are the
source line / column of that instruction. ``is_async`` (4.0.0) marks
code compiled from an ``async func`` body; v1/v2 dicts without the
key are read as ``False``. ``locals[i]`` is the name
occupying slot ``i`` (``None`` for a parameter passed via the env
because it is captured by a nested function). ``consts`` holds only
``int | float | str | bool | None`` plus nested code dicts as
``{"__code__": <code dict>}``.
This module does NOT run the type checker -- the caller runs
check-then-compile.

Slot-allocation rule (Wave 0): a variable is slot-allocated ONLY if
it is defined in the current function (a parameter, or a ``let`` /
function name / ``for`` variable / ``match`` binding) AND it is never
referenced by any nested function (directly or transitively, including
references hidden in string interpolations inside nested functions).
The top level (``<main>``, function_depth 0) never allocates slots:
its bindings are globals, and imported modules export exactly the
bindings in their env, so a slot-allocated module-level name would
silently vanish from the module's exports. Captured / free variables
stay name-based via the env chain on both sides: the defining
function keeps them in its env (DEFINE_VAR / STORE_VAR / LOAD_VAR)
and the nested function reads them the same way. Each ``let`` gets a
fresh slot that is never reused across scopes, so shadowing just
allocates a new slot. The hidden ``$tmp`` temps used for compound
index assignment stay name-based.

Compile-time errors (``break``/``continue`` outside a loop,
``return`` outside a function) raise ``KabirError`` with the
interpreter's exact messages.
"""

import json

from . import ast_nodes as ast
from .errors import KabirError, error_at
from .parser import parse as _parse_source

MAGIC = "KBC1"
VERSION = 2

# .kbc versions the loader accepts (v1: no "cols"/"locals", name-based
# only; v2: slot-based, see the module docstring).
SUPPORTED_VERSIONS = (1, 2)

_BINOP = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "**": "POW", "==": "EQ", "!=": "NE", "<": "LT", "<=": "LE",
    ">": "GT", ">=": "GE",
}


def _captured_names(statements, filename):
    """Names referenced by any nested function in *statements*.

    Walks the AST with an ``in_func`` flag that turns on at every
    ``FuncDecl`` and stays on for its whole subtree (so transitive
    references through deeper nesting count too). Every ``Var`` seen
    while the flag is on is captured. References hidden inside string
    interpolations are found by parsing each ``{ ... }`` part exactly
    as the compiler does later; an unparsable part is ignored here --
    compilation will raise the proper error for it. ``match`` binding
    patterns are ``Var`` nodes too, so a binding that merely shares a
    name with an outer variable conservatively counts as a capture:
    that only costs a slot, never correctness.
    """
    captured = set()
    stack = [(stmt, False) for stmt in statements]
    while stack:
        node, in_func = stack.pop()
        if isinstance(node, ast.FuncDecl):
            in_func = True
        if in_func and isinstance(node, ast.Var):
            captured.add(node.name)
        if isinstance(node, ast.StringLiteral) and in_func:
            for kind, text in node.parts:
                if kind != "expr":
                    continue
                try:
                    program = _parse_source(text, filename)
                except Exception:
                    continue
                for stmt in program.statements:
                    stack.append((stmt, True))
        if isinstance(node, ast.Node):
            children = []
            for value in vars(node).values():
                if isinstance(value, ast.Node):
                    children.append(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, ast.Node):
                            children.append(item)
                        elif isinstance(item, tuple):
                            for part in item:
                                if isinstance(part, ast.Node):
                                    children.append(part)
            for child in children:
                stack.append((child, in_func))
    return captured


def _reline(node, line, col):
    """Point a whole AST subtree at one source position (interpolation)."""
    node.line, node.col = line, col
    for value in vars(node).values():
        if isinstance(value, ast.Node):
            _reline(value, line, col)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.Node):
                    _reline(item, line, col)
                elif isinstance(item, tuple):
                    for part in item:
                        if isinstance(part, ast.Node):
                            _reline(part, line, col)


class _CodeBuilder:
    """Compiles one function body (or the top-level program)."""

    def __init__(self, name, params, filename, lines, function_depth=0,
                 statements=None, is_async=False):
        self.name = name
        self.params = list(params)
        self.filename = filename
        self.lines = lines or []
        self.function_depth = function_depth
        # 4.0.0 async: True while compiling an `async func` body.
        # `await` is only legal here (checked in _expr_AwaitExpr);
        # a nested sync function gets its own builder with False.
        self.is_async = is_async
        self.instrs = []
        self.consts = []
        self.instr_lines = []
        self.instr_cols = []
        # Slot state: self.locals[i] is the name in slot i (None for a
        # captured parameter, which is passed via the env instead).
        # Parameters occupy slots 0..n-1 in order; each later `let`
        # appends a fresh slot, never reused.
        self.locals = []
        self.captured = _captured_names(statements or [], filename)
        # The top level is globals: never slot-allocated (see the
        # module docstring -- imported modules export via their env).
        self._use_slots = function_depth > 0
        # Lexical slot scopes: a stack of {name: slot} dicts, mirroring
        # the ENTER_SCOPE / EXIT_SCOPE nesting. The root scope holds
        # the (slot-allocated) parameters.
        self._slot_scopes = [{}]
        for param in self.params:
            if not self._use_slots or param in self.captured:
                self.locals.append(None)
            else:
                self.locals.append(param)
                self._slot_scopes[0][param] = len(self.locals) - 1
        self.scope_depth = 0      # ENTER_SCOPEs currently open, lexically
        self.loop_stack = []      # innermost loop last
        self._temp_counter = 0
        self._last_line = 1
        self._last_col = 1

    # -- low-level -----------------------------------------------------
    def emit(self, op, arg, line, col=None):
        self.instrs.append([op, arg])
        self.instr_lines.append(line or 1)
        self.instr_cols.append(col or 1)
        self._last_line = line or self._last_line
        self._last_col = col or self._last_col
        return len(self.instrs) - 1

    def add_const(self, value):
        self.consts.append(value)
        return len(self.consts) - 1

    def patch(self, index, target):
        self.instrs[index][1] = target

    def fail(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        raise error_at(message, self.filename, self.lines, line, col)

    def _fresh_temp(self):
        self._temp_counter += 1
        return "$tmp%d" % self._temp_counter

    # -- slots ---------------------------------------------------------
    def _lookup_slot(self, name):
        """Slot index for *name* in the current lexical scope, or None."""
        if not self._use_slots:
            return None
        for scope in reversed(self._slot_scopes):
            if name in scope:
                return scope[name]
        return None

    def _alloc_slot(self, name):
        index = len(self.locals)
        self.locals.append(name)
        self._slot_scopes[-1][name] = index
        return index

    def _define_name(self, name, line, col):
        """Emit DEFINE_SLOT (fresh slot) or DEFINE_VAR (captured name
        or top-level global)."""
        if not self._use_slots or name in self.captured:
            self.emit("DEFINE_VAR", name, line, col)
            return None
        index = self._alloc_slot(name)
        self.emit("DEFINE_SLOT", index, line, col)
        return index

    def _store_name(self, name, line, col):
        """Emit STORE_SLOT if *name* is slot-allocated, else STORE_VAR."""
        slot = self._lookup_slot(name)
        if slot is not None:
            self.emit("STORE_SLOT", slot, line, col)
        else:
            self.emit("STORE_VAR", name, line, col)

    def finish(self):
        self.emit("LOAD_NULL", None, self._last_line, self._last_col)
        self.emit("RETURN", None, self._last_line, self._last_col)
        return {"name": self.name, "params": self.params,
                "instrs": self.instrs, "consts": self.consts,
                "lines": self.instr_lines, "cols": self.instr_cols,
                "locals": self.locals, "filename": self.filename,
                "is_async": self.is_async}

    # -- scopes --------------------------------------------------------
    def _enter_scope(self, line, col=None):
        self.emit("ENTER_SCOPE", None, line, col)
        self.scope_depth += 1

    def _exit_scope(self, line, col=None, track=True):
        self.emit("EXIT_SCOPE", None, line, col)
        if track:
            self.scope_depth -= 1

    def _block_defines_names(self, block):
        """True if *block* directly defines a name into its scope.

        A direct LetStmt / FuncDecl / ImportStmt defines a name, as
        does a compound index assignment (``a[i] += v``), which the
        compiler lowers through a hidden ``$tmp`` DEFINE_VAR. A
        block with no such statement needs no runtime scope at all:
        nothing is defined into it, and a nested block or branch
        gets its own scope when it needs one. 5.0.0: skipping the
        ENTER_SCOPE / EXIT_SCOPE pair for such blocks removes one
        Environment allocation per loop iteration in hot loops.
        """
        for stmt in block.statements:
            if isinstance(stmt, (ast.LetStmt, ast.FuncDecl,
                                 ast.ImportStmt)):
                return True
            if isinstance(stmt, ast.AssignStmt) \
                    and not isinstance(stmt.target, ast.Var) \
                    and stmt.op != "=":
                return True
        return False

    def compile_block(self, block):
        """A Block runs in a fresh child env (like interpreter.run_block).

        5.0.0: the child env (and the matching slot scope) is only
        emitted when the block directly defines a name (see
        _block_defines_names). A block that defines nothing reads
        exactly the same names with or without the empty child env
        -- the env chain and the slot scopes are searched
        innermost-first either way -- and break/continue stay
        consistent because scope_depth is only counted when a scope
        is actually emitted.
        """
        has_defs = self._block_defines_names(block)
        if has_defs:
            self._enter_scope(block.line, block.col)
            self._slot_scopes.append({})
        for stmt in block.statements:
            self.compile_stmt(stmt)
        if has_defs:
            self._slot_scopes.pop()
            self._exit_scope(getattr(block, "end_line", None)
                             or block.line, block.col)

    def compile_statements(self, statements):
        for stmt in statements:
            self.compile_stmt(stmt)

    # -- statements ----------------------------------------------------
    def compile_stmt(self, stmt):
        method = getattr(self, "_stmt_" + type(stmt).__name__, None)
        if method is None:
            self.fail(stmt, "Cannot compile this statement")
        method(stmt)

    def _stmt_LetStmt(self, stmt):
        self.compile_expr(stmt.value)
        self._define_name(stmt.name, stmt.line, stmt.col)

    def _stmt_AssignStmt(self, stmt):
        if isinstance(stmt.target, ast.Var):
            if stmt.op == "=":
                self.compile_expr(stmt.value)
            else:
                self.compile_expr(stmt.target)
                self.compile_expr(stmt.value)
                self.emit("ADD" if stmt.op == "+=" else "SUB", None,
                          stmt.line, stmt.col)
            self._store_name(stmt.target.name, stmt.line, stmt.col)
            return
        # Index target: container[index] (=|+=|-=) value. Index errors
        # are reported at the index's position (like the interpreter).
        target = stmt.target
        if stmt.op == "=":
            self.compile_expr(target.target)
            self.compile_expr(target.index)
            self.compile_expr(stmt.value)
            self.emit("SET_INDEX", None, target.index.line,
                      target.index.col)
            self.emit("POP", None, stmt.line, stmt.col)
            return
        # Compound: read current, apply op, stash in a hidden temp,
        # then re-evaluate container/index (the interpreter evaluates
        # them twice too) and store. Hidden "$tmpN" names cannot be
        # written by user code, so they never collide or leak visibly.
        self.compile_expr(target.target)
        self.compile_expr(target.index)
        self.emit("GET_INDEX", None, target.index.line, target.index.col)
        self.compile_expr(stmt.value)
        self.emit("ADD" if stmt.op == "+=" else "SUB", None, stmt.line, stmt.col)
        temp = self._fresh_temp()
        self.emit("DEFINE_VAR", temp, stmt.line, stmt.col)
        self.compile_expr(target.target)
        self.compile_expr(target.index)
        self.emit("LOAD_VAR", temp, stmt.line, stmt.col)
        self.emit("SET_INDEX", None, target.index.line, target.index.col)
        self.emit("POP", None, stmt.line, stmt.col)

    def _stmt_ExprStmt(self, stmt):
        self.compile_expr(stmt.expr)
        self.emit("POP", None, stmt.line, stmt.col)

    def _stmt_FuncDecl(self, stmt):
        idx = self._compile_function(stmt)
        self.emit("MAKE_FUNCTION", idx, stmt.line, stmt.col)
        if stmt.name is not None:
            self._define_name(stmt.name, stmt.line, stmt.col)
        else:
            self.emit("POP", None, stmt.line, stmt.col)

    def _stmt_ReturnStmt(self, stmt):
        if self.function_depth == 0:
            self.fail(stmt, "'return' can only be used inside a function")
        if stmt.value is not None:
            self.compile_expr(stmt.value)
        else:
            self.emit("LOAD_NULL", None, stmt.line, stmt.col)
        self.emit("RETURN", None, stmt.line, stmt.col)

    def _stmt_IfStmt(self, stmt):
        end_patches = []
        for condition, body in stmt.branches:
            self.compile_expr(condition)
            jump_false = self.emit("JUMP_IF_FALSE", None, stmt.line, stmt.col)
            self.compile_block(body)
            end_patches.append(self.emit("JUMP", None, stmt.line, stmt.col))
            self.patch(jump_false, len(self.instrs))
        if stmt.else_body is not None:
            self.compile_block(stmt.else_body)
        end = len(self.instrs)
        for index in end_patches:
            self.patch(index, end)

    def _stmt_WhileStmt(self, stmt):
        cond_start = len(self.instrs)
        self.compile_expr(stmt.condition)
        jump_false = self.emit("JUMP_IF_FALSE", None, stmt.line, stmt.col)
        loop = {"kind": "while", "base": self.scope_depth,
                "breaks": [], "continue_target": cond_start,
                "has_iter": False}
        self.loop_stack.append(loop)
        self.compile_block(stmt.body)
        self.loop_stack.pop()
        self.emit("JUMP", cond_start, stmt.line, stmt.col)
        end = len(self.instrs)
        self.patch(jump_false, end)
        for index in loop["breaks"]:
            self.patch(index, end)

    def _stmt_ForStmt(self, stmt):
        self.compile_expr(stmt.iterable)
        # The interpreter reports a bad iterable at its own position.
        self.emit("GET_ITER", None, stmt.iterable.line, stmt.iterable.col)
        loop_start = len(self.instrs)
        for_iter = self.emit("FOR_ITER", None, stmt.line, stmt.col)
        loop = {"kind": "for", "base": self.scope_depth,
                "breaks": [], "continue_target": loop_start,
                "has_iter": True}
        self.loop_stack.append(loop)
        # Per-iteration env holding the loop variable (and a slot
        # scope for it, mirroring the env scope).
        self._enter_scope(stmt.line, stmt.col)
        self._slot_scopes.append({})
        self._define_name(stmt.var, stmt.line, stmt.col)
        self.compile_block(stmt.body)
        self._slot_scopes.pop()
        self._exit_scope(stmt.line, stmt.col)
        self.loop_stack.pop()
        self.emit("JUMP", loop_start, stmt.line, stmt.col)
        end = len(self.instrs)
        self.patch(for_iter, end)
        for index in loop["breaks"]:
            self.patch(index, end)

    def _jump_out_of_loop(self, stmt, kind):
        """Shared break/continue: exit scopes, then jump (break patches)."""
        if not self.loop_stack:
            self.fail(stmt, "'%s' can only be used inside a loop" % kind)
        loop = self.loop_stack[-1]
        for _ in range(self.scope_depth - loop["base"]):
            self.emit("EXIT_SCOPE", None, stmt.line, stmt.col)
        if kind == "break":
            if loop["has_iter"]:
                self.emit("POP", None, stmt.line, stmt.col)
            loop["breaks"].append(self.emit("JUMP", None, stmt.line, stmt.col))
        else:
            self.emit("JUMP", loop["continue_target"], stmt.line, stmt.col)

    def _stmt_BreakStmt(self, stmt):
        self._jump_out_of_loop(stmt, "break")

    def _stmt_ContinueStmt(self, stmt):
        self._jump_out_of_loop(stmt, "continue")

    def _stmt_ImportStmt(self, stmt):
        self.emit("IMPORT", stmt.path, stmt.line, stmt.col)

    def _stmt_Block(self, stmt):
        self.compile_block(stmt)

    # -- functions -----------------------------------------------------
    def _compile_function(self, node):
        builder = _CodeBuilder(node.name or "<anonymous>", node.params,
                               self.filename, self.lines, function_depth=1,
                               statements=node.body.statements,
                               is_async=node.is_async)
        builder.compile_statements(node.body.statements)
        code = builder.finish()
        return self.add_const({"__code__": code})

    # -- expressions ---------------------------------------------------
    def compile_expr(self, expr):
        method = getattr(self, "_expr_" + type(expr).__name__, None)
        if method is None:
            self.fail(expr, "Cannot compile this expression")
        method(expr)

    def _emit_literal(self, value, line, col=None):
        if value is None:
            self.emit("LOAD_NULL", None, line, col)
        elif value is True:
            self.emit("LOAD_TRUE", None, line, col)
        elif value is False:
            self.emit("LOAD_FALSE", None, line, col)
        else:
            self.emit("LOAD_CONST", self.add_const(value), line, col)

    def _expr_Literal(self, expr):
        self._emit_literal(expr.value, expr.line, expr.col)

    def _expr_StringLiteral(self, expr):
        if all(kind == "lit" for kind, _text in expr.parts):
            self.emit("LOAD_CONST",
                      self.add_const("".join(t for _k, t in expr.parts)),
                      expr.line, expr.col)
            return
        for kind, text in expr.parts:
            if kind == "lit":
                self.emit("LOAD_CONST", self.add_const(text), expr.line, expr.col)
            else:
                self.compile_expr(self._parse_interpolation(text, expr))
        self.emit("BUILD_STRING", len(expr.parts), expr.line, expr.col)

    def _parse_interpolation(self, text, node):
        """Parse one { ... } part exactly like the interpreter does."""
        try:
            program = _parse_source(text, self.filename)
        except KabirError as err:
            raise KabirError(
                err.message, filename=self.filename,
                line=node.line, col=node.col,
                source_line=self.lines[node.line - 1]
                if self.lines and node.line
                and node.line <= len(self.lines) else None)
        if len(program.statements) != 1 \
                or not isinstance(program.statements[0], ast.ExprStmt):
            self.fail(node, "Only one expression is allowed inside { ... }")
        inner = program.statements[0].expr
        _reline(inner, node.line, node.col)
        return inner

    def _expr_Var(self, expr):
        slot = self._lookup_slot(expr.name)
        if slot is not None:
            self.emit("LOAD_SLOT", slot, expr.line, expr.col)
        else:
            self.emit("LOAD_VAR", expr.name, expr.line, expr.col)

    def _expr_BorrowExpr(self, expr):
        # 4.0.0 borrow checker: a borrow IS the value at runtime --
        # `&x` compiles to just `x`. Zero runtime cost.
        self.compile_expr(expr.expr)

    def _expr_ArrayLiteral(self, expr):
        for element in expr.elements:
            self.compile_expr(element)
        self.emit("BUILD_LIST", len(expr.elements), expr.line, expr.col)

    def _expr_DictLiteral(self, expr):
        for key, value_node in expr.pairs:
            self.emit("LOAD_CONST", self.add_const(key), expr.line, expr.col)
            self.compile_expr(value_node)
        self.emit("BUILD_DICT", len(expr.pairs), expr.line, expr.col)

    def _expr_BinaryOp(self, expr):
        if expr.op == "and":
            self.compile_expr(expr.left)
            self.emit("DUP", None, expr.line, expr.col)
            jump = self.emit("JUMP_IF_FALSE", None, expr.line, expr.col)
            self.emit("POP", None, expr.line, expr.col)
            self.compile_expr(expr.right)
            self.patch(jump, len(self.instrs))
            return
        if expr.op == "or":
            self.compile_expr(expr.left)
            self.emit("DUP", None, expr.line, expr.col)
            jump = self.emit("JUMP_IF_TRUE", None, expr.line, expr.col)
            self.emit("POP", None, expr.line, expr.col)
            self.compile_expr(expr.right)
            self.patch(jump, len(self.instrs))
            return
        self.compile_expr(expr.left)
        self.compile_expr(expr.right)
        self.emit(_BINOP[expr.op], None, expr.line, expr.col)

    def _expr_UnaryOp(self, expr):
        self.compile_expr(expr.operand)
        # The interpreter reports a bad '-' at the operand's position.
        if expr.op == "-":
            self.emit("NEG", None, expr.operand.line, expr.operand.col)
        else:
            self.emit("NOT", None, expr.line, expr.col)

    def _expr_Call(self, expr):
        # 4.0.0 async: `spawn(<call>)` is special. The call must NOT
        # run in the current task -- it becomes a new task -- so the
        # compiler intercepts it here and emits SPAWN_TASK instead of
        # a plain CALL. `spawn(f)` (a bare function value, no call)
        # spawns it with no arguments. Any other shape falls through
        # to the `spawn` builtin, which reports a clear error.
        # Caveat: a user-defined function named `spawn` cannot be
        # called as `spawn(f())` -- the compiler always reads that
        # shape as task creation. No program in the repo defines one.
        callee = expr.callee
        if (isinstance(callee, ast.Var) and callee.name == "spawn"
                and len(expr.args) == 1):
            inner = expr.args[0]
            if isinstance(inner, ast.Call):
                self.compile_expr(inner.callee)
                for arg in inner.args:
                    self.compile_expr(arg)
                self.emit("SPAWN_TASK", len(inner.args),
                          expr.line, expr.col)
                return
            if isinstance(inner, (ast.Var, ast.FuncDecl)):
                self.compile_expr(inner)
                self.emit("SPAWN_TASK", 0, expr.line, expr.col)
                return
        self.compile_expr(expr.callee)
        for arg in expr.args:
            self.compile_expr(arg)
        self.emit("CALL", len(expr.args), expr.line, expr.col)

    def _expr_AwaitExpr(self, expr):
        # `await` only compiles inside an `async func` body. The
        # top level and sync functions fail here, at compile time,
        # with the exact spot -- never silently, never at runtime.
        if not self.is_async:
            self.fail(expr, "'await' can only be used inside an "
                      "'async func' -- this is a sync function (or "
                      "the top level)")
        self.compile_expr(expr.expr)
        self.emit("AWAIT", None, expr.line, expr.col)

    def _expr_Index(self, expr):
        self.compile_expr(expr.target)
        self.compile_expr(expr.index)
        # The interpreter reports index errors at the index's
        # position, so the GET_INDEX instruction carries it.
        self.emit("GET_INDEX", None, expr.index.line, expr.index.col)

    def _expr_Member(self, expr):
        self.compile_expr(expr.object)
        self.emit("GET_MEMBER", expr.name, expr.line, expr.col)

    def _expr_MatchExpr(self, expr):
        self.compile_expr(expr.scrutinee)
        end_patches = []
        for arm in expr.arms:
            # Each arm runs in its own scope so a binding lives only
            # for that arm's guard/body (like the interpreter) and
            # never leaks. The scope is exited once on both the
            # success path and the failure path; the compiler's
            # scope counter is adjusted once, on the success path.
            self._enter_scope(arm.line, arm.col)
            self._slot_scopes.append({})
            fail_jump = None
            if isinstance(arm.pattern, ast.Literal):
                self.emit("DUP", None, arm.line, arm.col)
                self._emit_literal(arm.pattern.value, arm.line, arm.col)
                self.emit("EQ", None, arm.line, arm.col)
                fail_jump = self.emit("JUMP_IF_FALSE", None, arm.line, arm.col)
            elif isinstance(arm.pattern, ast.Var):
                self.emit("DUP", None, arm.line, arm.col)
                # DEFINE (not STORE): the binding is a fresh name in
                # this arm's scope; STORE would error on an undefined
                # name. Slot-allocated unless a nested function in
                # the guard/body captures it.
                self._define_name(arm.pattern.name, arm.line, arm.col)
            elif isinstance(arm.pattern, ast.Wildcard):
                pass
            else:
                self.fail(arm.pattern, "Invalid pattern in 'match'")
            if arm.guard is not None:
                self.compile_expr(arm.guard)
                guard_jump = self.emit("JUMP_IF_FALSE", None, arm.line, arm.col)
            else:
                guard_jump = None
            # Success: drop the scrutinee, run the body, leave the
            # arm's scope, jump to the end of the match.
            self.emit("POP", None, arm.line, arm.col)
            self.compile_expr(arm.body)
            self._exit_scope(arm.line, arm.col)  # counter adjusted here
            self._slot_scopes.pop()
            end_patches.append(self.emit("JUMP", None, arm.line, arm.col))
            # Failure stub: exit the arm's scope (counter already
            # adjusted) and fall through to the next arm.
            fail_stub = len(self.instrs)
            self.emit("EXIT_SCOPE", None, arm.line, arm.col)
            if fail_jump is not None:
                self.patch(fail_jump, fail_stub)
            if guard_jump is not None:
                self.patch(guard_jump, fail_stub)
        # No arm matched: drop the scrutinee and raise.
        self.emit("MATCH_FAIL", None, expr.line, expr.col)
        end = len(self.instrs)
        for index in end_patches:
            self.patch(index, end)

    def _expr_FuncDecl(self, expr):
        idx = self._compile_function(expr)
        self.emit("MAKE_FUNCTION", idx, expr.line, expr.col)


# -- public API ---------------------------------------------------------

def compile_program(program, filename, lines):
    """Compile an already-parsed program to a code dict."""
    builder = _CodeBuilder("<main>", [], filename, lines, function_depth=0,
                           statements=program.statements)
    builder.compile_statements(program.statements)
    return builder.finish()


def compile_source(source, filename="<stdin>"):
    """Parse and compile source text to a code dict."""
    program = _parse_source(source, filename)
    return compile_program(program, filename, source.split("\n"))


def save_bytecode(code, path):
    """Write a code dict to path as a .kbc JSON file."""
    payload = {"magic": MAGIC, "version": VERSION, "main": code}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def load_bytecode(path):
    """Read a .kbc JSON file and return its main code dict."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise KabirError("Cannot read bytecode file %r -- %s" % (path, exc))
    except ValueError:
        raise KabirError("Invalid bytecode file %r -- not valid JSON"
                         % path)
    if not isinstance(payload, dict) or payload.get("magic") != MAGIC:
        raise KabirError("Invalid bytecode file %r -- bad magic (not a "
                         "Kabir .kbc file)" % path)
    if payload.get("version") not in SUPPORTED_VERSIONS:
        raise KabirError("Invalid bytecode file %r -- unsupported version "
                         "%r (expected 1 or 2)"
                         % (path, payload.get("version")))
    if not isinstance(payload.get("main"), dict):
        raise KabirError("Invalid bytecode file %r -- missing main code"
                         % path)
    return payload["main"]
