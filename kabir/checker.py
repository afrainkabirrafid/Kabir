"""Opt-in static type checker for Kabir 2.0.0.

``check_program(program, filename="<stdin>", lines=None)`` walks the AST
and raises ``KabirError`` (with file/line) on the FIRST type error, or
returns ``None`` when the program type-checks.

Types are plain strings: ``"number"``, ``"string"``, ``"bool"``,
``"array"``, ``"dict"``, ``"function"``, ``"any"``, ``"null"``.

The checker is TYPE-ONLY: it never errors on undefined variables,
arity, or runtime errors -- the runtime owns those. Every rule errors
ONLY when the involved types are statically known (i.e. not ``"any"``),
so untyped programs always pass.
"""

from . import ast_nodes as ast
from .errors import KabirError, error_at

VALID_TYPES = frozenset(
    ("number", "string", "bool", "array", "dict", "function", "any", "null")
)

UNKNOWN_TYPE_MSG = (
    'Unknown type "%s" -- use number, string, bool, array, dict, '
    "function, or any"
)

# Builtin / stdlib signatures: name -> return type. Argument checking
# lives in _check_builtin_call / _check_stdlib_call below, because
# several take optional or variadic arguments.
_BUILTIN_RETURNS = {
    "print": "null",
    "input": "string",
    "len": "number",
    "str": "string",
    "num": "number",
    "type": "string",
    "range": "array",
    "push": "array",
    "pop": "any",
    "sqrt": "number",
    "pow": "number",
    "abs": "number",
    "keys": "array",
    "int": "number",
    "float": "number",
}

_STDLIB_RETURNS = {
    "randint": "number",
    "randrange": "number",
    "choice": "any",
    "shuffle": "array",
    "seed": "null",
    "floor": "number",
    "ceil": "number",
    "sin": "number",
    "cos": "number",
    "tan": "number",
    "log": "number",
    "exp": "number",
}

# String methods: name -> (arg-type sets per position, return type).
# An empty arg list means the method takes no arguments.
_STRING_METHODS = {
    "upper": ([], "string"),
    "lower": ([], "string"),
    "strip": ([], "string"),
    "split": ([{"string"}], "array"),   # separator is optional
    "join": ([{"array"}], "string"),
    "contains": ([{"string"}], "bool"),
    "starts_with": ([{"string"}], "bool"),
    "ends_with": ([{"string"}], "bool"),
    "replace": ([{"string"}, {"string"}], "string"),
}


def _literal_type(value):
    """The Kabir type of a literal Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "any"


class _Checker:
    def __init__(self, filename, lines):
        self.filename = filename
        self.lines = lines
        # Parallel stacks: scopes map name -> type, func_scopes map
        # name -> (param types, return type) for known user functions.
        self.scopes = [{}]
        self.func_scopes = [{}]
        # Stack of enclosing functions' return annotations (None when
        # the function has no return annotation, or when not in one).
        self.return_stack = []

    # -- plumbing ------------------------------------------------------

    def _fail(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        raise error_at(message, self.filename, self.lines, line, col)

    def _push_scope(self):
        self.scopes.append({})
        self.func_scopes.append({})

    def _pop_scope(self):
        self.scopes.pop()
        self.func_scopes.pop()

    def _define(self, name, type_):
        self.scopes[-1][name] = type_

    def _lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if name in _BUILTIN_RETURNS or name in _STDLIB_RETURNS:
            return "function"
        if name in ("pi", "e"):
            return "number"
        return "any"

    def _lookup_func(self, name):
        for scope in reversed(self.func_scopes):
            if name in scope:
                return scope[name]
        return None

    def _record_func(self, name, param_types, return_type):
        self.func_scopes[-1][name] = (param_types, return_type)

    def _validate_ann(self, ann):
        """Return the type string for a TypeAnn, or error if unknown."""
        if ann is None:
            return None
        if ann.name not in VALID_TYPES:
            self._fail(ann, UNKNOWN_TYPE_MSG % ann.name)
        return ann.name

    def _ann_types(self, anns):
        """Validate a list of TypeAnn-or-None, keeping None entries."""
        return [self._validate_ann(a) for a in (anns or [])]

    # -- entry point ---------------------------------------------------

    def check_program(self, program):
        # Pre-pass: record top-level named functions so forward calls
        # and mutual recursion can be checked. Only signatures are
        # recorded -- annotations are validated in source order while
        # checking, so the FIRST error is still the earliest one.
        for stmt in program.statements:
            if isinstance(stmt, ast.FuncDecl) and stmt.name is not None:
                # Invalid names are left unrecorded here: the
                # statement check below raises the unknown-type error
                # in source order, and a call before the definition
                # must not report the bogus name first.
                param_types = [
                    a.name if (a is not None and a.name in VALID_TYPES)
                    else None
                    for a in (stmt.param_types or [])
                ]
                return_type = None
                if (stmt.return_type is not None
                        and stmt.return_type.name in VALID_TYPES):
                    return_type = stmt.return_type.name
                self._record_func(stmt.name, param_types, return_type)
        for stmt in program.statements:
            self._check_stmt(stmt)

    # -- statements ----------------------------------------------------

    def _check_block(self, block):
        self._push_scope()
        try:
            for stmt in block.statements:
                self._check_stmt(stmt)
        finally:
            self._pop_scope()

    def _check_stmt(self, node):
        if isinstance(node, ast.LetStmt):
            self._check_let(node)
        elif isinstance(node, ast.AssignStmt):
            self._check_assign(node)
        elif isinstance(node, ast.ExprStmt):
            self._check_expr(node.expr)
        elif isinstance(node, ast.FuncDecl):
            self._check_func_decl(node, statement=True)
        elif isinstance(node, ast.ReturnStmt):
            self._check_return(node)
        elif isinstance(node, ast.IfStmt):
            for cond, body in node.branches:
                self._check_condition(cond)
                self._check_block(body)
            if node.else_body is not None:
                self._check_block(node.else_body)
        elif isinstance(node, ast.WhileStmt):
            self._check_condition(node.condition)
            self._check_block(node.body)
        elif isinstance(node, ast.ForStmt):
            self._check_for(node)
        elif isinstance(node, (ast.BreakStmt, ast.ContinueStmt)):
            pass
        elif isinstance(node, ast.ImportStmt):
            pass  # imports bring in unknown names; nothing to check
        else:  # pragma: no cover - parser only builds known nodes
            pass

    def _check_condition(self, cond):
        type_ = self._check_expr(cond)
        if type_ != "any" and type_ != "bool":
            self._fail(
                cond,
                "This condition is a %s, but 'if' and 'while' need a "
                "bool -- try a comparison like == or <" % type_,
            )

    def _check_let(self, node):
        ann = self._validate_ann(node.type_ann)
        value_type = self._check_expr(node.value)
        if isinstance(node.value, ast.FuncDecl):
            # A let-bound anonymous function is a known user function
            # for later call-site checks.
            param_types = self._ann_types(node.value.param_types)
            return_type = self._validate_ann(node.value.return_type)
            self._record_func(node.name, param_types, return_type)
        if ann is not None:
            if (ann != "any" and value_type != "any"
                    and value_type != ann):
                self._fail(
                    node.value,
                    "Variable '%s' is declared as %s, but the value "
                    "is a %s -- give it a %s, or change the annotation"
                    % (node.name, ann, value_type, ann),
                )
            self._define(node.name, ann)
        else:
            self._define(node.name, value_type)

    def _check_assign(self, node):
        target_type = self._check_expr(node.target)
        value_type = self._check_expr(node.value)
        if node.op == "+=":
            self._check_plus(node, target_type, value_type)
        elif node.op == "-=":
            self._check_number_operands(node, "-=", target_type, value_type)

    def _check_return(self, node):
        if node.value is None:
            value_type = "null"
        else:
            value_type = self._check_expr(node.value)
        if not self.return_stack:
            return  # 'return' outside a function: the runtime owns it
        expected = self.return_stack[-1]
        if expected is None or expected == "any":
            return
        if value_type != "any" and value_type != expected:
            self._fail(
                node.value if node.value is not None else node,
                "This function should return a %s, but this 'return' "
                "gives a %s -- return a %s instead"
                % (expected, value_type, expected),
            )

    def _check_for(self, node):
        iter_type = self._check_expr(node.iterable)
        if iter_type != "any" and iter_type not in ("array", "string", "dict"):
            self._fail(
                node.iterable,
                "'for' needs an array, a string, or a dict to loop "
                "over, but this is a %s" % iter_type,
            )
        if iter_type == "string" or iter_type == "dict":
            elem_type = "string"  # chars for strings, keys for dicts
        else:
            elem_type = "any"
        self._push_scope()
        try:
            self._define(node.var, elem_type)
            self._check_block(node.body)
        finally:
            self._pop_scope()

    def _check_func_decl(self, node, statement):
        param_types = self._ann_types(node.param_types)
        return_type = self._validate_ann(node.return_type)
        if statement and node.name is not None:
            # Register before checking the body so recursion works.
            self._define(node.name, "function")
            self._record_func(node.name, param_types, return_type)
        self._push_scope()
        try:
            for name, type_ in zip(node.params, param_types):
                self._define(name, type_ if type_ is not None else "any")
            # Any params beyond the annotation list (cannot happen
            # from the parser, but be safe) are untyped.
            for name in node.params[len(param_types):]:
                self._define(name, "any")
            self.return_stack.append(return_type)
            try:
                for stmt in node.body.statements:
                    self._check_stmt(stmt)
            finally:
                self.return_stack.pop()
        finally:
            self._pop_scope()

    # -- expressions ---------------------------------------------------

    def _check_expr(self, node):
        if isinstance(node, ast.Literal):
            return _literal_type(node.value)
        if isinstance(node, ast.StringLiteral):
            for kind, part in node.parts:
                if kind == "expr":
                    # Interpolation parts are source text, not AST;
                    # nothing the checker can inspect.
                    pass
            return "string"
        if isinstance(node, ast.Var):
            return self._lookup(node.name)
        if isinstance(node, ast.ArrayLiteral):
            for element in node.elements:
                self._check_expr(element)
            return "array"
        if isinstance(node, ast.DictLiteral):
            for _key, value in node.pairs:
                self._check_expr(value)
            return "dict"
        if isinstance(node, ast.BinaryOp):
            return self._check_binary(node)
        if isinstance(node, ast.UnaryOp):
            self._check_expr(node.operand)
            return "number" if node.op == "-" else "bool"
        if isinstance(node, ast.Call):
            return self._check_call(node)
        if isinstance(node, ast.Index):
            target_type = self._check_expr(node.target)
            self._check_expr(node.index)
            if target_type == "string":
                return "string"
            return "any"  # array / dict elements, and anything unknown
        if isinstance(node, ast.Member):
            self._check_expr(node.object)
            if node.name in _STRING_METHODS:
                return "function"
            return "any"
        if isinstance(node, ast.FuncDecl):
            self._check_func_decl(node, statement=False)
            return "function"
        if isinstance(node, ast.MatchExpr):
            return self._check_match(node)
        return "any"  # pragma: no cover - unknown node kind

    def _check_plus(self, node, left, right):
        if left != "any" and right != "any":
            if not (left == right and left in ("number", "string", "array")):
                self._fail(
                    node,
                    "Cannot add a %s and a %s -- '+' works on two "
                    "numbers, two strings, or two arrays" % (left, right),
                )

    def _check_number_operands(self, node, op, left, right):
        if left != "any" and left != "number":
            self._fail(
                node,
                "'%s' needs numbers, but the left side is a %s"
                % (op, left),
            )
        if right != "any" and right != "number":
            self._fail(
                node,
                "'%s' needs numbers, but the right side is a %s"
                % (op, right),
            )

    def _check_binary(self, node):
        left = self._check_expr(node.left)
        right = self._check_expr(node.right)
        op = node.op
        if op == "+":
            self._check_plus(node, left, right)
            if left == right and left in ("number", "string", "array"):
                return left
            return "any"
        if op in ("-", "*", "/", "%", "**"):
            self._check_number_operands(node, op, left, right)
            if left == "number" and right == "number":
                return "number"
            return "any"
        if op in ("<", "<=", ">", ">="):
            if left != "any" and right != "any":
                if not (left == right
                        and left in ("number", "string")):
                    self._fail(
                        node,
                        "Cannot compare a %s with a %s using '%s' -- "
                        "comparisons need two numbers or two strings"
                        % (left, right, op),
                    )
            return "bool"
        if op in ("==", "!="):
            return "bool"
        if op in ("and", "or"):
            return "any"
        return "any"  # pragma: no cover - unknown operator

    # -- calls ---------------------------------------------------------

    def _check_arg(self, node, arg_types, index, allowed, what):
        """Error if a known argument type is outside *allowed*."""
        if index >= len(arg_types):
            return
        got = arg_types[index]
        if got != "any" and got not in allowed:
            self._fail(
                node,
                "%s, but got a %s -- pass %s instead"
                % (what, got, " or ".join(sorted(allowed))),
            )

    def _check_builtin_call(self, name, node, arg_types):
        if name == "print":
            pass  # any arguments, any count
        elif name == "input":
            self._check_arg(node, arg_types, 0, {"string"},
                            "input() needs a string prompt")
        elif name == "len":
            self._check_arg(node, arg_types, 0,
                            {"string", "array", "dict"},
                            "len() works on a string, array, or dict")
        elif name == "str" or name == "type":
            pass  # any single argument
        elif name == "num":
            self._check_arg(node, arg_types, 0, {"string", "number"},
                            "num() works on a string or a number")
        elif name == "range":
            self._check_arg(node, arg_types, 0, {"number"},
                            "range() needs whole numbers")
            self._check_arg(node, arg_types, 1, {"number"},
                            "range() needs whole numbers")
        elif name == "push":
            self._check_arg(node, arg_types, 0, {"array"},
                            "push() needs an array first")
        elif name == "pop":
            self._check_arg(node, arg_types, 0, {"array"},
                            "pop() needs an array")
        elif name == "sqrt" or name == "abs":
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs a number" % name)
        elif name == "pow":
            self._check_arg(node, arg_types, 0, {"number"},
                            "pow() needs two numbers")
            self._check_arg(node, arg_types, 1, {"number"},
                            "pow() needs two numbers")
        elif name == "keys":
            self._check_arg(node, arg_types, 0, {"dict"},
                            "keys() needs a dict")
        elif name in ("int", "float"):
            self._check_arg(node, arg_types, 0, {"string", "number"},
                            "%s() works on a string or a number" % name)
        return _BUILTIN_RETURNS[name]

    def _check_stdlib_call(self, name, node, arg_types):
        if name in ("randint", "randrange"):
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs numbers" % name)
            self._check_arg(node, arg_types, 1, {"number"},
                            "%s() needs numbers" % name)
        elif name == "choice":
            self._check_arg(node, arg_types, 0, {"array"},
                            "choice() needs an array")
        elif name == "shuffle":
            self._check_arg(node, arg_types, 0, {"array"},
                            "shuffle() needs an array")
        elif name == "seed":
            self._check_arg(node, arg_types, 0, {"number"},
                            "seed() needs a number")
        else:  # floor, ceil, sin, cos, tan, log, exp
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs a number" % name)
        return _STDLIB_RETURNS[name]

    def _check_call(self, node):
        arg_types = [self._check_expr(arg) for arg in node.args]
        callee = node.callee
        if isinstance(callee, ast.Var):
            name = callee.name
            sig = self._lookup_func(name)
            if sig is not None:
                param_types, return_type = sig
                for index, expected in enumerate(param_types):
                    if expected is None or expected == "any":
                        continue
                    if index < len(arg_types):
                        got = arg_types[index]
                        if got != "any" and got != expected:
                            self._fail(
                                node.args[index],
                                "Argument %d to '%s' should be a %s, "
                                "but got a %s" % (
                                    index + 1, name, expected, got),
                            )
                return return_type if return_type is not None else "any"
            if name in _BUILTIN_RETURNS:
                return self._check_builtin_call(name, node, arg_types)
            if name in _STDLIB_RETURNS:
                return self._check_stdlib_call(name, node, arg_types)
            return "any"  # unknown callee: the runtime owns it
        if isinstance(callee, ast.Member):
            receiver_type = self._check_expr(callee.object)
            method = callee.name
            if method in _STRING_METHODS:
                if receiver_type != "any" and receiver_type != "string":
                    self._fail(
                        callee,
                        "Method '%s' works on a string, but this is "
                        "a %s" % (method, receiver_type),
                    )
                allowed, return_type = _STRING_METHODS[method]
                for index, allowed_set in enumerate(allowed):
                    self._check_arg(
                        node, arg_types, index, allowed_set,
                        "'%s()' needs %s" % (
                            method, " or ".join(sorted(allowed_set))))
                return return_type
            return "any"
        # Callee is some other expression (a call result, a match,
        # an anonymous func, ...): check it and give up on the type.
        self._check_expr(callee)
        return "any"

    # -- match ---------------------------------------------------------

    def _check_match(self, node):
        scrutinee_type = self._check_expr(node.scrutinee)
        body_types = []
        for arm in node.arms:
            self._push_scope()
            try:
                pattern = arm.pattern
                if isinstance(pattern, ast.Literal):
                    pattern_type = _literal_type(pattern.value)
                    if (scrutinee_type != "any"
                            and pattern_type != scrutinee_type):
                        self._fail(
                            pattern,
                            "This pattern is a %s, but 'match' is "
                            "matching a %s -- use a %s pattern, or '_'"
                            % (pattern_type, scrutinee_type,
                               scrutinee_type),
                        )
                elif isinstance(pattern, ast.Var):
                    # A binding arm binds the scrutinee's type.
                    self._define(pattern.name, scrutinee_type)
                # Wildcard: binds nothing.
                if arm.guard is not None:
                    guard_type = self._check_expr(arm.guard)
                    if guard_type != "any" and guard_type != "bool":
                        self._fail(
                            arm.guard,
                            "A 'match' guard must be a bool, but this "
                            "is a %s" % guard_type,
                        )
                body_types.append(self._check_expr(arm.body))
            finally:
                self._pop_scope()
        if body_types and all(t == body_types[0] for t in body_types):
            return body_types[0]
        return "any"


def check_program(program, filename="<stdin>", lines=None):
    """Type-check *program* (a parsed AST).

    Raises ``KabirError`` with file/line on the FIRST type error;
    returns ``None`` on success. Untyped code always passes.
    """
    _Checker(filename, lines).check_program(program)
    return None
