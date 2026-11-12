"""Canonical formatter for Kabir (`kabir fmt`).

Reads a .kbr file, parses it (which also validates it), and re-prints
it in one canonical style. The formatter is AST-based: it re-emits the
program from the parsed tree, so spacing, indentation and blank lines
are fully deterministic and formatting twice is a no-op.

Comments are preserved: they are not part of the AST, so they are
extracted from the source first (by a small string-aware scanner) and
re-attached by line number — a comment on its own line stays on its own
line before the statement it preceded, and a comment after code on a
line stays at the end of that line. A comment the formatter cannot
place exactly (for example one inside a multi-line array literal) is
kept as its own line right after the statement it was inside — never
dropped.

String literals are never re-interpreted: literal parts are re-escaped
character for character, and interpolation expressions are re-parsed
and re-formatted, falling back to the original text if they cannot be
parsed. Nothing about the program's meaning changes; `kabir fmt`
followed by `kabir run` prints exactly what the unformatted file did.

Canonical style (documented in docs/LANGUAGE.md):
- 4-space indentation, one statement per line.
- One space around binary operators and after commas; no space before
  commas, `(` or `[` in calls and indexing.
- `if`/`while`/`for`/`func` headers keep `{` on the same line; the
  closing `}` sits on its own line at the header's indent.
- At the top level, function declarations are separated from their
  neighbours by exactly one blank line, and a blank line after a
  comment block (the file header, a section note) is kept; no blank
  lines are inserted anywhere else.
- Short arrays and dicts stay on one line (up to 80 characters);
  longer ones break to one element per line, with no trailing comma
  (Kabir does not allow trailing commas).
- No trailing whitespace; the file ends with exactly one newline.
"""

from . import ast_nodes as ast
from .errors import KabirError
from .parser import parse

INDENT = "    "
MAX_ONE_LINE = 80

# Precedence, lowest to highest (mirrors parser.py). A child expression
# is parenthesised when its precedence is lower than the context it
# sits in; that is the only place the formatter adds parentheses.
_PREC = {
    "or": 1, "and": 2,
    "==": 3, "!=": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
    "**": 7,
}
_UNARY_PREC = 8
_POSTFIX_PREC = 9
_PRIMARY_PREC = 10


class _Comment:
    __slots__ = ("line", "text", "standalone")

    def __init__(self, line, text, standalone):
        self.line = line            # 1-based source line
        self.text = text            # from "#" to end of line, rstripped
        self.standalone = standalone  # True if the line holds only the comment


def _skip_string(source, i):
    """Index just past the string starting at source[i] == '"'.

    Mirrors the lexer's string rules: backslash escapes, and { ... }
    interpolation with nested strings and braces. A "#" inside a
    string (or inside an interpolation) is not a comment.
    """
    n = len(source)
    i += 1  # opening quote
    while i < n:
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        if ch == "\n":
            return i  # unterminated; the parser will report it
        if ch == "{":
            i = _skip_interpolation(source, i)
            continue
        i += 1
    return i


def _skip_interpolation(source, i):
    """Index just past the interpolation starting at source[i] == '{'."""
    n = len(source)
    depth = 1
    i += 1
    while i < n and depth > 0:
        ch = source[i]
        if ch == '"':
            i = _skip_string(source, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _extract_comments(source):
    """All # comments in the source, in line order.

    The lexer drops comments, so the formatter collects them itself
    with a scanner that understands strings (a # inside "..." is
    text, not a comment).
    """
    comments = []
    lines = source.split("\n")
    i = 0
    n = len(source)
    line = 1
    while i < n:
        ch = source[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch == '"':
            i = _skip_string(source, i)
        elif ch == "#":
            j = source.find("\n", i)
            if j == -1:
                j = n
            text = source[i:j].rstrip()
            standalone = lines[line - 1].lstrip().startswith("#")
            comments.append(_Comment(line, text, standalone))
            i = j
        else:
            i += 1
    return comments


def _escape_lit(text):
    """Re-escape a decoded string part back to source form."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


def _format_number(value):
    """A numeric literal back to source text.

    repr() round-trips Python floats, but it can produce exponent
    notation (1e+16), which Kabir's lexer does not accept. In that
    case fall back to plain decimal notation when it keeps the value.
    """
    text = repr(value)
    if "e" in text or "E" in text:
        plain = ("%.15f" % value).rstrip("0").rstrip(".")
        if plain and float(plain) == value:
            return plain
    return text


class _Printer:
    def __init__(self, comments):
        self.comments = comments
        self.cidx = 0  # next comment not yet emitted

    # -- comment bookkeeping ------------------------------------------
    def _pending_before(self, line):
        """Comments on lines before `line`, in order; consumes them."""
        out = []
        while (self.cidx < len(self.comments)
               and self.comments[self.cidx].line < line):
            out.append(self.comments[self.cidx])
            self.cidx += 1
        return out

    def _comment_on(self, line):
        """The comment on exactly `line`, or None; consumes it."""
        if (self.cidx < len(self.comments)
                and self.comments[self.cidx].line == line):
            comment = self.comments[self.cidx]
            self.cidx += 1
            return comment
        return None

    def _drain(self):
        """Every comment not yet emitted, in order; consumes them."""
        out = self.comments[self.cidx:]
        self.cidx = len(self.comments)
        return out

    # -- program -------------------------------------------------------
    def format_program(self, program):
        lines = []
        prev_was_func = False
        for stmt in program.statements:
            is_func = isinstance(stmt, ast.FuncDecl) and stmt.name is not None
            # Top-level functions are separated from their neighbours by
            # exactly one blank line; nothing else gets one.
            if lines and (is_func or prev_was_func):
                lines.append("")
            lines.extend(self._fmt_stmt(stmt, 0))
            prev_was_func = is_func
        for comment in self._drain():
            lines.append(comment.text)
        return lines

    # -- statements ----------------------------------------------------
    def _fmt_stmt(self, stmt, level):
        indent = INDENT * level
        lines = []
        pending = self._pending_before(stmt.line)
        for comment in pending:
            lines.append(indent + comment.text)
        if (level == 0 and pending and pending[-1].standalone
                and stmt.line - pending[-1].line > 1):
            # A blank line after a top-level comment block is part of
            # the block's spacing (the file header, a section note);
            # keep exactly one. Inside blocks, blank lines are removed.
            lines.append("")
        body = self._stmt_lines(stmt, level)
        comment = self._comment_on(stmt.line)
        if comment is not None and body:
            body[0] += "  " + comment.text
        lines.extend(body)
        return lines

    def _stmt_lines(self, stmt, level):
        indent = INDENT * level
        if isinstance(stmt, ast.LetStmt):
            prefix = "let " + ("mut " if getattr(stmt, "mutable", False)
                               else "")
            if getattr(stmt, "type_ann", None) is not None:
                return [indent + "%s%s: %s = %s"
                        % (prefix, stmt.name, stmt.type_ann.full_name,
                           self._fmt_expr(stmt.value, level))]
            return [indent + "%s%s = %s"
                    % (prefix, stmt.name, self._fmt_expr(stmt.value, level))]
        if isinstance(stmt, ast.AssignStmt):
            target = self._fmt_expr(stmt.target, level)
            return [indent + "%s %s %s"
                    % (target, stmt.op, self._fmt_expr(stmt.value, level))]
        if isinstance(stmt, ast.ExprStmt):
            return [indent + self._fmt_expr(stmt.expr, level)]
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is None:
                return [indent + "return"]
            return [indent + "return "
                    + self._fmt_expr(stmt.value, level)]
        if isinstance(stmt, ast.ImportStmt):
            return [indent + "import " + _quote(stmt.path)]
        if isinstance(stmt, ast.BreakStmt):
            return [indent + "break"]
        if isinstance(stmt, ast.ContinueStmt):
            return [indent + "continue"]
        if isinstance(stmt, ast.FuncDecl):
            return self._fmt_func(stmt, level)
        if isinstance(stmt, ast.WhileStmt):
            lines = [indent + "while %s {"
                     % self._fmt_expr(stmt.condition, level)]
            body, close = self._fmt_block(stmt.body, level + 1)
            lines.extend(body)
            lines.append(self._close_line(level, close))
            return lines
        if isinstance(stmt, ast.ForStmt):
            lines = [indent + "for %s in %s {"
                     % (stmt.var, self._fmt_expr(stmt.iterable, level))]
            body, close = self._fmt_block(stmt.body, level + 1)
            lines.extend(body)
            lines.append(self._close_line(level, close))
            return lines
        if isinstance(stmt, ast.IfStmt):
            return self._fmt_if(stmt, level)
        raise AssertionError("formatter: unknown statement %r" % (stmt,))

    def _close_line(self, level, close_comment):
        line = INDENT * level + "}"
        if close_comment:
            line += "  " + close_comment
        return line

    def _fmt_func_header(self, stmt):
        """The `func name(params) -> Ret {` header, without indent."""
        types = getattr(stmt, "param_types", None) or [None] * len(stmt.params)
        params = ", ".join(
            p if t is None else "%s: %s" % (p, t.full_name)
            for p, t in zip(stmt.params, types))
        ret = getattr(stmt, "return_type", None)
        ret_text = (" -> %s" % ret.full_name) if ret is not None else ""
        # 4.0.0 async: `async func ...` round-trips through the
        # formatter (used by both declarations and anonymous funcs).
        prefix = "async " if getattr(stmt, "is_async", False) else ""
        if stmt.name is not None:
            return "%sfunc %s(%s)%s {" % (prefix, stmt.name, params, ret_text)
        return "%sfunc(%s)%s {" % (prefix, params, ret_text)

    def _fmt_func(self, stmt, level):
        indent = INDENT * level
        lines = [indent + self._fmt_func_header(stmt)]
        body, close = self._fmt_block(stmt.body, level + 1)
        lines.extend(body)
        lines.append(self._close_line(level, close))
        return lines

    def _fmt_if(self, stmt, level):
        indent = INDENT * level
        lines = []
        first = True
        close = None
        for cond, block in stmt.branches:
            header = "%s %s {" % ("if" if first else "elif",
                                 self._fmt_expr(cond, level))
            if first:
                lines.append(indent + header)
            else:
                line = indent + "} " + header
                if close:
                    line += "  " + close
                lines.append(line)
            first = False
            body, close = self._fmt_block(block, level + 1)
            lines.extend(body)
        if stmt.else_body is not None:
            line = indent + "} else {"
            if close:
                line += "  " + close
            lines.append(line)
            body, close = self._fmt_block(stmt.else_body, level + 1)
            lines.extend(body)
        lines.append(self._close_line(level, close))
        return lines

    def _fmt_block(self, block, level):
        """A block's inner lines, plus its closing-line comment.

        Returns (lines, close_comment): the lines do not include the
        closing "}", and close_comment is the text of a comment on the
        "}" line (or None), so the caller can put it after the "}"
        or after the "} elif ... {" that shares that line.
        """
        indent = INDENT * level
        lines = []
        for stmt in block.statements:
            lines.extend(self._fmt_stmt(stmt, level))
        end_line = getattr(block, "end_line", None)
        if end_line is not None:
            for comment in self._pending_before(end_line):
                lines.append(indent + comment.text)
            comment = self._comment_on(end_line)
            return lines, (comment.text if comment else None)
        return lines, None

    # -- expressions ---------------------------------------------------
    def _fmt_expr(self, node, level, ctx=0):
        text, prec = self._expr_parts(node, level)
        if prec < ctx:
            return "(" + text + ")"
        return text

    def _expr_parts(self, node, level):
        if isinstance(node, ast.Literal):
            value = node.value
            if value is None:
                return "null", _PRIMARY_PREC
            if value is True:
                return "true", _PRIMARY_PREC
            if value is False:
                return "false", _PRIMARY_PREC
            if isinstance(value, (int, float)):
                return _format_number(value), _PRIMARY_PREC
            if isinstance(value, str):
                # Only a string literal in a match pattern reaches
                # here as a Literal; ordinary strings are StringLiteral.
                return _quote(value), _PRIMARY_PREC
            raise AssertionError("formatter: bad literal %r" % (value,))
        if isinstance(node, ast.StringLiteral):
            return self._fmt_string(node), _PRIMARY_PREC
        if isinstance(node, ast.Var):
            return node.name, _PRIMARY_PREC
        if isinstance(node, ast.ArrayLiteral):
            return self._fmt_array(node, level), _PRIMARY_PREC
        if isinstance(node, ast.DictLiteral):
            return self._fmt_dict(node, level), _PRIMARY_PREC
        if isinstance(node, ast.BinaryOp):
            prec = _PREC[node.op]
            if node.op == "**":
                # Right-associative: the left side binds tighter.
                left = self._fmt_expr(node.left, level, _UNARY_PREC)
                right = self._fmt_expr(node.right, level, prec)
            else:
                left = self._fmt_expr(node.left, level, prec)
                right = self._fmt_expr(node.right, level, prec + 1)
            return "%s %s %s" % (left, node.op, right), prec
        if isinstance(node, ast.UnaryOp):
            if node.op == "not":
                text = "not " + self._fmt_expr(node.operand, level,
                                                _UNARY_PREC)
            else:
                text = "-" + self._fmt_expr(node.operand, level,
                                            _UNARY_PREC)
            return text, _UNARY_PREC
        if isinstance(node, ast.AwaitExpr):
            # 4.0.0 async: `await` binds like a unary operator.
            text = "await " + self._fmt_expr(node.expr, level, _UNARY_PREC)
            return text, _UNARY_PREC
        if isinstance(node, ast.BorrowExpr):
            # A borrow binds like a unary operator: `&x`, `&mut x`,
            # `&x[0]`; a lower-precedence operand gets parentheses.
            inner = self._fmt_expr(node.expr, level, _UNARY_PREC)
            if node.mutable:
                return "&mut " + inner, _UNARY_PREC
            return "&" + inner, _UNARY_PREC
        if isinstance(node, ast.Call):
            callee = self._fmt_expr(node.callee, level, _POSTFIX_PREC)
            args = ", ".join(self._fmt_expr(a, level) for a in node.args)
            return "%s(%s)" % (callee, args), _POSTFIX_PREC
        if isinstance(node, ast.Index):
            target = self._fmt_expr(node.target, level, _POSTFIX_PREC)
            index = self._fmt_expr(node.index, level)
            return "%s[%s]" % (target, index), _POSTFIX_PREC
        if isinstance(node, ast.Member):
            obj = self._fmt_expr(node.object, level, _POSTFIX_PREC)
            return "%s.%s" % (obj, node.name), _POSTFIX_PREC
        if isinstance(node, ast.FuncDecl):
            return self._fmt_func_expr(node, level), _PRIMARY_PREC
        if isinstance(node, ast.MatchExpr):
            return self._fmt_match(node, level), _PRIMARY_PREC
        raise AssertionError("formatter: unknown expression %r" % (node,))

    def _fmt_func_expr(self, node, level):
        """An anonymous function as an expression value."""
        lines = [self._fmt_func_header(node)]
        body, close = self._fmt_block(node.body, level + 1)
        lines.extend(body)
        lines.append(self._close_line(level, close))
        return "\n".join(lines)

    def _fmt_match(self, node, level):
        """A match expression: one line if it fits in 80 characters,
        else one arm per line."""
        scrut = self._fmt_expr(node.scrutinee, level)
        arms = [self._fmt_arm(a, level) for a in node.arms]
        if arms:
            one_line = "match %s { %s }" % (scrut, ", ".join(arms))
        else:
            one_line = "match %s { }" % scrut
        if "\n" not in one_line and len(one_line) <= MAX_ONE_LINE:
            return one_line
        indent = INDENT * (level + 1)
        close = INDENT * level
        return ("match %s {\n" % scrut
                + ",\n".join(indent + a for a in arms)
                + "\n" + close + "}")

    def _fmt_arm(self, arm, level):
        if isinstance(arm.pattern, ast.Wildcard):
            text = "_"
        else:
            text = self._fmt_expr(arm.pattern, level)
        if arm.guard is not None:
            text += " if " + self._fmt_expr(arm.guard, level)
        return "%s => %s" % (text, self._fmt_expr(arm.body, level))

    def _fmt_string(self, node):
        parts = []
        for kind, text in node.parts:
            if kind == "lit":
                parts.append(_escape_lit(text))
            else:
                parts.append("{" + self._fmt_interpolation(text) + "}")
        return '"' + "".join(parts) + '"'

    def _fmt_interpolation(self, text):
        """Format an interpolation expression; keep it verbatim if it
        will not parse (the program parsed fine, so this is only a
        safety net -- the runtime parses it the same way)."""
        try:
            program = parse(text, "<interpolation>")
        except KabirError:
            return text
        if (len(program.statements) != 1
                or not isinstance(program.statements[0], ast.ExprStmt)):
            return text
        return self._fmt_expr(program.statements[0].expr, 0)

    def _fmt_array(self, node, level):
        if not node.elements:
            return "[]"
        parts = [self._fmt_expr(e, level) for e in node.elements]
        one_line = "[" + ", ".join(parts) + "]"
        if "\n" not in one_line and len(one_line) <= MAX_ONE_LINE:
            return one_line
        indent = INDENT * (level + 1)
        close = INDENT * level
        return ("[\n" + ",\n".join(indent + p for p in parts)
                + "\n" + close + "]")

    def _fmt_dict(self, node, level):
        if not node.pairs:
            return "{}"
        parts = ["%s: %s" % (_quote(key), self._fmt_expr(v, level))
                 for key, v in node.pairs]
        one_line = "{" + ", ".join(parts) + "}"
        if "\n" not in one_line and len(one_line) <= MAX_ONE_LINE:
            return one_line
        indent = INDENT * (level + 1)
        close = INDENT * level
        return ("{\n" + ",\n".join(indent + p for p in parts)
                + "\n" + close + "}")


def _quote(text):
    """A plain string as a double-quoted Kabir literal."""
    return '"' + _escape_lit(text) + '"'


def format_source(source, filename="<stdin>"):
    """Format Kabir source text; returns the formatted text.

    Raises KabirError on invalid syntax -- callers must not write
    the file in that case.
    """
    program = parse(source, filename)
    printer = _Printer(_extract_comments(source))
    lines = printer.format_program(program)
    text = "\n".join(lines)
    if text:
        text += "\n"
    return text


def format_file(path):
    """Format one file in place. Returns True if it changed.

    Raises FileNotFoundError / OSError on I/O problems and KabirError
    on invalid syntax; in both cases the file is left untouched.
    """
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    formatted = format_source(source, path)
    if formatted == source:
        return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(formatted)
    return True
