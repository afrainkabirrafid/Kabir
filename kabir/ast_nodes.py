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
    def __init__(self, name, value, line=None, col=None):
        super().__init__(line, col)
        self.name = name
        self.value = value


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
    def __init__(self, name, params, body, line=None, col=None):
        super().__init__(line, col)
        self.name = name      # may be None for an anonymous func expression
        self.params = params  # list of names
        self.body = body      # Block


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
