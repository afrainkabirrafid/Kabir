"""Lexer for Kabir.

Turns source text into a stream of tokens. A string token's value is a
list of parts: ("lit", text) for plain text and ("expr", source) for an
interpolation expression inside { ... }.
"""

from .errors import KabirError, error_at

KEYWORDS = {
    "let", "func", "return", "if", "elif", "else", "while", "for", "in",
    "break", "continue", "true", "false", "null", "and", "or", "not",
}

SINGLE_OPS = set("+-*/%=<>")
MULTI_OPS = ("**", "==", "!=", "<=", ">=", "+=", "-=")
PUNCT = set("()[]{},;:")


class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind, value, line, col):
        self.kind = kind      # e.g. "NUMBER", "STRING", "IDENT", "KEYWORD",
                              # "OP", "PUNCT", "NEWLINE", "EOF"
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return "Token(%r, %r, %d:%d)" % (self.kind, self.value,
                                         self.line, self.col)


class Lexer:
    def __init__(self, source, filename="<stdin>"):
        self.source = source
        self.filename = filename
        self.lines = source.split("\n")
        self.pos = 0
        self.line = 1
        self.col = 1
        self.length = len(source)

    def _fail(self, message, line=None, col=None):
        raise error_at(message, self.filename, self.lines,
                       line if line is not None else self.line,
                       col if col is not None else self.col)

    def _peek(self, offset=0):
        i = self.pos + offset
        return self.source[i] if i < self.length else ""

    def _advance(self):
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def tokenize(self):
        tokens = []
        while self.pos < self.length:
            ch = self._peek()
            if ch in " \t\r":
                self._advance()
            elif ch == "#":
                while self.pos < self.length and self._peek() != "\n":
                    self._advance()
            elif ch == "\n":
                tokens.append(Token("NEWLINE", "\n", self.line, self.col))
                self._advance()
            elif ch.isdigit():
                tokens.append(self._number())
            elif ch == '"' or ch == "'":
                tokens.append(self._string())
            elif ch.isalpha() or ch == "_":
                tokens.append(self._ident())
            elif self.source[self.pos:self.pos + 2] in MULTI_OPS:
                op = self.source[self.pos:self.pos + 2]
                tokens.append(Token("OP", op, self.line, self.col))
                self._advance()
                self._advance()
            elif ch in SINGLE_OPS:
                tokens.append(Token("OP", ch, self.line, self.col))
                self._advance()
            elif ch in PUNCT:
                tokens.append(Token("PUNCT", ch, self.line, self.col))
                self._advance()
            else:
                self._fail("Unexpected character %r" % ch)
        tokens.append(Token("EOF", None, self.line, self.col))
        return tokens

    def _number(self):
        start_line, start_col = self.line, self.col
        start = self.pos
        while self.pos < self.length and self._peek().isdigit():
            self._advance()
        is_float = False
        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance()  # consume "."
            while self.pos < self.length and self._peek().isdigit():
                self._advance()
        text = self.source[start:self.pos]
        value = float(text) if is_float else int(text)
        return Token("NUMBER", value, start_line, start_col)

    def _ident(self):
        start_line, start_col = self.line, self.col
        start = self.pos
        while (self.pos < self.length
               and (self._peek().isalnum() or self._peek() == "_")):
            self._advance()
        text = self.source[start:self.pos]
        kind = "KEYWORD" if text in KEYWORDS else "IDENT"
        return Token(kind, text, start_line, start_col)

    def _string(self):
        # Kabir strings use double quotes. Single quotes are a friendly
        # error rather than a silent second string style.
        start_line, start_col = self.line, self.col
        quote = self._peek()
        if quote == "'":
            self._fail("Strings use double quotes, not single quotes -- "
                       'write "hello" instead of \'hello\'')
        self._advance()  # opening quote
        parts = []
        buf = []
        while True:
            if self.pos >= self.length:
                self._fail("Unterminated string -- add a closing \"",
                           line=start_line, col=start_col)
            ch = self._peek()
            if ch == '"':
                self._advance()
                break
            if ch == "\n":
                self._fail("Unterminated string -- strings cannot span "
                           "multiple lines", line=start_line, col=start_col)
            if ch == "\\":
                self._advance()
                esc = self._peek()
                mapping = {"n": "\n", "t": "\t", "r": "\r",
                           '"': '"', "\\": "\\"}
                if esc in mapping:
                    buf.append(mapping[esc])
                    self._advance()
                else:
                    self._fail("Unknown escape sequence \\%s -- use \\n, "
                               '\\", or \\\\' % esc)
                continue
            if ch == "{":
                # Interpolation: { expression }
                if buf:
                    parts.append(("lit", "".join(buf)))
                    buf = []
                self._advance()  # consume "{"
                expr_start = self.pos
                depth = 1
                in_str = False
                while self.pos < self.length and depth > 0:
                    c = self._peek()
                    if in_str:
                        if c == "\\":
                            self._advance()
                            if self.pos < self.length:
                                self._advance()
                            continue
                        if c == '"':
                            in_str = False
                    else:
                        if c == '"':
                            in_str = True
                        elif c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                break
                    self._advance()
                if depth != 0:
                    self._fail("Unterminated interpolation -- add a "
                               "closing }", line=start_line, col=start_col)
                expr_text = self.source[expr_start:self.pos].strip()
                if not expr_text:
                    self._fail("Empty interpolation {} -- put an "
                               "expression inside the braces",
                               line=start_line, col=start_col)
                parts.append(("expr", expr_text))
                self._advance()  # consume "}"
                continue
            buf.append(ch)
            self._advance()
        if buf:
            parts.append(("lit", "".join(buf)))
        return Token("STRING", parts, start_line, start_col)


def tokenize(source, filename="<stdin>"):
    return Lexer(source, filename).tokenize()
