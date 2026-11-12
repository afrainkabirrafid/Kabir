"""Bytecode compiler for Kabir 2.0.0.

Parses Kabir source (or takes an AST program) and emits the code-dict
format from ``.planning/DESIGN-2.0.0.md`` section 4::

    {"name": str, "params": [str],
     "instrs": [[op, arg], ...], "consts": [...],
     "lines": [int, ...], "filename": str}

``instrs[i]`` is ``[op, arg]``; ``lines[i]`` is the source line of
that instruction. ``consts`` holds only ``int | float | str | bool |
None`` plus nested code dicts as ``{"__code__": <code dict>}``.
This module does NOT run the type checker -- the caller runs
check-then-compile.

Compile-time errors (``break``/``continue`` outside a loop,
``return`` outside a function) raise ``KabirError`` with the
interpreter's exact messages.
"""

import json

from . import ast_nodes as ast
from .errors import KabirError, error_at
from .parser import parse as _parse_source

MAGIC = "KBC1"
VERSION = 1

_BINOP = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "**": "POW", "==": "EQ", "!=": "NE", "<": "LT", "<=": "LE",
    ">": "GT", ">=": "GE",
}


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

    def __init__(self, name, params, filename, lines, function_depth=0):
        self.name = name
        self.params = list(params)
        self.filename = filename
        self.lines = lines or []
        self.function_depth = function_depth
        self.instrs = []
        self.consts = []
        self.instr_lines = []
        self.scope_depth = 0      # ENTER_SCOPEs currently open, lexically
        self.loop_stack = []      # innermost loop last
        self._temp_counter = 0
        self._last_line = 1

    # -- low-level -----------------------------------------------------
    def emit(self, op, arg, line):
        self.instrs.append([op, arg])
        self.instr_lines.append(line or 1)
        self._last_line = line or self._last_line
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

    def finish(self):
        self.emit("LOAD_NULL", None, self._last_line)
        self.emit("RETURN", None, self._last_line)
        return {"name": self.name, "params": self.params,
                "instrs": self.instrs, "consts": self.consts,
                "lines": self.instr_lines, "filename": self.filename}

    # -- scopes --------------------------------------------------------
    def _enter_scope(self, line):
        self.emit("ENTER_SCOPE", None, line)
        self.scope_depth += 1

    def _exit_scope(self, line, track=True):
        self.emit("EXIT_SCOPE", None, line)
        if track:
            self.scope_depth -= 1

    def compile_block(self, block):
        """A Block runs in a fresh child env (like interpreter.run_block)."""
        self._enter_scope(block.line)
        for stmt in block.statements:
            self.compile_stmt(stmt)
        self._exit_scope(getattr(block, "end_line", None) or block.line)

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
        self.emit("DEFINE_VAR", stmt.name, stmt.line)

    def _stmt_AssignStmt(self, stmt):
        if isinstance(stmt.target, ast.Var):
            if stmt.op == "=":
                self.compile_expr(stmt.value)
            else:
                self.compile_expr(stmt.target)
                self.compile_expr(stmt.value)
                self.emit("ADD" if stmt.op == "+=" else "SUB", None,
                          stmt.line)
            self.emit("STORE_VAR", stmt.target.name, stmt.line)
            return
        # Index target: container[index] (=|+=|-=) value
        target = stmt.target
        if stmt.op == "=":
            self.compile_expr(target.target)
            self.compile_expr(target.index)
            self.compile_expr(stmt.value)
            self.emit("SET_INDEX", None, stmt.line)
            self.emit("POP", None, stmt.line)
            return
        # Compound: read current, apply op, stash in a hidden temp,
        # then re-evaluate container/index (the interpreter evaluates
        # them twice too) and store. Hidden "$tmpN" names cannot be
        # written by user code, so they never collide or leak visibly.
        self.compile_expr(target.target)
        self.compile_expr(target.index)
        self.emit("GET_INDEX", None, stmt.line)
        self.compile_expr(stmt.value)
        self.emit("ADD" if stmt.op == "+=" else "SUB", None, stmt.line)
        temp = self._fresh_temp()
        self.emit("DEFINE_VAR", temp, stmt.line)
        self.compile_expr(target.target)
        self.compile_expr(target.index)
        self.emit("LOAD_VAR", temp, stmt.line)
        self.emit("SET_INDEX", None, stmt.line)
        self.emit("POP", None, stmt.line)

    def _stmt_ExprStmt(self, stmt):
        self.compile_expr(stmt.expr)
        self.emit("POP", None, stmt.line)

    def _stmt_FuncDecl(self, stmt):
        idx = self._compile_function(stmt)
        self.emit("MAKE_FUNCTION", idx, stmt.line)
        if stmt.name is not None:
            self.emit("DEFINE_VAR", stmt.name, stmt.line)
        else:
            self.emit("POP", None, stmt.line)

    def _stmt_ReturnStmt(self, stmt):
        if self.function_depth == 0:
            self.fail(stmt, "'return' can only be used inside a function")
        if stmt.value is not None:
            self.compile_expr(stmt.value)
        else:
            self.emit("LOAD_NULL", None, stmt.line)
        self.emit("RETURN", None, stmt.line)

    def _stmt_IfStmt(self, stmt):
        end_patches = []
        for condition, body in stmt.branches:
            self.compile_expr(condition)
            jump_false = self.emit("JUMP_IF_FALSE", None, stmt.line)
            self.compile_block(body)
            end_patches.append(self.emit("JUMP", None, stmt.line))
            self.patch(jump_false, len(self.instrs))
        if stmt.else_body is not None:
            self.compile_block(stmt.else_body)
        end = len(self.instrs)
        for index in end_patches:
            self.patch(index, end)

    def _stmt_WhileStmt(self, stmt):
        cond_start = len(self.instrs)
        self.compile_expr(stmt.condition)
        jump_false = self.emit("JUMP_IF_FALSE", None, stmt.line)
        loop = {"kind": "while", "base": self.scope_depth,
                "breaks": [], "continue_target": cond_start,
                "has_iter": False}
        self.loop_stack.append(loop)
        self.compile_block(stmt.body)
        self.loop_stack.pop()
        self.emit("JUMP", cond_start, stmt.line)
        end = len(self.instrs)
        self.patch(jump_false, end)
        for index in loop["breaks"]:
            self.patch(index, end)

    def _stmt_ForStmt(self, stmt):
        self.compile_expr(stmt.iterable)
        self.emit("GET_ITER", None, stmt.line)
        loop_start = len(self.instrs)
        for_iter = self.emit("FOR_ITER", None, stmt.line)
        loop = {"kind": "for", "base": self.scope_depth,
                "breaks": [], "continue_target": loop_start,
                "has_iter": True}
        self.loop_stack.append(loop)
        # Per-iteration env holding the loop variable.
        self._enter_scope(stmt.line)
        self.emit("DEFINE_VAR", stmt.var, stmt.line)
        self.compile_block(stmt.body)
        self._exit_scope(stmt.line)
        self.loop_stack.pop()
        self.emit("JUMP", loop_start, stmt.line)
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
            self.emit("EXIT_SCOPE", None, stmt.line)
        if kind == "break":
            if loop["has_iter"]:
                self.emit("POP", None, stmt.line)
            loop["breaks"].append(self.emit("JUMP", None, stmt.line))
        else:
            self.emit("JUMP", loop["continue_target"], stmt.line)

    def _stmt_BreakStmt(self, stmt):
        self._jump_out_of_loop(stmt, "break")

    def _stmt_ContinueStmt(self, stmt):
        self._jump_out_of_loop(stmt, "continue")

    def _stmt_ImportStmt(self, stmt):
        self.emit("IMPORT", stmt.path, stmt.line)

    def _stmt_Block(self, stmt):
        self.compile_block(stmt)

    # -- functions -----------------------------------------------------
    def _compile_function(self, node):
        builder = _CodeBuilder(node.name or "<anonymous>", node.params,
                               self.filename, self.lines, function_depth=1)
        builder.compile_statements(node.body.statements)
        code = builder.finish()
        return self.add_const({"__code__": code})

    # -- expressions ---------------------------------------------------
    def compile_expr(self, expr):
        method = getattr(self, "_expr_" + type(expr).__name__, None)
        if method is None:
            self.fail(expr, "Cannot compile this expression")
        method(expr)

    def _emit_literal(self, value, line):
        if value is None:
            self.emit("LOAD_NULL", None, line)
        elif value is True:
            self.emit("LOAD_TRUE", None, line)
        elif value is False:
            self.emit("LOAD_FALSE", None, line)
        else:
            self.emit("LOAD_CONST", self.add_const(value), line)

    def _expr_Literal(self, expr):
        self._emit_literal(expr.value, expr.line)

    def _expr_StringLiteral(self, expr):
        if all(kind == "lit" for kind, _text in expr.parts):
            self.emit("LOAD_CONST",
                      self.add_const("".join(t for _k, t in expr.parts)),
                      expr.line)
            return
        for kind, text in expr.parts:
            if kind == "lit":
                self.emit("LOAD_CONST", self.add_const(text), expr.line)
            else:
                self.compile_expr(self._parse_interpolation(text, expr))
        self.emit("BUILD_STRING", len(expr.parts), expr.line)

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
        self.emit("LOAD_VAR", expr.name, expr.line)

    def _expr_ArrayLiteral(self, expr):
        for element in expr.elements:
            self.compile_expr(element)
        self.emit("BUILD_LIST", len(expr.elements), expr.line)

    def _expr_DictLiteral(self, expr):
        for key, value_node in expr.pairs:
            self.emit("LOAD_CONST", self.add_const(key), expr.line)
            self.compile_expr(value_node)
        self.emit("BUILD_DICT", len(expr.pairs), expr.line)

    def _expr_BinaryOp(self, expr):
        if expr.op == "and":
            self.compile_expr(expr.left)
            self.emit("DUP", None, expr.line)
            jump = self.emit("JUMP_IF_FALSE", None, expr.line)
            self.emit("POP", None, expr.line)
            self.compile_expr(expr.right)
            self.patch(jump, len(self.instrs))
            return
        if expr.op == "or":
            self.compile_expr(expr.left)
            self.emit("DUP", None, expr.line)
            jump = self.emit("JUMP_IF_TRUE", None, expr.line)
            self.emit("POP", None, expr.line)
            self.compile_expr(expr.right)
            self.patch(jump, len(self.instrs))
            return
        self.compile_expr(expr.left)
        self.compile_expr(expr.right)
        self.emit(_BINOP[expr.op], None, expr.line)

    def _expr_UnaryOp(self, expr):
        self.compile_expr(expr.operand)
        self.emit("NEG" if expr.op == "-" else "NOT", None, expr.line)

    def _expr_Call(self, expr):
        self.compile_expr(expr.callee)
        for arg in expr.args:
            self.compile_expr(arg)
        self.emit("CALL", len(expr.args), expr.line)

    def _expr_Index(self, expr):
        self.compile_expr(expr.target)
        self.compile_expr(expr.index)
        self.emit("GET_INDEX", None, expr.line)

    def _expr_Member(self, expr):
        self.compile_expr(expr.object)
        self.emit("GET_MEMBER", expr.name, expr.line)

    def _expr_MatchExpr(self, expr):
        self.compile_expr(expr.scrutinee)
        end_patches = []
        for arm in expr.arms:
            # Each arm runs in its own scope so a binding lives only
            # for that arm's guard/body (like the interpreter) and
            # never leaks. The scope is exited once on both the
            # success path and the failure path; the compiler's
            # scope counter is adjusted once, on the success path.
            self._enter_scope(arm.line)
            fail_jump = None
            if isinstance(arm.pattern, ast.Literal):
                self.emit("DUP", None, arm.line)
                self._emit_literal(arm.pattern.value, arm.line)
                self.emit("EQ", None, arm.line)
                fail_jump = self.emit("JUMP_IF_FALSE", None, arm.line)
            elif isinstance(arm.pattern, ast.Var):
                self.emit("DUP", None, arm.line)
                # DEFINE_VAR (not STORE_VAR): the binding is a fresh
                # name in this arm's scope; STORE_VAR would error on
                # an undefined name. See the report for this deviation.
                self.emit("DEFINE_VAR", arm.pattern.name, arm.line)
            elif isinstance(arm.pattern, ast.Wildcard):
                pass
            else:
                self.fail(arm.pattern, "Invalid pattern in 'match'")
            if arm.guard is not None:
                self.compile_expr(arm.guard)
                guard_jump = self.emit("JUMP_IF_FALSE", None, arm.line)
            else:
                guard_jump = None
            # Success: drop the scrutinee, run the body, leave the
            # arm's scope, jump to the end of the match.
            self.emit("POP", None, arm.line)
            self.compile_expr(arm.body)
            self._exit_scope(arm.line)  # counter adjusted here
            end_patches.append(self.emit("JUMP", None, arm.line))
            # Failure stub: exit the arm's scope (counter already
            # adjusted) and fall through to the next arm.
            fail_stub = len(self.instrs)
            self.emit("EXIT_SCOPE", None, arm.line)
            if fail_jump is not None:
                self.patch(fail_jump, fail_stub)
            if guard_jump is not None:
                self.patch(guard_jump, fail_stub)
        # No arm matched: drop the scrutinee and raise.
        self.emit("MATCH_FAIL", None, expr.line)
        end = len(self.instrs)
        for index in end_patches:
            self.patch(index, end)

    def _expr_FuncDecl(self, expr):
        idx = self._compile_function(expr)
        self.emit("MAKE_FUNCTION", idx, expr.line)


# -- public API ---------------------------------------------------------

def compile_program(program, filename, lines):
    """Compile an already-parsed program to a code dict."""
    builder = _CodeBuilder("<main>", [], filename, lines, function_depth=0)
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
    if payload.get("version") != VERSION:
        raise KabirError("Invalid bytecode file %r -- unsupported version "
                         "%r (expected %d)"
                         % (path, payload.get("version"), VERSION))
    if not isinstance(payload.get("main"), dict):
        raise KabirError("Invalid bytecode file %r -- missing main code"
                         % path)
    return payload["main"]
