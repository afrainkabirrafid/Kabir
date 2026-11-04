"""Tree-walking interpreter for Kabir.

Walks the AST produced by kabir.parser and runs it directly --
no dynamic code runs, no transpiling. Lexical scoping via chained
Environment objects; closures capture their defining environment.
"""

from . import ast_nodes as ast
from .builtins import build_builtins, BuiltinFunction, _type_name
from .errors import KabirError, error_at
from .parser import parse as _parse_source


class _ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class Environment:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent

    def define(self, name, value):
        self.vars[name] = value

    def get(self, name):
        env = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        return None, False

    def lookup(self, name):
        env = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        return None

    def has(self, name):
        env = self
        while env is not None:
            if name in env.vars:
                return True
            env = env.parent
        return False

    def assign(self, name, value):
        env = self
        while env is not None:
            if name in env.vars:
                env.vars[name] = value
                return True
            env = env.parent
        return False


class KabirFunction:
    """A user-defined Kabir function value (a closure)."""

    def __init__(self, name, params, body, closure_env):
        self.name = name or "<anonymous>"
        self.params = params
        self.body = body
        self.closure_env = closure_env

    def __repr__(self):
        return "<function %s>" % self.name


class Interpreter:
    MAX_CALL_DEPTH = 500

    def __init__(self, source="", filename="<stdin>"):
        self.source = source
        self.filename = filename
        self.lines = source.split("\n") if source else []
        self.globals = Environment()
        for name, fn in build_builtins().items():
            self.globals.define(name, fn)
        self.function_depth = 0
        self.loop_depth = 0
        self.call_depth = 0

    # -- errors --------------------------------------------------------
    def error(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        raise error_at(message, self.filename, self.lines, line, col)

    # -- formatting / truthiness / equality ----------------------------
    def format_value(self, value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return repr(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "[" + ", ".join(self.format_value(v) for v in value) + "]"
        if isinstance(value, dict):
            inner = ", ".join('"%s": %s' % (k, self.format_value(v))
                              for k, v in value.items())
            return "{" + inner + "}"
        if isinstance(value, KabirFunction):
            return "<function %s>" % value.name
        if isinstance(value, BuiltinFunction):
            return "<builtin function %s>" % value.name
        return str(value)

    @staticmethod
    def is_truthy(value):
        if value is None or value is False:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value != 0
        if isinstance(value, (str, list, dict)):
            return len(value) > 0
        return True

    def kabir_equal(self, left, right):
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) \
                and left == right
        if left is None or right is None:
            return left is None and right is None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, str) and isinstance(right, str):
            return left == right
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                self.kabir_equal(a, b) for a, b in zip(left, right))
        if isinstance(left, dict) and isinstance(right, dict):
            return (set(left.keys()) == set(right.keys())
                    and all(self.kabir_equal(left[k], right[k])
                            for k in left))
        if isinstance(left, (KabirFunction, BuiltinFunction)) \
                or isinstance(right, (KabirFunction, BuiltinFunction)):
            return left is right
        return False

    # -- running -------------------------------------------------------
    def run(self, program):
        """Run a whole program. Returns None."""
        for stmt in program.statements:
            self.run_stmt(stmt)

    def run_source(self, source, filename="<stdin>"):
        """Lex, parse and run source text. Used by the CLI and REPL."""
        self.source = source
        self.filename = filename
        self.lines = source.split("\n")
        program = _parse_source(source, filename)
        self.run(program)
        return program

    def run_stmt(self, stmt):
        method = getattr(self, "_run_" + type(stmt).__name__, None)
        if method is None:
            self.error(stmt, "Cannot run this statement")
        return method(stmt)

    def compute(self, expr):
        method = getattr(self, "_compute_" + type(expr).__name__, None)
        if method is None:
            self.error(expr, "Cannot compute this expression")
        return method(expr)

    # -- statements ----------------------------------------------------
    def _run_LetStmt(self, stmt):
        value = self.compute(stmt.value)
        self._current_env().define(stmt.name, value)
        return None

    def _run_AssignStmt(self, stmt):
        value = self.compute(stmt.value)
        if stmt.op in ("+=", "-="):
            current = self.compute(stmt.target)
            op = "+" if stmt.op == "+=" else "-"
            value = self._binary(op, current, value, stmt)
        if isinstance(stmt.target, ast.Var):
            if not self._current_env().assign(stmt.target.name, value):
                self.error(stmt.target,
                           "Cannot assign to '%s' -- it was never defined. "
                           "Use 'let %s = ...' first"
                           % (stmt.target.name, stmt.target.name))
        else:  # Index target
            self._assign_index(stmt.target, value, stmt)
        return None

    def _assign_index(self, target, value, stmt):
        container = self.compute(target.target)
        index = self.compute(target.index)
        if isinstance(container, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self.error(target.index,
                           "An array index must be a whole number, not %s"
                           % _type_name(index))
            if index < 0:
                index += len(container)
            if index < 0 or index >= len(container):
                self.error(target.index,
                           "Array index %d is out of range -- the array "
                           "has %d item%s"
                           % (index, len(container),
                              "" if len(container) == 1 else "s"))
            container[index] = value
        elif isinstance(container, dict):
            if not isinstance(index, str):
                self.error(target.index,
                           "A dict key must be a string, not %s"
                           % _type_name(index))
            container[index] = value
        else:
            self.error(target.target,
                       "Cannot assign into %s -- only arrays and dicts "
                       "support index assignment" % _type_name(container))

    def _run_ExprStmt(self, stmt):
        return self.compute(stmt.expr)

    def _run_FuncDecl(self, stmt):
        fn = KabirFunction(stmt.name, stmt.params, stmt.body,
                           self._current_env())
        if stmt.name is not None:
            self._current_env().define(stmt.name, fn)
        return fn

    def _run_ReturnStmt(self, stmt):
        if self.function_depth == 0:
            self.error(stmt, "'return' can only be used inside a function")
        value = self.compute(stmt.value) if stmt.value is not None else None
        raise _ReturnSignal(value)

    def _run_IfStmt(self, stmt):
        for condition, body in stmt.branches:
            if self.is_truthy(self.compute(condition)):
                self.run_block(body)
                return None
        if stmt.else_body is not None:
            self.run_block(stmt.else_body)
        return None

    def _run_WhileStmt(self, stmt):
        self.loop_depth += 1
        try:
            while self.is_truthy(self.compute(stmt.condition)):
                try:
                    self.run_block(stmt.body)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    break
        finally:
            self.loop_depth -= 1
        return None

    def _run_ForStmt(self, stmt):
        iterable = self.compute(stmt.iterable)
        if isinstance(iterable, str):
            items = list(iterable)
        elif isinstance(iterable, list):
            items = list(iterable)
        elif isinstance(iterable, dict):
            items = list(iterable.keys())
        else:
            self.error(stmt.iterable,
                       "Cannot loop over %s -- 'for' needs an array, a "
                       "string, or a range" % _type_name(iterable))
        self.loop_depth += 1
        try:
            for item in items:
                env = Environment(self._current_env())
                env.define(stmt.var, item)
                prev = self._current_env()
                self._push_env(env)
                try:
                    self.run_block(stmt.body)
                except _ContinueSignal:
                    pass
                except _BreakSignal:
                    break
                finally:
                    self._pop_env(prev)
        finally:
            self.loop_depth -= 1
        return None

    def _run_BreakStmt(self, stmt):
        if self.loop_depth == 0:
            self.error(stmt, "'break' can only be used inside a loop")
        raise _BreakSignal()

    def _run_ContinueStmt(self, stmt):
        if self.loop_depth == 0:
            self.error(stmt, "'continue' can only be used inside a loop")
        raise _ContinueSignal()

    def _run_Block(self, stmt):
        self.run_block(stmt)
        return None

    # -- blocks / environments -----------------------------------------
    def _env_stack(self):
        if not hasattr(self, "_stack"):
            self._stack = [self.globals]
        return self._stack

    def _current_env(self):
        return self._env_stack()[-1]

    def _push_env(self, env):
        self._env_stack().append(env)

    def _pop_env(self, prev):
        stack = self._env_stack()
        while stack and stack[-1] is not prev:
            stack.pop()

    def run_block(self, block, env=None):
        if env is None:
            env = Environment(self._current_env())
        prev = self._current_env()
        self._push_env(env)
        try:
            for stmt in block.statements:
                self.run_stmt(stmt)
        finally:
            self._pop_env(prev)

    # -- expressions ---------------------------------------------------
    def _compute_Literal(self, expr):
        return expr.value

    def _compute_StringLiteral(self, expr):
        out = []
        for kind, text in expr.parts:
            if kind == "lit":
                out.append(text)
            else:
                value = self._compute_interpolation(text, expr)
                out.append(self.format_value(value))
        return "".join(out)

    def _compute_interpolation(self, text, node):
        try:
            program = _parse_source(text, self.filename)
        except KabirError as err:
            # Re-point the error at the string's own line.
            raise KabirError(err.message, filename=self.filename,
                             line=node.line, col=node.col,
                             source_line=self.lines[node.line - 1]
                             if self.lines and node.line
                             and node.line <= len(self.lines) else None)
        if len(program.statements) != 1 \
                or not isinstance(program.statements[0], ast.ExprStmt):
            self.error(node, "Only one expression is allowed inside { ... }")
        inner = program.statements[0].expr
        self._reline(inner, node.line, node.col)
        return self.compute(inner)

    @staticmethod
    def _reline(node, line, col):
        node.line, node.col = line, col
        for value in vars(node).values():
            if isinstance(value, ast.Node):
                Interpreter._reline(value, line, col)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.Node):
                        Interpreter._reline(item, line, col)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, ast.Node):
                                Interpreter._reline(part, line, col)

    def _compute_Var(self, expr):
        if self._current_env().has(expr.name):
            return self._current_env().lookup(expr.name)
        self.error(expr, "Undefined variable '%s' -- it was never defined "
                   "with 'let'. Check the spelling" % expr.name)

    def _compute_ArrayLiteral(self, expr):
        return [self.compute(el) for el in expr.elements]

    def _compute_DictLiteral(self, expr):
        result = {}
        for key, value_node in expr.pairs:
            result[key] = self.compute(value_node)
        return result

    def _compute_BinaryOp(self, expr):
        if expr.op == "and":
            left = self.compute(expr.left)
            return self.compute(expr.right) if self.is_truthy(left) else left
        if expr.op == "or":
            left = self.compute(expr.left)
            return left if self.is_truthy(left) else self.compute(expr.right)
        left = self.compute(expr.left)
        right = self.compute(expr.right)
        return self._binary(expr.op, left, right, expr)

    def _binary(self, op, left, right, node):
        if op == "==":
            return self.kabir_equal(left, right)
        if op == "!=":
            return not self.kabir_equal(left, right)
        if op in ("<", "<=", ">", ">="):
            if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                    and not isinstance(left, bool) and not isinstance(right, bool):
                pass
            elif isinstance(left, str) and isinstance(right, str):
                pass
            else:
                self.error(node, "Cannot compare %s with %s using '%s' -- "
                           "compare numbers with numbers, or strings with "
                           "strings" % (_type_name(left), _type_name(right), op))
            return {"<": left < right, "<=": left <= right,
                    ">": left > right, ">=": left >= right}[op]
        if op == "+":
            if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                    and not isinstance(left, bool) and not isinstance(right, bool):
                return left + right
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            self.error(node, "Cannot add %s and %s -- '+' works on two "
                       "numbers, two strings, or two arrays"
                       % (_type_name(left), _type_name(right)))
        if op in ("-", "*", "/", "%", "**"):
            if not (isinstance(left, (int, float)) and not isinstance(left, bool)) \
                    or not (isinstance(right, (int, float)) and not isinstance(right, bool)):
                self.error(node, "'%s' needs two numbers, but got %s and %s"
                           % (op, _type_name(left), _type_name(right)))
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                if right == 0:
                    self.error(node, "Division by zero -- the number after "
                               "'/' cannot be 0")
                if isinstance(left, int) and isinstance(right, int) \
                        and left % right == 0:
                    return left // right
                return left / right
            if op == "%":
                if right == 0:
                    self.error(node, "Division by zero -- the number after "
                               "'%' cannot be 0")
                return left % right
            if op == "**":
                try:
                    return left ** right
                except (OverflowError, ZeroDivisionError):
                    self.error(node, "That power is too large to calculate")
        self.error(node, "Unknown operator %r" % op)

    def _compute_UnaryOp(self, expr):
        if expr.op == "not":
            return not self.is_truthy(self.compute(expr.operand))
        value = self.compute(expr.operand)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.error(expr.operand, "Cannot negate %s -- '-' needs a number"
                       % _type_name(value))
        return -value

    def _compute_Call(self, expr):
        callee = self.compute(expr.callee)
        args = [self.compute(a) for a in expr.args]
        return self.call(callee, args, expr)

    def call(self, callee, args, node):
        if isinstance(callee, BuiltinFunction):
            return callee.impl(self, node, args)
        if isinstance(callee, KabirFunction):
            if len(args) != len(callee.params):
                self.error(node, "Function '%s' takes %d argument%s, but "
                           "got %d" % (callee.name, len(callee.params),
                                       "" if len(callee.params) == 1 else "s",
                                       len(args)))
            if self.call_depth >= self.MAX_CALL_DEPTH:
                self.error(node, "Too many nested function calls -- check "
                           "for infinite recursion")
            env = Environment(callee.closure_env)
            for name, value in zip(callee.params, args):
                env.define(name, value)
            self.call_depth += 1
            self.function_depth += 1
            try:
                self.run_block(callee.body, env=env)
            except _ReturnSignal as ret:
                return ret.value
            finally:
                self.call_depth -= 1
                self.function_depth -= 1
            return None
        name = getattr(callee, "name", None)
        if isinstance(callee, str):
            self.error(node, "'%s' is a string, not a function -- check "
                       "that you are calling the right name" % callee)
        self.error(node, "This value (%s) is not a function, so it cannot "
                   "be called" % _type_name(callee))

    def _compute_Index(self, expr):
        target = self.compute(expr.target)
        index = self.compute(expr.index)
        if isinstance(target, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self.error(expr.index, "An array index must be a whole "
                           "number, not %s" % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self.error(expr.index, "Array index %d is out of range -- "
                           "the array has %d item%s"
                           % (index, len(target),
                              "" if len(target) == 1 else "s"))
            return target[idx]
        if isinstance(target, str):
            if not isinstance(index, int) or isinstance(index, bool):
                self.error(expr.index, "A string index must be a whole "
                           "number, not %s" % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self.error(expr.index, "String index %d is out of range -- "
                           "the string has %d character%s"
                           % (index, len(target),
                              "" if len(target) == 1 else "s"))
            return target[idx]
        if isinstance(target, dict):
            if not isinstance(index, str):
                self.error(expr.index, "A dict key must be a string, not %s"
                           % _type_name(index))
            if index not in target:
                self.error(expr.index, "No key %r in this dict -- available "
                           "keys: %s" % (index, ", ".join(
                               '"%s"' % k for k in target.keys()) or "none"))
            return target[index]
        self.error(expr.target, "Cannot index %s -- only arrays, strings "
                   "and dicts support [ ... ]" % _type_name(target))

    def _compute_FuncDecl(self, expr):
        # Anonymous function expression: func(a, b) { ... }
        return KabirFunction(expr.name, expr.params, expr.body,
                             self._current_env())
