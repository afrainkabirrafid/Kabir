"""Recursive-descent parser for Kabir.

Produces an AST from the token stream. Precedence, lowest to highest:
or -> and -> equality (== !=) -> comparison (< <= > >=) -> additive
(+ -) -> multiplicative (* / %) -> power (** , right-associative) ->
unary (-, not) -> postfix (call, index) -> primary.
"""

import re

from .errors import KabirError, error_at
from . import ast_nodes as ast

# Package names for bare imports (import greet): same rule as
# project.NAME_RE -- starts with a letter, then letters, digits,
# "-" and "_". Kept here (rather than importing project) so the
# parser stays independent of the project/registry layer.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class Parser:
    def __init__(self, tokens, filename="<stdin>", source_lines=None):
        self.tokens = tokens
        self.filename = filename
        self.lines = source_lines or []
        self.pos = 0

    # -- helpers -------------------------------------------------------
    @property
    def current(self):
        return self.tokens[self.pos]

    def _fail(self, message, token=None):
        tok = token or self.current
        raise error_at(message, self.filename, self.lines, tok.line, tok.col)

    def _advance(self):
        tok = self.current
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def _check(self, kind, value=None):
        tok = self.current
        if tok.kind != kind:
            return False
        return value is None or tok.value == value

    def _match(self, kind, value=None):
        if self._check(kind, value):
            return self._advance()
        return None

    def _expect(self, kind, value=None, what=None):
        if self._check(kind, value):
            return self._advance()
        want = what or repr(value if value is not None else kind)
        got = self.current
        if got.kind == "EOF":
            self._fail("Expected %s but reached the end of the file" % want)
        self._fail("Expected %s but found %r" % (want, got.value))

    def _skip_newlines(self):
        while self._check("NEWLINE"):
            self._advance()

    def _skip_separators(self):
        while self._check("NEWLINE") or self._check("PUNCT", ";"):
            self._advance()

    # -- entry point ---------------------------------------------------
    def parse(self):
        statements = []
        self._skip_separators()
        while not self._check("EOF"):
            statements.append(self._statement())
            self._skip_separators()
        return ast.Program(statements, line=1, col=1)

    # -- statements ----------------------------------------------------
    def _block(self):
        tok = self._expect("PUNCT", "{", "'{' to start a block")
        statements = []
        self._skip_separators()
        while not self._check("PUNCT", "}"):
            if self._check("EOF"):
                self._fail("Expected '}' to close this block", token=tok)
            statements.append(self._statement())
            self._skip_separators()
        close = self._advance()  # consume "}"
        block = ast.Block(statements, line=tok.line, col=tok.col)
        # The formatter needs the closing brace's line to place comments
        # that sit inside the block or on the "}" line itself.
        block.end_line = close.line
        return block

    def _statement(self):
        tok = self.current
        if self._check("KEYWORD", "let"):
            return self._let()
        if self._check("KEYWORD", "func"):
            # func declaration (named) or func expression statement
            return self._func_decl_or_expr_stmt()
        if self._check("KEYWORD", "async"):
            # async func declaration: `async func name() { ... }`
            async_tok = self._advance()
            return self._func_decl_or_expr_stmt(is_async=True,
                                                async_tok=async_tok)
        if self._check("KEYWORD", "return"):
            self._advance()
            value = None
            if not (self._check("NEWLINE") or self._check("PUNCT", ";")
                    or self._check("PUNCT", "}") or self._check("EOF")):
                value = self._expression()
            return ast.ReturnStmt(value, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "if"):
            return self._if()
        if self._check("KEYWORD", "while"):
            self._advance()
            cond = self._expression()
            body = self._block()
            return ast.WhileStmt(cond, body, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "for"):
            return self._for()
        if self._check("KEYWORD", "break"):
            self._advance()
            return ast.BreakStmt(line=tok.line, col=tok.col)
        if self._check("KEYWORD", "continue"):
            self._advance()
            return ast.ContinueStmt(line=tok.line, col=tok.col)
        if self._check("KEYWORD", "import"):
            return self._import()
        if self._check("PUNCT", "{"):
            # A "{" that opens a string key followed by ":" is a dict
            # literal, not a block: {"a": 1} == {"a": 1}.
            nxt = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
            nxt2 = self.tokens[self.pos + 2] if self.pos + 2 < len(self.tokens) else None
            if (nxt is not None and nxt.kind == "STRING"
                    and nxt2 is not None and nxt2.kind == "PUNCT"
                    and nxt2.value == ":"):
                return self._expr_or_assign_stmt()
            return self._block()
        return self._expr_or_assign_stmt()

    def _import(self):
        tok = self._advance()  # import
        if self._check("STRING"):
            # Quoted file import: import "helpers.kbr".
            path_tok = self._advance()
            parts = path_tok.value
            if len(parts) != 1 or parts[0][0] != "lit":
                self._fail("import paths must be plain strings, not "
                           "interpolated ones", token=path_tok)
            path = parts[0][1]
            if not path.endswith(".kbr"):
                self._fail('import "%s" must name a .kbr file' % path,
                           token=path_tok)
            return ast.ImportStmt(path, line=tok.line, col=tok.col)
        if self._check("IDENT"):
            # Bare-name package import: import greet (7.0.0, registry
            # wiring -- resolved at run time by kabir/imports.py).
            # A hyphenated name (import my-pkg) lexes as IDENT, OP(-),
            # IDENT, so consume "-name" segments while they follow.
            name = self._advance().value
            while self._check("OP", "-"):
                nxt = (self.tokens[self.pos + 1]
                       if self.pos + 1 < len(self.tokens) else None)
                if nxt is None or nxt.kind != "IDENT":
                    break
                self._advance()  # -
                name += "-" + self._advance().value
            if not _PACKAGE_NAME_RE.match(name):
                self._fail('Invalid package name "%s" -- package names '
                           "must start with a letter and contain only "
                           'letters, digits, "-" and "_"', token=tok)
            return ast.ImportStmt(name, line=tok.line, col=tok.col)
        self._fail("Expected a file path in double quotes after 'import' "
                   '-- like import "helpers.kbr" -- or a package name, '
                   "like import greet", token=tok)

    def _type_ann(self):
        # 4.0.0 borrow types: `&number` (shared) and `&mut number`.
        # 7.0.0 lifetimes: `&'a number` and `&'a mut array`.
        borrow = None
        lifetime = None
        if self._match("OP", "&"):
            borrow = "shared"
            if self._check("LIFETIME"):
                lifetime = self._advance().value
            if self._check("IDENT", "mut"):
                nxt = (self.tokens[self.pos + 1]
                       if self.pos + 1 < len(self.tokens) else None)
                # `&mut` is a mutable-borrow marker only when a type
                # name follows; `&mut` on its own is not a type.
                if nxt is not None and nxt.kind == "IDENT":
                    self._advance()
                    borrow = "mut"
        tok = self._expect("IDENT",
                           what="a type name, like 'number'")
        return ast.TypeAnn(tok.value, line=tok.line, col=tok.col,
                           borrow=borrow, lifetime=lifetime)

    def _let(self):
        tok = self._advance()  # let
        # `mut` is CONTEXTUAL, not a keyword: the lexer produces IDENT
        # "mut", and it only means "mutable binding" when it appears
        # immediately after `let` AND is followed by a variable name.
        # `let mut = 5` still declares a variable named `mut`, and
        # `mut` keeps working as an ordinary identifier everywhere
        # else (a variable, a call, a member, ...).
        mutable = False
        if self._check("IDENT", "mut"):
            nxt = (self.tokens[self.pos + 1]
                   if self.pos + 1 < len(self.tokens) else None)
            if nxt is not None and nxt.kind == "IDENT":
                self._advance()
                mutable = True
        name_tok = self._expect("IDENT", what="a variable name after 'let'")
        type_ann = None
        if self._match("PUNCT", ":"):
            type_ann = self._type_ann()
        self._expect("OP", "=", "'=' after the variable name in 'let'")
        value = self._expression()
        return ast.LetStmt(name_tok.value, value,
                           line=tok.line, col=tok.col, type_ann=type_ann,
                           mutable=mutable)

    def _func_decl_or_expr_stmt(self, is_async=False, async_tok=None):
        if async_tok is not None:
            # `async` was already consumed; `func` must follow it.
            # _expect returns (and consumes) the `func` token.
            tok = self._expect("KEYWORD", "func",
                               "'func' after 'async' -- write 'async func "
                               "name() { ... }'")
        else:
            tok = self._advance()  # func
        name = None
        if self._check("IDENT"):
            name = self._advance().value
        # 7.0.0 lifetimes: `func f<'a, 'b>(...)` declares the
        # lifetime parameters the annotations below may use.
        lifetimes = []
        if self._check("OP", "<"):
            self._advance()
            self._skip_newlines()
            while True:
                lt = self._expect("LIFETIME",
                                  what="a lifetime name, like 'a, after '<'")
                lifetimes.append(lt.value)
                self._skip_newlines()
                if not self._match("PUNCT", ","):
                    break
                self._skip_newlines()
            self._expect("OP", ">",
                         "'>' to close the lifetime list")
        self._expect("PUNCT", "(", "'(' after the function name")
        params = []
        param_types = []
        if not self._check("PUNCT", ")"):
            while True:
                p = self._expect("IDENT", what="a parameter name")
                params.append(p.value)
                if self._match("PUNCT", ":"):
                    param_types.append(self._type_ann())
                else:
                    param_types.append(None)
                if not self._match("PUNCT", ","):
                    break
        self._expect("PUNCT", ")", "')' after the parameter list")
        return_type = None
        if self._match("OP", "->"):
            return_type = self._type_ann()
        body = self._block()
        line = async_tok.line if async_tok is not None else tok.line
        col = async_tok.col if async_tok is not None else tok.col
        return ast.FuncDecl(name, params, body,
                            line=line, col=col,
                            param_types=param_types,
                            return_type=return_type,
                            is_async=is_async,
                            lifetimes=lifetimes)

    # -- match expressions --------------------------------------------
    def _match_expr(self):
        tok = self._advance()  # match
        scrutinee = self._expression()
        self._expect("PUNCT", "{", "'{' after the value in 'match'")
        arms = []
        self._skip_newlines()
        if not self._check("PUNCT", "}"):
            while True:
                arms.append(self._match_arm())
                self._skip_newlines()
                if not self._match("PUNCT", ","):
                    break
                self._skip_newlines()
                if self._check("PUNCT", "}"):
                    break  # trailing comma
        self._expect("PUNCT", "}", "'}' to close the match")
        return ast.MatchExpr(scrutinee, arms, line=tok.line, col=tok.col)

    def _match_arm(self):
        tok = self.current
        pattern = self._pattern()
        guard = None
        if self._check("KEYWORD", "if"):
            self._advance()
            guard = self._expression()
        self._expect("OP", "=>", "'=>' after the pattern in a 'match' arm")
        body = self._expression()
        return ast.MatchArm(pattern, guard, body,
                            line=tok.line, col=tok.col)

    def _pattern(self):
        tok = self.current
        if self._check("IDENT"):
            self._advance()
            if tok.value == "_":
                return ast.Wildcard(line=tok.line, col=tok.col)
            return ast.Var(tok.value, line=tok.line, col=tok.col)
        if self._check("NUMBER"):
            self._advance()
            return ast.Literal(tok.value, line=tok.line, col=tok.col)
        if self._check("STRING"):
            self._advance()
            parts = tok.value
            if len(parts) != 1 or parts[0][0] != "lit":
                self._fail("A 'match' pattern must be a plain string, not "
                           "an interpolated one", token=tok)
            return ast.Literal(parts[0][1], line=tok.line, col=tok.col)
        if self._check("KEYWORD", "true"):
            self._advance()
            return ast.Literal(True, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "false"):
            self._advance()
            return ast.Literal(False, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "null"):
            self._advance()
            return ast.Literal(None, line=tok.line, col=tok.col)
        self._fail("Expected a pattern in 'match' -- a literal, a name, "
                   "or '_'")

    def _if(self):
        tok = self._advance()  # if
        cond = self._expression()
        body = self._block()
        branches = [(cond, body)]
        else_body = None
        while True:
            # Allow newlines between } and elif/else.
            save = self.pos
            self._skip_newlines()
            if self._check("KEYWORD", "elif"):
                self._advance()
                c = self._expression()
                b = self._block()
                branches.append((c, b))
                continue
            if self._check("KEYWORD", "else"):
                self._advance()
                else_body = self._block()
                break
            self.pos = save
            break
        return ast.IfStmt(branches, else_body, line=tok.line, col=tok.col)

    def _for(self):
        tok = self._advance()  # for
        var_tok = self._expect("IDENT", what="a loop variable after 'for'")
        self._expect("KEYWORD", "in", "'in' after the loop variable")
        iterable = self._expression()
        body = self._block()
        return ast.ForStmt(var_tok.value, iterable, body,
                           line=tok.line, col=tok.col)

    def _expr_or_assign_stmt(self):
        tok = self.current
        expr = self._expression()
        if self._check("OP") and self.current.value in ("=", "+=", "-="):
            op = self._advance().value
            if not isinstance(expr, (ast.Var, ast.Index, ast.DerefExpr)):
                self._fail("Only a variable or an index can be assigned "
                           "to -- a dereference (*r) can be assigned to "
                           "as well, but nothing else",
                           token=tok)
            value = self._expression()
            return ast.AssignStmt(expr, op, value, line=tok.line, col=tok.col)
        return ast.ExprStmt(expr, line=tok.line, col=tok.col)

    # -- expressions ---------------------------------------------------
    def _expression(self):
        return self._or()

    def _or(self):
        left = self._and()
        while self._check("KEYWORD", "or"):
            tok = self._advance()
            right = self._and()
            left = ast.BinaryOp("or", left, right, line=tok.line, col=tok.col)
        return left

    def _and(self):
        left = self._equality()
        while self._check("KEYWORD", "and"):
            tok = self._advance()
            right = self._equality()
            left = ast.BinaryOp("and", left, right, line=tok.line, col=tok.col)
        return left

    def _equality(self):
        left = self._comparison()
        while self._check("OP") and self.current.value in ("==", "!="):
            tok = self._advance()
            right = self._comparison()
            left = ast.BinaryOp(tok.value, left, right,
                                line=tok.line, col=tok.col)
        return left

    def _comparison(self):
        left = self._additive()
        while self._check("OP") and self.current.value in ("<", "<=", ">", ">="):
            tok = self._advance()
            right = self._additive()
            left = ast.BinaryOp(tok.value, left, right,
                                line=tok.line, col=tok.col)
        return left

    def _additive(self):
        left = self._multiplicative()
        while self._check("OP") and self.current.value in ("+", "-"):
            tok = self._advance()
            right = self._multiplicative()
            left = ast.BinaryOp(tok.value, left, right,
                                line=tok.line, col=tok.col)
        return left

    def _multiplicative(self):
        left = self._unary()
        while self._check("OP") and self.current.value in ("*", "/", "%"):
            tok = self._advance()
            right = self._unary()
            left = ast.BinaryOp(tok.value, left, right,
                                line=tok.line, col=tok.col)
        return left

    def _unary(self):
        tok = self.current
        if self._check("KEYWORD", "await"):
            # `await <expr>`: binds like a unary operator, so
            # `await f()` awaits the call and `await a + b` means
            # `(await a) + b`.
            self._advance()
            return ast.AwaitExpr(self._unary(),
                                 line=tok.line, col=tok.col)
        if self._check("OP", "&"):
            # 4.0.0 borrow: `&x` (shared) or `&mut x` (mutable).
            # `mut` is contextual here, as after `let`: `&mut x` is
            # a mutable borrow, while `&mut` on its own borrows a
            # variable actually named `mut`.
            self._advance()
            mutable = False
            if self._check("IDENT", "mut"):
                nxt = (self.tokens[self.pos + 1]
                       if self.pos + 1 < len(self.tokens) else None)
                if nxt is not None and _starts_expression(nxt):
                    self._advance()
                    mutable = True
            return ast.BorrowExpr(self._unary(), mutable,
                                  line=tok.line, col=tok.col)
        if self._check("OP", "*"):
            # 7.0.0 deref: `*r` reads the value behind a borrow.
            # Binds like a unary operator, so `*r + 1` is `(*r) + 1`
            # and `&mut *r` is a reborrow of `r`.
            self._advance()
            return ast.DerefExpr(self._unary(),
                                 line=tok.line, col=tok.col)
        if self._check("OP", "**"):
            # 7.0.0: `**r` at an operand position is a double deref
            # (`*` `*r`). A binary `**` (power) never appears here --
            # it is consumed by `_power` after a left operand -- so
            # this cannot steal a power expression.
            self._advance()
            inner = ast.DerefExpr(self._unary(),
                                  line=tok.line, col=tok.col)
            return ast.DerefExpr(inner, line=tok.line, col=tok.col)
        if self._check("OP", "-"):
            self._advance()
            return ast.UnaryOp("-", self._unary(), line=tok.line, col=tok.col)
        if self._check("KEYWORD", "not"):
            self._advance()
            return ast.UnaryOp("not", self._unary(),
                               line=tok.line, col=tok.col)
        return self._power()

    def _power(self):
        left = self._postfix()
        if self._check("OP", "**"):
            tok = self._advance()
            right = self._unary()  # right-associative; allows negative exponents
            return ast.BinaryOp("**", left, right, line=tok.line, col=tok.col)
        return left

    def _postfix(self):
        expr = self._primary()
        while True:
            if self._check("PUNCT", "("):
                tok = self._advance()
                args = []
                self._skip_newlines()
                if not self._check("PUNCT", ")"):
                    while True:
                        args.append(self._expression())
                        self._skip_newlines()
                        if not self._match("PUNCT", ","):
                            break
                        self._skip_newlines()
                self._expect("PUNCT", ")", "')' after the arguments")
                expr = ast.Call(expr, args, line=tok.line, col=tok.col)
            elif self._check("PUNCT", "["):
                tok = self._advance()
                self._skip_newlines()
                index = self._expression()
                self._skip_newlines()
                self._expect("PUNCT", "]", "']' after the index")
                expr = ast.Index(expr, index, line=tok.line, col=tok.col)
            elif self._check("PUNCT", "."):
                dot = self._advance()
                name_tok = self._expect("IDENT",
                                        what="a method name after '.'")
                expr = ast.Member(expr, name_tok.value,
                                  line=dot.line, col=dot.col)
            else:
                break
        return expr

    def _primary(self):
        tok = self.current
        if self._check("NUMBER"):
            self._advance()
            return ast.Literal(tok.value, line=tok.line, col=tok.col)
        if self._check("STRING"):
            self._advance()
            return ast.StringLiteral(tok.value, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "true"):
            self._advance()
            return ast.Literal(True, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "false"):
            self._advance()
            return ast.Literal(False, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "null"):
            self._advance()
            return ast.Literal(None, line=tok.line, col=tok.col)
        if self._check("KEYWORD", "func"):
            # Anonymous function expression: func(a, b) { ... }
            return self._func_decl_or_expr_stmt()
        if self._check("KEYWORD", "async"):
            # Anonymous async function expression: async func(a) { ... }
            async_tok = self._advance()
            return self._func_decl_or_expr_stmt(is_async=True,
                                                async_tok=async_tok)
        if self._check("KEYWORD", "match"):
            return self._match_expr()
        if self._check("IDENT"):
            self._advance()
            return ast.Var(tok.value, line=tok.line, col=tok.col)
        if self._check("PUNCT", "("):
            self._advance()
            self._skip_newlines()
            expr = self._expression()
            self._skip_newlines()
            self._expect("PUNCT", ")", "')' to close the parentheses")
            return expr
        if self._check("PUNCT", "["):
            self._advance()
            elements = []
            self._skip_newlines()
            if not self._check("PUNCT", "]"):
                while True:
                    elements.append(self._expression())
                    self._skip_newlines()
                    if not self._match("PUNCT", ","):
                        break
                    self._skip_newlines()
            self._expect("PUNCT", "]", "']' to close the array")
            return ast.ArrayLiteral(elements, line=tok.line, col=tok.col)
        if self._check("PUNCT", "{"):
            self._advance()
            pairs = []
            self._skip_newlines()
            if not self._check("PUNCT", "}"):
                while True:
                    key_tok = self.current
                    if not self._check("STRING"):
                        self._fail("Dict keys must be strings, like "
                                   '{"name": "Kat"}', token=key_tok)
                    self._advance()
                    # Only a plain literal string may be a key.
                    if len(key_tok.value) != 1 or key_tok.value[0][0] != "lit":
                        self._fail("Dict keys must be plain strings, not "
                                   "interpolated ones", token=key_tok)
                    key = key_tok.value[0][1]
                    self._expect("PUNCT", ":", "':' after the dict key")
                    value = self._expression()
                    pairs.append((key, value))
                    self._skip_newlines()
                    if not self._match("PUNCT", ","):
                        break
                    self._skip_newlines()
            self._expect("PUNCT", "}", "'}' to close the dict")
            return ast.DictLiteral(pairs, line=tok.line, col=tok.col)
        if tok.kind == "EOF":
            self._fail("Unexpected end of file -- an expression was expected "
                       "here")
        self._fail("Unexpected %r -- an expression was expected here"
                   % (tok.value,))


def _starts_expression(tok):
    """True if *tok* can begin an expression (operand).

    Used for the contextual `mut` after `&`: `&mut x` is a mutable
    borrow, but `&mut` followed by anything that cannot start an
    expression borrows a variable actually named `mut`.
    """
    if tok.kind in ("IDENT", "NUMBER", "STRING"):
        return True
    if tok.kind == "PUNCT" and tok.value in ("(", "[", "{"):
        return True
    if tok.kind == "KEYWORD" and tok.value in (
            "func", "match", "true", "false", "null", "not"):
        return True
    if tok.kind == "OP" and tok.value == "-":
        return True
    if tok.kind == "OP" and tok.value in ("*", "&", "**"):
        # 7.0.0: `&mut *r` is a mutable reborrow (deref starts an
        # expression), and `&mut &x` is a mutable borrow of a borrow
        # (a checker error, but it parses as one borrow).
        return True
    return False


def parse(source, filename="<stdin>"):
    from .lexer import tokenize
    lines = source.split("\n")
    tokens = tokenize(source, filename)
    return Parser(tokens, filename, lines).parse()
