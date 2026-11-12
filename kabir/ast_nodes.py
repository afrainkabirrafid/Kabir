"""AST node classes for Kabir.

Every node carries its source line/column so the interpreter can
report friendly errors pointing at the exact spot.
"""


class Node:
    def __init__(self, line=None, col=None):
        self.line = line
        self.col = col


class Program(Node):
    def __init__(self, statements, line=None, col=None):
        super().__init__(line, col)
        self.statements = statements


class Block(Node):
    def __init__(self, statements, line=None, col=None):
        super().__init__(line, col)
        self.statements = statements


# --- Statements -------------------------------------------------------

class LetStmt(Node):
    def __init__(self, name, value, line=None, col=None, type_ann=None,
                 mutable=False):
        super().__init__(line, col)
        self.name = name
        self.value = value
        self.type_ann = type_ann  # TypeAnn or None
        # 3.0.0 ownership lite: True for `let mut x = ...`. Plain `let`
        # stays mutable at runtime exactly as before; this flag only
        # marks the binding for the checker's move tracking.
        self.mutable = mutable


class AssignStmt(Node):
    def __init__(self, target, op, value, line=None, col=None):
        super().__init__(line, col)
        self.target = target  # Var or Index node
        self.op = op          # "=" | "+=" | "-="
        self.value = value


class ExprStmt(Node):
    def __init__(self, expr, line=None, col=None):
        super().__init__(line, col)
        self.expr = expr


class FuncDecl(Node):
    def __init__(self, name, params, body, line=None, col=None,
                 param_types=None, return_type=None, is_async=False):
        super().__init__(line, col)
        self.name = name      # may be None for an anonymous func expression
        self.params = params  # list of names
        self.body = body      # Block
        # param_types: list parallel to params, each a TypeAnn or None;
        # return_type: a TypeAnn or None. Both None when unannotated.
        self.param_types = param_types
        self.return_type = return_type
        # 4.0.0 async: True for `async func ...`. Calling an async
        # function starts a task and returns it at once (VM only);
        # `await` inside it suspends that task until awaited work
        # finishes.
        self.is_async = is_async


class ReturnStmt(Node):
    def __init__(self, value, line=None, col=None):
        super().__init__(line, col)
        self.value = value    # may be None


class IfStmt(Node):
    def __init__(self, branches, else_body, line=None, col=None):
        super().__init__(line, col)
        # branches: list of (condition, Block); else_body: Block or None
        self.branches = branches
        self.else_body = else_body


class WhileStmt(Node):
    def __init__(self, condition, body, line=None, col=None):
        super().__init__(line, col)
        self.condition = condition
        self.body = body


class ForStmt(Node):
    def __init__(self, var, iterable, body, line=None, col=None):
        super().__init__(line, col)
        self.var = var
        self.iterable = iterable
        self.body = body


class BreakStmt(Node):
    pass


class ContinueStmt(Node):
    pass


class ImportStmt(Node):
    def __init__(self, path, line=None, col=None):
        super().__init__(line, col)
        self.path = path    # plain string, e.g. "utils/helpers.kbr"


# --- Expressions ------------------------------------------------------

class Literal(Node):
    def __init__(self, value, line=None, col=None):
        super().__init__(line, col)
        self.value = value


class StringLiteral(Node):
    def __init__(self, parts, line=None, col=None):
        super().__init__(line, col)
        # parts: list of ("lit", text) | ("expr", source text)
        self.parts = parts


class Var(Node):
    def __init__(self, name, line=None, col=None):
        super().__init__(line, col)
        self.name = name


class ArrayLiteral(Node):
    def __init__(self, elements, line=None, col=None):
        super().__init__(line, col)
        self.elements = elements


class DictLiteral(Node):
    def __init__(self, pairs, line=None, col=None):
        super().__init__(line, col)
        # pairs: list of (key-string, value-node)
        self.pairs = pairs


class BinaryOp(Node):
    def __init__(self, op, left, right, line=None, col=None):
        super().__init__(line, col)
        self.op = op
        self.left = left
        self.right = right


class UnaryOp(Node):
    def __init__(self, op, operand, line=None, col=None):
        super().__init__(line, col)
        self.op = op  # "-" | "not"
        self.operand = operand


class AwaitExpr(Node):
    """`await <expr>` (4.0.0 async): suspends the current task until
    the awaited task completes, then evaluates to that task's return
    value. Only valid inside an `async func` (the compiler enforces
    this); only the VM runs it (the interpreter rejects it)."""

    def __init__(self, expr, line=None, col=None):
        super().__init__(line, col)
        self.expr = expr


class Call(Node):
    def __init__(self, callee, args, line=None, col=None):
        super().__init__(line, col)
        self.callee = callee
        self.args = args


class Index(Node):
    def __init__(self, target, index, line=None, col=None):
        super().__init__(line, col)
        self.target = target
        self.index = index


class Member(Node):
    def __init__(self, object, name, line=None, col=None):
        super().__init__(line, col)
        self.object = object  # receiver expression node
        self.name = name      # method name as a plain string


# --- 4.0.0 additions: borrow expressions and borrow types --------

class BorrowExpr(Node):
    """A borrow: `&x` (shared) or `&mut x` (mutable).

    Compile-time only: at runtime a borrow IS the value -- `&x`
    compiles to just `x`, so borrowing costs nothing. The checker
    enforces the borrow rules; the VM/interpreter just evaluate
    the borrowed expression.
    """
    def __init__(self, expr, mutable, line=None, col=None):
        super().__init__(line, col)
        self.expr = expr        # the borrowed place expression
        self.mutable = mutable  # True for `&mut x`, False for `&x`


class TypeAnn(Node):
    def __init__(self, name, line=None, col=None, borrow=None):
        super().__init__(line, col)
        self.name = name      # base type name as written, e.g. "number"
        # 4.0.0 borrow types: None (plain), "shared" (`&number`),
        # "mut" (`&mut number`).
        self.borrow = borrow

    @property
    def full_name(self):
        """The type as written, e.g. "number", "&number", "&mut array"."""
        if self.borrow == "shared":
            return "&" + self.name
        if self.borrow == "mut":
            return "&mut " + self.name
        return self.name


class Wildcard(Node):
    """The `_` pattern in a match arm: matches anything, binds nothing."""
    pass


class MatchArm(Node):
    def __init__(self, pattern, guard, body, line=None, col=None):
        super().__init__(line, col)
        self.pattern = pattern  # Literal | Var | Wildcard
        self.guard = guard      # expression or None
        self.body = body        # expression


class MatchExpr(Node):
    def __init__(self, scrutinee, arms, line=None, col=None):
        super().__init__(line, col)
        self.scrutinee = scrutinee
        self.arms = arms        # list of MatchArm
