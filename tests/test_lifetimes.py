"""Explicit lifetime parameter tests (Kabir 7.0.0, Leap 3).

`func f<'a>(x: &'a number) -> &'a number` declares the lifetime
`'a`; the checker enforces it:

- A lifetime must be declared on the function before use, must be
  used at least once, and must not be duplicated.
- A returned `&'a T` needs a parameter carrying lifetime `'a`.
- Inside a function body a value must carry exactly the lifetime
  the return type (or annotation) requires: a borrow with a
  different named lifetime, or with no lifetime at all, does not
  satisfy `'a`.
- At a call site lifetimes are generic: each argument instantiates
  the callee's lifetime parameter. When the callee requires the
  SAME lifetime in two or more positions, all arguments there must
  carry the same named lifetime; the return lifetime is
  instantiated to the lifetime the arguments carried.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.checker import check_program
from kabir.errors import KabirError
from kabir.formatter import format_source
from kabir.lexer import tokenize
from kabir.parser import parse
from kabir import ast_nodes as ast


def check(source):
    """Type-check *source*; return None on success."""
    program = parse(source, "<test>")
    return check_program(program, filename="<test>",
                         lines=source.split("\n"))


def check_err(source, fragment):
    """Assert *source* fails checking with *fragment* in the error."""
    with unittest.TestCase().assertRaises(KabirError) as ctx:
        check(source)
    assert fragment in str(ctx.exception), (
        "expected %r in error, got: %s" % (fragment, ctx.exception))
    return ctx.exception


def fmt(source):
    return format_source(source, "test.kbr")


def run_vm(source):
    """Check, compile and run *source* on the VM; return stdout."""
    from contextlib import redirect_stdout
    from kabir.vm import VM
    check(source)
    buf = io.StringIO()
    with redirect_stdout(buf):
        VM(source, filename="<test>").run_source(source, filename="<test>")
    return buf.getvalue()


def run_interp(source):
    """Check and run *source* on the tree-walking interpreter."""
    from contextlib import redirect_stdout
    from kabir.interpreter import Interpreter
    check(source)
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(source, filename="<test>").run_source(
            source, filename="<test>")
    return buf.getvalue()


class TestLifetimeSyntax(unittest.TestCase):
    def test_lifetime_token(self):
        toks = tokenize("func f<'a>(x: &'a number) -> &'a number\n")
        kinds = [(t.kind, t.value) for t in toks]
        self.assertIn(("LIFETIME", "a"), kinds)

    def test_lifetime_params_on_decl(self):
        program = parse(
            "func f<'a, 'b>(x: &'a number, y: &'b number) "
            "-> &'a number { return x }\n", "<test>")
        decl = program.statements[0]
        self.assertEqual(decl.lifetimes, ["a", "b"])
        self.assertEqual(decl.param_types[0].full_name, "&'a number")
        self.assertEqual(decl.param_types[1].full_name, "&'b number")
        self.assertEqual(decl.return_type.full_name, "&'a number")

    def test_lifetime_mut_annotation(self):
        program = parse(
            "func f<'a>(x: &'a mut array) -> &'a mut array "
            "{ return x }\n", "<test>")
        decl = program.statements[0]
        self.assertEqual(decl.param_types[0].full_name, "&'a mut array")
        self.assertEqual(decl.return_type.full_name, "&'a mut array")

    def test_lifetime_on_let_annotation(self):
        program = parse("let r: &'a number = &x\n", "<test>")
        ann = program.statements[0].type_ann
        self.assertEqual(ann.full_name, "&'a number")
        self.assertEqual(ann.lifetime, "a")

    def test_anonymous_func_lifetimes(self):
        program = parse(
            "let f = func<'a>(x: &'a number) -> &'a number "
            "{ return x }\n", "<test>")
        decl = program.statements[0].value
        self.assertEqual(decl.lifetimes, ["a"])
        self.assertIsNone(decl.name)

    def test_no_lifetimes_is_empty_list(self):
        program = parse("func f(x: number) { return x }\n", "<test>")
        self.assertEqual(program.statements[0].lifetimes, [])

    def test_single_quote_string_still_errors(self):
        with self.assertRaises(KabirError) as ctx:
            tokenize("'hello'")
        self.assertIn("double quotes", str(ctx.exception))


class TestLifetimeFormatter(unittest.TestCase):
    def test_lifetime_func_formats_back(self):
        self.assertEqual(
            fmt("func first<'a>(x: &'a number) -> &'a number "
                "{ return x }\n"),
            "func first<'a>(x: &'a number) -> &'a number {\n"
            "    return x\n}\n")

    def test_two_lifetimes_format_back(self):
        self.assertEqual(
            fmt("func f<'a, 'b>(x: &'a number, y: &'b number) "
                "-> &'a number { return x }\n"),
            "func f<'a, 'b>(x: &'a number, y: &'b number) "
            "-> &'a number {\n    return x\n}\n")

    def test_lifetime_idempotent(self):
        source = ("func first<'a>(x: &'a number) -> &'a number {\n"
                  "    return x\n}\n")
        self.assertEqual(fmt(fmt(source)), source)


class TestLifetimeDeclarationRules(unittest.TestCase):
    def test_undeclared_lifetime_errors(self):
        check_err("func f(x: &'a number) -> &'a number { return x }\n",
                  "Lifetime 'a' is not declared")

    def test_undeclared_lifetime_in_return_errors(self):
        check_err("func f(x: &number) -> &'a number { return x }\n",
                  "Lifetime 'a' is not declared")

    def test_duplicate_lifetime_errors(self):
        check_err("func f<'a, 'a>(x: &'a number) -> &'a number "
                  "{ return x }\n",
                  "declared twice")

    def test_unused_lifetime_errors(self):
        check_err("func f<'a>(x: number) -> number { return x }\n",
                  "never used")

    def test_lifetime_used_only_in_return_counts(self):
        # 'a appears in the return type, so it is used -- but no
        # parameter carries it, so the carrier rule fires instead.
        check_err("func f<'a>(x: number) -> &'a number { return x }\n",
                  "no parameter carries lifetime 'a'")

    def test_return_lifetime_needs_carrier_param(self):
        check_err("func f<'a>(x: number, y: &number) -> &'a number "
                  "{ return y }\n",
                  "no parameter carries lifetime 'a'")

    def test_top_level_lifetime_annotation_errors(self):
        check_err("let r: &'a number = &x\n",
                  "Lifetime 'a' is not declared")

    def test_elided_return_still_needs_one_borrowed_param(self):
        # The 4.0.0 elision rule is unchanged for elided returns,
        # even when a named lifetime is also in play.
        check_err("func f<'a>(x: &'a number, y: &number) -> &number "
                  "{ return x }\n",
                  "exactly one borrowed parameter")

    def test_mut_param_carries_for_shared_return(self):
        # A `&'a mut T` parameter can back a `&'a T` return.
        check("func f<'a>(x: &'a mut number) -> &'a number "
              "{ return x }\n")

    def test_shared_param_cannot_carry_mut_return(self):
        check_err("func f<'a>(x: &'a number) -> &'a mut number "
                  "{ return x }\n",
                  "no parameter carries lifetime 'a'")


class TestLifetimeBodyRules(unittest.TestCase):
    def test_return_matching_lifetime_ok(self):
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n")

    def test_return_elided_borrow_for_named_return_errors(self):
        check_err("func bad<'a>(x: &'a number, y: &number) "
                  "-> &'a number { return y }\n",
                  "does not live as long as 'a'")

    def test_return_wrong_named_lifetime_errors(self):
        check_err("func bad<'a, 'b>(x: &'a number, y: &'b number) "
                  "-> &'a number { return y }\n",
                  "lifetime 'a'")

    def test_return_borrow_of_local_for_named_return_errors(self):
        check_err("func bad<'a>(x: &'a number) -> &'a number {\n"
                  "let mut y = 99\nreturn &y\n}\n",
                  "will not live long enough")

    def test_let_annotation_must_match_lifetime(self):
        check_err("func f<'a, 'b>(x: &'a number, y: &'b number) "
                  "-> &'a number {\n"
                  "let r: &'a number = y\nreturn r\n}\n",
                  "declared as &'a number")

    def test_let_annotation_matching_lifetime_ok(self):
        check("func f<'a>(x: &'a number) -> &'a number {\n"
              "let r: &'a number = x\nreturn r\n}\n")

    def test_deref_of_lifetime_param_ok(self):
        check("func f<'a>(x: &'a number) -> number { return *x }\n")


class TestLifetimeCallRules(unittest.TestCase):
    def test_call_with_elided_borrow_ok(self):
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n"
              "let mut v = 5\nlet r = &v\nprint(*first(r))\n")

    def test_call_with_fresh_borrow_ok(self):
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n"
              "let mut v = 5\nprint(*first(&v))\n")

    def test_call_with_named_borrow_ok(self):
        # A call instantiates the callee's lifetime from the
        # argument it is given.
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n"
              "func outer<'b>(r: &'b number) -> number {\n"
              "return *first(r)\n}\n")

    def test_return_through_nested_call_ok(self):
        # The nested call's return lifetime is instantiated to 'b,
        # so it satisfies outer's `-> &'b number`.
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n"
              "func outer<'b>(r: &'b number) -> &'b number {\n"
              "return first(r)\n}\n")

    def test_call_two_args_same_lifetime_ok(self):
        check("func pick<'a>(x: &'a number, y: &'a number) "
              "-> &'a number { return x }\n"
              "func outer<'b>(r: &'b number, s: &'b number) "
              "-> &'b number { return pick(r, s) }\n")

    def test_call_two_args_different_lifetimes_errors(self):
        check_err(
            "func pick<'a>(x: &'a number, y: &'a number) "
            "-> &'a number { return x }\n"
            "func caller<'a, 'b>(x: &'a number, y: &'b number) "
            "-> &'a number { return pick(x, y) }\n",
            "needs all its 'a' arguments to share one lifetime")

    def test_call_elided_and_named_args_agree_ok(self):
        # An elided argument agrees with any lifetime. The result
        # is used locally (not returned): returning it would hand
        # back a borrow that may be the local `&v`, which the
        # return-a-borrow rule rightly rejects.
        check("func pick<'a>(x: &'a number, y: &'a number) "
              "-> &'a number { return x }\n"
              "func outer<'b>(r: &'b number) -> number {\n"
              "let mut v = 1\nlet s = pick(r, &v)\nreturn *s\n}\n")

    def test_call_result_lifetime_flows_to_let(self):
        check("func first<'a>(x: &'a number) -> &'a number "
              "{ return x }\n"
              "func outer<'b>(r: &'b number) -> number {\n"
              "let s = first(r)\nreturn *s\n}\n")


class TestLifetimeRuntime(unittest.TestCase):
    def test_first_vm(self):
        out = run_vm("func first<'a>(x: &'a number) -> &'a number "
                     "{ return x }\n"
                     "let mut v = 41\nlet r = &v\nprint(*first(r))\n")
        self.assertEqual(out, "41\n")

    def test_first_interp(self):
        out = run_interp("func first<'a>(x: &'a number) -> &'a number "
                         "{ return x }\n"
                         "let mut v = 41\nlet r = &v\nprint(*first(r))\n")
        self.assertEqual(out, "41\n")

    def test_pick_vm(self):
        out = run_vm(
            "func pick<'a>(x: &'a number, y: &'a number) "
            "-> &'a number { return y }\n"
            "let mut a = 1\nlet mut b = 2\n"
            "print(*pick(&a, &b))\n")
        self.assertEqual(out, "2\n")

    def test_first_array_vm(self):
        out = run_vm("func first<'a>(x: &'a array) -> &'a array "
                     "{ return x }\n"
                     "let mut a = [7, 8]\nprint(first(&a)[0])\n")
        self.assertEqual(out, "7\n")

    def test_first_native(self):
        from kabir.codegen import _find_cc, run_native
        if _find_cc() is None:
            self.skipTest("no C compiler for native mode")
        from kabir.parser import parse as _parse
        from kabir.checker import check_program as _check
        source = ("func first<'a>(x: &'a number) -> &'a number "
                  "{ return x }\n"
                  "let mut v = 41\nlet r = &v\nprint(*first(r))\n")
        program = _parse(source, "<test>")
        _check(program, filename="<test>", lines=source.split("\n"))
        code, out, err = run_native(source, filename="<test>",
                                    program=program)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "41\n")


if __name__ == "__main__":
    unittest.main()
