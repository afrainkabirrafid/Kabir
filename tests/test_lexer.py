"""Lexer tests: token types, strings, numbers, comments, errors."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.lexer import tokenize


def kinds(source):
    return [(t.kind, t.value) for t in tokenize(source) if t.kind != "EOF"]


class TestLexer(unittest.TestCase):
    def test_keywords_and_idents(self):
        toks = kinds("let x = 1")
        self.assertEqual(toks[0], ("KEYWORD", "let"))
        self.assertEqual(toks[1], ("IDENT", "x"))
        self.assertEqual(toks[2], ("OP", "="))
        self.assertEqual(toks[3], ("NUMBER", 1))

    def test_all_keywords_recognised(self):
        for word in ("let func return if elif else while for in break "
                     "continue true false null and or not").split():
            self.assertEqual(tokenize(word)[0].kind, "KEYWORD", word)

    def test_int_and_float_numbers(self):
        self.assertEqual(tokenize("42")[0].value, 42)
        self.assertIsInstance(tokenize("42")[0].value, int)
        self.assertEqual(tokenize("3.14")[0].value, 3.14)
        self.assertIsInstance(tokenize("3.14")[0].value, float)

    def test_operators(self):
        toks = kinds("a ** b == c != d <= e >= f += g -= h % i")
        ops = [v for k, v in toks if k == "OP"]
        self.assertEqual(ops, ["**", "==", "!=", "<=", ">=", "+=", "-=", "%"])

    def test_plain_string(self):
        tok = tokenize('"hello"')[0]
        self.assertEqual(tok.kind, "STRING")
        self.assertEqual(tok.value, [("lit", "hello")])

    def test_string_escapes(self):
        tok = tokenize(r'"a\nb\"c\\d"')[0]
        self.assertEqual(tok.value, [("lit", 'a\nb"c\\d')])

    def test_string_interpolation_parts(self):
        tok = tokenize('"Hello, {name}! You are {age + 1}."')[0]
        self.assertEqual(tok.value, [("lit", "Hello, "),
                                     ("expr", "name"),
                                     ("lit", "! You are "),
                                     ("expr", "age + 1"),
                                     ("lit", ".")])

    def test_comments_ignored(self):
        toks = kinds("let x = 1 # set x\nlet y = 2")
        self.assertNotIn(("#", None), toks)
        self.assertEqual([v for k, v in toks if k == "NUMBER"], [1, 2])

    def test_newlines_are_tokens(self):
        toks = tokenize("1\n2")
        self.assertEqual([t.kind for t in toks],
                         ["NUMBER", "NEWLINE", "NUMBER", "EOF"])

    def test_unterminated_string_errors(self):
        with self.assertRaises(KabirError) as ctx:
            tokenize('"never closed')
        self.assertIn("Unterminated string", str(ctx.exception))

    def test_single_quote_string_errors(self):
        with self.assertRaises(KabirError) as ctx:
            tokenize("'hello'")
        self.assertIn("double quotes", str(ctx.exception))

    def test_unexpected_character_errors(self):
        with self.assertRaises(KabirError):
            tokenize("let x = @")

    def test_line_numbers(self):
        toks = tokenize("let a = 1\nlet b = 2")
        b = [t for t in toks if t.value == "b"][0]
        self.assertEqual(b.line, 2)


if __name__ == "__main__":
    unittest.main()
