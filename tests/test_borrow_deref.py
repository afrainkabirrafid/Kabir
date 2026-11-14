"""Deref + reborrowing tests (Kabir 7.0.0, Leap 3).

- `*r` dereferences a borrow: the operand must be a borrow type
  (`&T` / `&mut T`, with or without a lifetime) and the result is
  the plain type `T`. A deref is a compile-time no-op at runtime (a
  borrow IS the value), so it costs nothing.
- Reborrowing: `&mut *r` re-borrows a mutable borrow and `*s = 10`
  writes through one. The checker resolves the write to the place
  the borrow chain actually borrows, so a write through a reborrow
  of a number reaches the original variable.
- `&r` (no `*`) is still an error -- reborrowing goes through `*`.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.checker import check_program
from kabir.errors import KabirError
from kabir.formatter import format_source
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
    """Check, compile and run *source* on the VM; return stdout.

    Mirrors the CLI: the CHECKED program is compiled (not a fresh
    parse), so checker annotations on the AST -- a deref-write's
    resolved place -- reach the compiler."""
    from contextlib import redirect_stdout
    from kabir.compile import compile_program
    from kabir.vm import VM
    program = parse(source, "<test>")
    check_program(program, filename="<test>",
                  lines=source.split("\n"))
    code = compile_program(program, "<test>", source.split("\n"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        VM(source, filename="<test>").run_code(
            code, filename="<test>", lines=source.split("\n"))
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


class TestDerefSyntax(unittest.TestCase):
    def test_deref_node(self):
        program = parse("let r = &x\nprint(*r)\n", "<test>")
        deref = program.statements[1].expr.args[0]
        self.assertIsInstance(deref, ast.DerefExpr)
        self.assertIsInstance(deref.operand, ast.Var)
        self.assertEqual(deref.operand.name, "r")

    def test_deref_binds_like_unary(self):
        # *r + 1 is (*r) + 1, not *(r + 1).
        program = parse("let y = *r + 1\n", "<test>")
        value = program.statements[0].value
        self.assertIsInstance(value, ast.BinaryOp)
        self.assertEqual(value.op, "+")
        self.assertIsInstance(value.left, ast.DerefExpr)

    def test_deref_of_index(self):
        program = parse("let y = *a[0]\n", "<test>")
        deref = program.statements[0].value
        self.assertIsInstance(deref, ast.DerefExpr)
        self.assertIsInstance(deref.operand, ast.Index)

    def test_double_deref(self):
        program = parse("print(**r)\n", "<test>")
        outer = program.statements[0].expr.args[0]
        self.assertIsInstance(outer, ast.DerefExpr)
        self.assertIsInstance(outer.operand, ast.DerefExpr)
        # `* *r` (spaced) parses the same way.
        spaced = parse("print(* *r)\n", "<test>")
        self.assertIsInstance(
            spaced.statements[0].expr.args[0].operand, ast.DerefExpr)

    def test_power_and_multiply_unaffected(self):
        power = parse("let y = 2 ** 3\n", "<test>").statements[0].value
        self.assertIsInstance(power, ast.BinaryOp)
        self.assertEqual(power.op, "**")
        mult = parse("let y = 2 * 3\n", "<test>").statements[0].value
        self.assertIsInstance(mult, ast.BinaryOp)
        self.assertEqual(mult.op, "*")
        mixed = parse("let y = 2 * *r\n", "<test>").statements[0].value
        self.assertIsInstance(mixed, ast.BinaryOp)
        self.assertEqual(mixed.op, "*")
        self.assertIsInstance(mixed.right, ast.DerefExpr)

    def test_reborrow_node(self):
        program = parse("let s = &mut *r\n", "<test>")
        borrow = program.statements[0].value
        self.assertIsInstance(borrow, ast.BorrowExpr)
        self.assertTrue(borrow.mutable)
        self.assertIsInstance(borrow.expr, ast.DerefExpr)

    def test_shared_reborrow_node(self):
        program = parse("let s = &*r\n", "<test>")
        borrow = program.statements[0].value
        self.assertIsInstance(borrow, ast.BorrowExpr)
        self.assertFalse(borrow.mutable)
        self.assertIsInstance(borrow.expr, ast.DerefExpr)

    def test_deref_assign_target_node(self):
        program = parse("*s = 10\n", "<test>")
        stmt = program.statements[0]
        self.assertIsInstance(stmt, ast.AssignStmt)
        self.assertIsInstance(stmt.target, ast.DerefExpr)
        self.assertEqual(stmt.target.operand.name, "s")
        self.assertEqual(stmt.op, "=")

    def test_deref_compound_assign_target_node(self):
        program = parse("*s += 1\n", "<test>")
        stmt = program.statements[0]
        self.assertIsInstance(stmt.target, ast.DerefExpr)
        self.assertEqual(stmt.op, "+=")


class TestFormatterRoundTrip(unittest.TestCase):
    def test_deref_formats_back(self):
        self.assertEqual(fmt("let y = *r + 1\n"), "let y = *r + 1\n")

    def test_reborrow_formats_back(self):
        self.assertEqual(fmt("let s = &mut *r\n"), "let s = &mut *r\n")
        self.assertEqual(fmt("let s = &*r\n"), "let s = &*r\n")

    def test_deref_assign_formats_back(self):
        self.assertEqual(fmt("*s = 10\n"), "*s = 10\n")
        self.assertEqual(fmt("*s += 1\n"), "*s += 1\n")

    def test_double_deref_formats_back(self):
        self.assertEqual(fmt("print(**r)\n"), "print(**r)\n")

    def test_deref_idempotent(self):
        source = ("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
                  "*s = 10\nprint(*r)\nprint(x)\n")
        self.assertEqual(fmt(fmt(source)), source)


class TestDerefRules(unittest.TestCase):
    def test_deref_shared_borrow_in_arithmetic(self):
        check("let x = 5\nlet r = &x\nlet y = *r + 1\nprint(y)\n")

    def test_deref_mut_borrow_in_arithmetic(self):
        check("let mut x = 5\nlet r = &mut x\nlet y = *r + 1\nprint(y)\n")

    def test_deref_in_call_args(self):
        check("let x = [1, 2]\nlet r = &x\nprint(len(*r))\n")

    def test_deref_in_return(self):
        check("func f(a: &number) -> number { return *a }\n"
              "let n = 3\nprint(f(&n))\n")

    def test_deref_of_borrow_param_in_return(self):
        check("func f<'a>(a: &'a number) -> number { return *a }\n"
              "let n = 3\nprint(f(&n))\n")

    def test_deref_comparison(self):
        check("let x = 5\nlet r = &x\nprint(*r == 5)\n")

    def test_deref_string_method(self):
        check('let s = "hi"\nlet r = &s\nprint((*r).upper())\n')

    def test_deref_of_plain_number_errors(self):
        check_err("let x = 5\nprint(*x)\n",
                  "Cannot dereference a number")

    def test_deref_of_plain_array_errors(self):
        check_err("let x = [1]\nprint(*x)\n",
                  "Cannot dereference a array")

    def test_deref_of_borrow_in_len_now_works(self):
        # The 4.0.0 limit is gone: len(&x) is still a type error
        # (no auto-deref), but len(*r) works.
        check("let x = [1, 2]\nlet r = &x\nprint(len(*r))\n")
        check_err("let x = [1]\nprint(len(&x))\n",
                  "len() works on a string, array, or dict")

    def test_deref_of_unknown_is_any(self):
        check("func f() { return 5 }\n"
              "let r = f()\nprint(*r + 1)\n")

    def test_deref_in_match_arm(self):
        check("let x = 5\nlet r = &x\n"
              "let y = match 1 { 1 => *r + 1, _ => 0 }\nprint(y)\n")


class TestReborrowRules(unittest.TestCase):
    def test_mut_reborrow_ok(self):
        check("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
              "print(*s)\n")

    def test_shared_reborrow_ok(self):
        check("let x = 5\nlet r = &x\nlet s = &*r\nprint(*s)\n")

    def test_shared_reborrow_of_mut_borrow_ok(self):
        check("let mut x = 5\nlet r = &mut x\nlet s = &*r\n"
              "print(*s)\n")

    def test_mut_reborrow_of_shared_errors(self):
        check_err("let x = 5\nlet r = &x\nlet s = &mut *r\n",
                  "Cannot reborrow 'r' as mutable")

    def test_reborrow_of_non_borrow_errors(self):
        check_err("let x = 5\nlet s = &mut *x\n",
                  "Cannot reborrow 'x'")

    def test_second_reborrow_while_first_live_errors(self):
        check_err("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
                  "let t = &mut *r\nprint(*s)\nprint(*t)\n",
                  "while it is reborrowed")

    def test_use_of_original_while_reborrow_live_errors(self):
        check_err("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
                  "print(*r)\nprint(*s)\n",
                  "while it is reborrowed")

    def test_read_of_place_while_reborrow_live_errors(self):
        check_err("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
                  "print(x)\nprint(*s)\n",
                  "while it is mutably borrowed")

    def test_write_through_original_while_reborrow_live_errors(self):
        check_err("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
                  "*r = 20\nprint(*s)\n",
                  "while it is reborrowed")

    def test_use_of_original_after_reborrow_ends_ok(self):
        check("let mut x = 5\nlet r = &mut x\nlet s = &mut *r\n"
              "*s = 10\nprint(*r)\nprint(x)\n")

    def test_borrow_of_borrow_still_errors(self):
        # `&r` (no `*`) is still an error -- reborrowing goes
        # through `*`.
        check_err("let mut x = [1]\nlet r = &x\nlet s = &r\n",
                  "already holds a borrow")

    def test_move_while_reborrowed_errors(self):
        # The move itself is reported at the use of 'x': while the
        # mutable borrow (through the reborrow) is live, no other
        # access to the place is allowed.
        check_err("let mut x = [1]\nlet r = &mut x\n"
                  "let s = &mut *r\nlet y = x\nprint(*s)\n",
                  "while it is mutably borrowed")


class TestDerefAssignRules(unittest.TestCase):
    def test_write_through_mut_borrow_ok(self):
        check("let mut x = 5\nlet r = &mut x\n*r = 10\nprint(x)\n")

    def test_write_through_shared_borrow_errors(self):
        check_err("let x = 5\nlet r = &x\n*r = 10\n",
                  "Cannot write through '*r'")

    def test_write_through_non_borrow_errors(self):
        check_err("let x = 5\n*x = 10\n",
                  "Cannot write through '*x'")

    def test_compound_write_through_ok(self):
        check("let mut x = 5\nlet r = &mut x\n*r += 3\nprint(x)\n")

    def test_compound_write_through_bad_type_errors(self):
        check_err('let mut x = 5\nlet r = &mut x\n*r += "a"\n',
                  "Cannot add a number and a string")

    def test_write_through_element_borrow_ok(self):
        check("let mut a = [1, 2]\nlet m = &mut a[0]\n*m = 9\n"
              "print(a)\n")


class TestDerefRuntime(unittest.TestCase):
    def test_deref_vm(self):
        out = run_vm("let x = 5\nlet r = &x\nprint(*r)\n"
                     "let y = *r + 1\nprint(y)\n")
        self.assertEqual(out, "5\n6\n")

    def test_deref_interp(self):
        out = run_interp("let x = 5\nlet r = &x\nprint(*r)\n"
                         "let y = *r + 1\nprint(y)\n")
        self.assertEqual(out, "5\n6\n")

    def test_deref_in_function_vm(self):
        out = run_vm("func f(a: &number) -> number { return *a + 1 }\n"
                     "let n = 41\nprint(f(&n))\n")
        self.assertEqual(out, "42\n")

    def test_reborrow_write_through_vm(self):
        out = run_vm("let mut x = 5\nlet r = &mut x\n"
                     "let s = &mut *r\n*s = 10\nprint(x)\n")
        self.assertEqual(out, "10\n")

    def test_reborrow_write_through_interp(self):
        out = run_interp("let mut x = 5\nlet r = &mut x\n"
                         "let s = &mut *r\n*s = 10\nprint(x)\n")
        self.assertEqual(out, "10\n")

    def test_reborrow_chain_write_through_vm(self):
        out = run_vm("let mut x = 5\nlet r = &mut x\n"
                     "let s = &mut *r\nlet t = &mut *s\n*t = 7\n"
                     "print(x)\n")
        self.assertEqual(out, "7\n")

    def test_reborrow_chain_write_through_interp(self):
        out = run_interp("let mut x = 5\nlet r = &mut x\n"
                         "let s = &mut *r\nlet t = &mut *s\n*t = 7\n"
                         "print(x)\n")
        self.assertEqual(out, "7\n")

    def test_compound_write_through_vm(self):
        out = run_vm("let mut x = 5\nlet r = &mut x\n*r += 3\n"
                     "print(x)\n")
        self.assertEqual(out, "8\n")

    def test_compound_write_through_interp(self):
        out = run_interp("let mut x = 5\nlet r = &mut x\n*r += 3\n"
                         "print(x)\n")
        self.assertEqual(out, "8\n")

    def test_write_through_element_borrow_vm(self):
        out = run_vm("let mut a = [1, 2]\nlet m = &mut a[0]\n*m = 9\n"
                     "print(a)\n")
        self.assertEqual(out, "[9, 2]\n")

    def test_write_through_element_borrow_interp(self):
        out = run_interp("let mut a = [1, 2]\nlet m = &mut a[0]\n*m = 9\n"
                         "print(a)\n")
        self.assertEqual(out, "[9, 2]\n")

    def test_original_usable_after_reborrow_ends_vm(self):
        out = run_vm("let mut x = 5\nlet r = &mut x\n"
                     "let s = &mut *r\n*s = 10\nprint(*r)\nprint(x)\n")
        self.assertEqual(out, "10\n10\n")

    def test_deref_native(self):
        from kabir.codegen import _find_cc, run_native
        if _find_cc() is None:
            self.skipTest("no C compiler for native mode")
        from kabir.parser import parse as _parse
        from kabir.checker import check_program as _check
        source = ("let mut x = 5\nlet r = &mut x\n"
                  "let s = &mut *r\n*s = 10\nprint(x)\n")
        program = _parse(source, "<test>")
        _check(program, filename="<test>", lines=source.split("\n"))
        code, out, err = run_native(source, filename="<test>",
                                    program=program)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "10\n")


if __name__ == "__main__":
    unittest.main()
