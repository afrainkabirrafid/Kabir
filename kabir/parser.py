"""Recursive-descent parser for Kabir.

Produces an AST from the token stream. Precedence, lowest to highest:
or -> and -> equality (== !=) -> comparison (< <= > >=) -> additive
(+ -) -> multiplicative (* / %) -> power (** , right-associative) ->
unary (-, not) -> postfix (call, index) -> primary.
"""

from .errors import KabirError, error_at
from . import ast_nodes as ast


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
        self._advance()  # consume "}"
        return ast.Block(statements, line=tok.line, col=tok.col)

    def _statement(self):
        tok = self.current
        if self._check("KEYWORD", "let"):
            return self._let()
        if self._check("KEYWORD", "func"):
            # func declaration (named) or func expression statement
            return self._func_decl_or_expr_stmt()
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

    def _let(self):
        tok = self._advance()  # let
        name_tok = self._expect("IDENT", what="a variable name after 'let'")
        self._expect("OP", "=", "'=' after the variable name in 'let'")
        value = self._expression()
        return ast.LetStmt(name_tok.value, value, line=tok.line, col=tok.col)

    def _func_decl_or_expr_stmt(self):
        tok = self._advance()  # func
        name = None
        if self._check("IDENT"):
            name = self._advance().value
        self._expect("PUNCT", "(", "'(' after the function name")
        params = []
        if not self._check("PUNCT", ")"):
            while True:
                p = self._expect("IDENT", what="a parameter name")
                params.append(p.value)
                if not self._match("PUNCT", ","):
                    break
        self._expect("PUNCT", ")", "')' after the parameter list")
        body = self._block()
        return ast.FuncDecl(name, params, body, line=tok.line, col=tok.col)

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
            if not isinstance(expr, (ast.Var, ast.Index)):
                self._fail("Only a variable or an index can be assigned to",
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


def parse(source, filename="<stdin>"):
    from .lexer import tokenize
    lines = source.split("\n")
    tokens = tokenize(source, filename)
    return Parser(tokens, filename, lines).parse()
