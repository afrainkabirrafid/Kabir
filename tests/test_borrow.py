"""Borrow checker tests (Kabir 4.0.0 Wave 0, design-doc section 4).

Honest subset -- lifetime elision only, NO explicit `'a` lifetime
parameters. A borrow is a compile-time discipline only: at runtime
`&x` IS the value, so borrowing costs nothing and the REPL (which
runs without the checker) does not enforce it.

Rules enforced here:
- `&x` shared borrow, `&mut x` mutable borrow; `&mut x` needs a
  `let mut` binding.
- While a mutable borrow is live: no other access to the place.
  While shared borrows are live: no mutation or move of the place.
- A borrowed place cannot be moved while borrowed (owned bindings
  only -- plain `let` never moves).
- NLL-lite: a borrow ends at its last use. A borrow whose holder is
  never used again ends at once -- so a mutation AFTER the holder's
  last use is fine, and an unused borrow never blocks anything.
- A borrow of `a[i]` borrows the whole container.
- A function returning a borrow must take exactly one borrowed
  parameter (lifetime elision); returning a borrow of a local is an
  error -- only a borrow of a borrowed parameter (or of a top-level
  binding) may be returned.
- No reborrowing: borrowing a borrow-typed value is an error.
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


class TestBorrowSyntax(unittest.TestCase):
    def test_shared_borrow_node(self):
        program = parse("let r = &x\n", "<test>")
        stmt = program.statements[0]
        self.assertIsInstance(stmt.value, ast.BorrowExpr)
        self.assertFalse(stmt.value.mutable)
        self.assertIsInstance(stmt.value.expr, ast.Var)
        self.assertEqual(stmt.value.expr.name, "x")

    def test_mut_borrow_node(self):
        program = parse("let m = &mut x\n", "<test>")
        stmt = program.statements[0]
        self.assertIsInstance(stmt.value, ast.BorrowExpr)
        self.assertTrue(stmt.value.mutable)

    def test_borrow_of_index(self):
        program = parse("let r = &x[0]\n", "<test>")
        borrow = program.statements[0].value
        self.assertIsInstance(borrow, ast.BorrowExpr)
        self.assertIsInstance(borrow.expr, ast.Index)

    def test_borrow_in_call_args(self):
        program = parse("print(&x)\n", "<test>")
        call = program.statements[0].expr
        self.assertIsInstance(call.args[0], ast.BorrowExpr)

    def test_borrow_binds_like_unary(self):
        # &x[0] is a borrow of x[0], not (&x)[0].
        program = parse("let r = &x[0]\n", "<test>")
        self.assertIsInstance(program.statements[0].value.expr,
                              ast.Index)

    def test_borrow_of_parenthesised(self):
        program = parse("let r = &(x)\n", "<test>")
        borrow = program.statements[0].value
        self.assertIsInstance(borrow, ast.BorrowExpr)
        self.assertEqual(borrow.expr.name, "x")

    def test_bare_ampersand_is_parse_error(self):
        with self.assertRaises(KabirError):
            parse("let r = &\n", "<test>")

    def test_mut_as_borrowed_variable(self):
        # `&mut` on its own borrows a variable actually named `mut`.
        program = parse("let mut = 5\nlet r = &mut\n", "<test>")
        borrow = program.statements[1].value
        self.assertIsInstance(borrow, ast.BorrowExpr)
        self.assertFalse(borrow.mutable)
        self.assertEqual(borrow.expr.name, "mut")
        check("let mut = 5\nlet r = &mut\nprint(r)\n")

    def test_borrow_type_annotations_parse(self):
        program = parse(
            "func f(a: &number, b: &mut array) -> &number { return a }\n",
            "<test>")
        decl = program.statements[0]
        self.assertEqual(decl.param_types[0].full_name, "&number")
        self.assertEqual(decl.param_types[1].full_name, "&mut array")
        self.assertEqual(decl.return_type.full_name, "&number")
        let = parse("let r: &number = &x\n", "<test>").statements[0]
        self.assertEqual(let.type_ann.full_name, "&number")


class TestFormatterRoundTrip(unittest.TestCase):
    def test_shared_borrow_formats_back(self):
        self.assertEqual(fmt("let r = &x\n"), "let r = &x\n")
        self.assertEqual(fmt("let   r=&x\n"), "let r = &x\n")

    def test_mut_borrow_formats_back(self):
        self.assertEqual(fmt("let m = &mut x\n"), "let m = &mut x\n")
        self.assertEqual(fmt("let m=&mut  x\n"), "let m = &mut x\n")

    def test_borrow_of_index_formats_back(self):
        self.assertEqual(fmt("let r = &x[0]\n"), "let r = &x[0]\n")

    def test_borrow_annotations_format_back(self):
        self.assertEqual(
            fmt("func f(a: &number) -> &number { return a }\n"),
            "func f(a: &number) -> &number {\n    return a\n}\n")
        self.assertEqual(fmt("let r: &mut array = &mut x\n"),
                         "let r: &mut array = &mut x\n")

    def test_borrow_idempotent(self):
        source = ("let mut x = [1, 2]\nlet r = &x\n"
                  "let m = &mut x\nprint(r)\nprint(m)\n")
        self.assertEqual(fmt(fmt(source)), source)


class TestSharedBorrowRules(unittest.TestCase):
    def test_read_while_shared_borrow_live(self):
        check("let mut x = [1, 2]\nlet r = &x\nprint(x[0])\nprint(r)\n")

    def test_two_shared_borrows(self):
        check("let mut x = [1]\nlet r = &x\nlet s = &x\n"
              "print(r)\nprint(s)\n")

    def test_assign_while_shared_borrow_live_errors(self):
        check_err("let mut x = [1, 2]\nlet r = &x\nx = [9]\nprint(r)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_assign_after_last_use_ok(self):
        check("let mut x = [1, 2]\nlet r = &x\nprint(r)\nx = [9]\n")

    def test_unused_borrow_never_blocks(self):
        # NLL: a borrow whose holder is never used ends at once.
        check("let mut x = [1]\nlet r = &x\nx = [9]\n")

    def test_index_assign_while_shared_borrow_live_errors(self):
        check_err("let mut x = [1]\nlet r = &x\nx[0] = 9\nprint(r)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_compound_assign_while_borrowed_errors(self):
        check_err("let mut x = [1]\nlet m = &mut x\nx += [2]\n"
                  "print(m)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_borrow_in_branch_extends_liveness(self):
        check_err("let mut x = [1]\nlet r = &x\nif true {\n"
                  "print(r)\n}\nx = [9]\nprint(r)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_borrow_used_only_inside_branch_ends_at_branch(self):
        check("let mut x = [1]\nlet r = &x\nif true {\n"
              "print(r)\n}\nx = [9]\n")

    def test_borrow_of_element_borrows_whole_container(self):
        check_err("let mut x = [1]\nlet r = &x[0]\nx[0] = 9\n"
                  "print(r)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_push_while_shared_borrow_live_errors(self):
        check_err("let x = [1]\nlet r = &x\npush(x, 2)\nprint(r)\n",
                  "Cannot change 'x' while it is borrowed")

    def test_push_after_last_use_ok(self):
        check("let x = [1]\nlet r = &x\nprint(r)\npush(x, 2)\n")


class TestMutBorrowRules(unittest.TestCase):
    def test_mut_borrow_needs_let_mut(self):
        check_err("let x = [1]\nlet m = &mut x\n",
                  "not declared with 'let mut'")

    def test_mut_borrow_of_let_mut_ok(self):
        check("let mut x = [1]\nlet m = &mut x\nprint(m)\n")

    def test_mut_borrow_of_copy_let_mut_ok(self):
        check("let mut n = 5\nlet m = &mut n\nprint(m)\n")

    def test_read_while_mut_borrow_live_errors(self):
        check_err("let mut x = [1]\nlet m = &mut x\nprint(x)\n"
                  "print(m)\n",
                  "Cannot use 'x' while it is mutably borrowed")

    def test_mutate_through_mut_borrow_ok(self):
        check("let mut x = [1]\nlet m = &mut x\nm[0] = 9\nprint(m)\n")

    def test_second_mut_borrow_errors(self):
        check_err("let mut x = [1]\nlet m = &mut x\nlet n = &mut x\n"
                  "print(m)\nprint(n)\n",
                  "already borrowed")

    def test_shared_borrow_while_mut_live_errors(self):
        check_err("let mut x = [1]\nlet m = &mut x\nlet r = &x\n"
                  "print(m)\nprint(r)\n",
                  "already mutably borrowed")

    def test_mut_borrow_while_shared_live_errors(self):
        check_err("let mut x = [1]\nlet r = &x\nlet m = &mut x\n"
                  "print(r)\nprint(m)\n",
                  "already borrowed")

    def test_unused_mut_borrow_never_blocks(self):
        check("let mut x = [1]\nlet m = &mut x\nprint(x)\n")

    def test_read_after_mut_borrow_ends_ok(self):
        check("let mut x = [1]\nlet m = &mut x\nprint(m)\nprint(x)\n")

    def test_mut_borrow_of_param_errors(self):
        # Params cannot be `let mut` (3.0.0 decision), so a mutable
        # borrow of a parameter -- including a borrowed one -- is
        # not supported.
        check_err("func f(a: number) { let m = &mut a }\n",
                  "not declared with 'let mut'")
        # A borrowed parameter already holds a borrow, so the
        # reborrowing error fires first for it.
        check_err("func f(a: &number) -> &number {\n"
                  "let m = &mut a\nreturn a\n}\n",
                  "already holds a borrow")


class TestBorrowAndMove(unittest.TestCase):
    def test_move_while_borrowed_errors(self):
        check_err("let mut x = [1]\nlet r = &x\nlet y = x\nprint(r)\n",
                  "Cannot move 'x' while it is borrowed")

    def test_move_before_borrow_still_fine(self):
        # The borrow is dead (r unused), so the move is fine.
        check("let mut x = [1]\nlet r = &x\nlet y = x\nprint(y)\n")

    def test_borrow_of_moved_value_errors(self):
        check_err("let mut x = [1]\nlet y = x\nlet r = &x\n",
                  "used after move")

    def test_call_arg_move_while_borrowed_errors(self):
        check_err("func f(a) { return a }\nlet mut x = [1]\n"
                  "let r = &x\nlet y = f(x)\nprint(r)\n",
                  "Cannot move 'x' while it is borrowed")

    def test_print_moves_owned_value_while_borrowed(self):
        # Passing an owned value to any function moves it (3.0.0),
        # so print(x) while x is borrowed is a move error.
        check_err("let mut x = [1, 2]\nlet r = &x\nprint(x)\n"
                  "print(r)\n",
                  "Cannot move 'x' while it is borrowed")

    def test_reassign_after_move_then_borrow_ok(self):
        check("let mut x = [1]\nlet y = x\nx = [9]\nlet r = &x\n"
              "print(r)\n")

    def test_plain_let_never_moves_so_borrow_never_blocks_move(self):
        # Plain `let` bindings are never move-tracked; aliasing is
        # the norm for them, so borrowing one never blocks an
        # assignment to another binding.
        check("let x = [1]\nlet r = &x\nlet y = x\nprint(r)\n"
              "print(y)\n")


class TestBorrowHolders(unittest.TestCase):
    def test_alias_extends_borrow(self):
        # let y = r makes y a holder too: the borrow lives until
        # y's last use.
        check_err("let mut x = [1]\nlet r = &x\nlet y = r\nx = [9]\n"
                  "print(y)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_alias_used_before_mutation_ok(self):
        check("let mut x = [1]\nlet r = &x\nlet y = r\nprint(y)\n"
              "x = [9]\n")

    def test_borrow_in_array_tracked_via_holder(self):
        check_err("let mut x = [1]\nlet a = [&x]\nx = [9]\n"
                  "print(a)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_match_arm_yielding_borrow_extends_it(self):
        check_err("let mut x = [1]\nlet r = &x\n"
                  "let y = match 1 { 1 => r, _ => [0] }\n"
                  "x = [9]\nprint(y)\n",
                  "Cannot assign to 'x' while it is borrowed")

    def test_reassigning_holder_ends_borrow(self):
        check("let mut x = [1]\nlet r = &x\nr = 5\nx = [9]\n"
              "print(x)\n")

    def test_reassigning_holder_to_new_borrow(self):
        check("let mut x = [1]\nlet y = [2]\nlet r = &x\n"
              "r = &y\nprint(r)\nx = [9]\n")

    def test_reassign_holder_to_array_holding_itself(self):
        # r = [r] still references the old borrow, so it stays live.
        check_err("let mut x = [1]\nlet r = &x\nr = [r]\nx = [9]\n"
                  "print(r)\n",
                  "Cannot assign to 'x' while it is borrowed")


class TestBorrowAnnotationsAndElision(unittest.TestCase):
    def test_borrow_param_accepted(self):
        check("func f(a: &number) -> &number { return a }\n"
              "let n = 3\nprint(f(&n))\n")

    def test_plain_value_arg_to_borrow_param_errors(self):
        # No auto-borrow: a plain number is not a &number.
        check_err("func f(a: &number) -> &number { return a }\n"
                  "print(f(3))\n",
                  "should be a &number")

    def test_mut_borrow_coerces_to_shared(self):
        check("func f(a: &number) -> &number { return a }\n"
              "let mut n = 3\nprint(f(&mut n))\n")

    def test_shared_borrow_does_not_coerce_to_mut(self):
        check_err("func f(a: &mut number) -> &mut number { return a }\n"
                  "let n = 3\nprint(f(&n))\n",
                  "should be a &mut number")

    def test_borrow_annotation_on_let(self):
        check("let mut x = [1]\nlet r: &array = &x\nprint(r)\n")

    def test_mut_borrow_annotation_on_let(self):
        check("let mut x = [1]\nlet m: &mut array = &mut x\nprint(m)\n")

    def test_plain_value_for_borrow_annotation_errors(self):
        check_err("let n: &number = 5\n",
                  "declared as &number")

    def test_return_plain_for_borrow_return_errors(self):
        check_err("func f(a: &number) -> &number { return 5 }\n",
                  "should return a &number")

    def test_elision_needs_exactly_one_borrowed_param(self):
        check_err("func f() -> &number { return 5 }\n",
                  "exactly one borrowed parameter")
        check_err("func f(a: number) -> &number { return a }\n",
                  "exactly one borrowed parameter")
        check_err("func f(a: &number, b: &number) -> &number {\n"
                  "return a\n}\n",
                  "exactly one borrowed parameter")

    def test_elision_ok_with_one_borrowed_param(self):
        check("func f(a: &number, b: number) -> &number { return a }\n")

    def test_return_borrow_of_local_errors(self):
        check_err("func f(a: &number) -> &number {\n"
                  "let n = 5\nreturn &n\n}\n",
                  "will not live long enough")

    def test_return_borrowed_param_ok(self):
        check("func f(a: &number) -> &number { return a }\n")

    def test_return_alias_of_borrowed_param_ok(self):
        check("func f(a: &number) -> &number {\n"
              "let r = a\nreturn r\n}\n")

    def test_return_borrow_of_top_level_ok(self):
        check("let g = 7\n"
              "func f(a: &number) -> &number { return &g }\n")

    def test_return_local_alias_holding_borrow_errors(self):
        check_err("func f(a: &number) -> &number {\n"
                  "let mut x = [1]\nlet r = &x\nreturn r\n}\n",
                  "will not live long enough")

    def test_borrow_param_may_be_read(self):
        # A borrowed value can be printed and compared, but not
        # used in arithmetic: there is no dereference operator in
        # this subset, so `a + 1` would be a type error.
        check("func f(a: &number) -> &number {\n"
              "print(a)\nprint(a == 3)\nreturn a\n}\n")


class TestBorrowLimits(unittest.TestCase):
    def test_borrow_of_literal_errors(self):
        check_err("let r = &[1]\n", "Can only borrow")
        check_err("let r = &5\n", "Can only borrow")

    def test_borrow_of_call_result_errors(self):
        check_err("func f() { return [1] }\nlet r = &f()\n",
                  "Can only borrow")

    def test_borrow_of_borrow_errors(self):
        check_err("let mut x = [1]\nlet r = &x\nlet s = &r\n",
                  "already holds a borrow")
        check_err("let mut x = [1]\nlet s = & &x\n",
                  "Cannot borrow a borrow")

    def test_borrow_of_borrowed_param_errors(self):
        # Single-level reborrowing only: use the param directly.
        check_err("func f(a: &number) -> &number {\n"
                  "let r = &a\nreturn a\n}\n",
                  "already holds a borrow")

    def test_len_of_borrow_is_type_error(self):
        # No auto-deref: a borrow is not the plain type. Pass the
        # original binding instead.
        check_err("let x = [1]\nprint(len(&x))\n",
                  "len() works on a string, array, or dict")

    def test_unknown_type_borrow_is_any(self):
        # Borrowing a value of unknown type never errors.
        check("func f() { return [1] }\n"
              "let x = f()\nlet r = &x\nprint(r)\n")

    def test_borrow_inside_nested_function_is_local_to_it(self):
        # A borrow created inside a nested function does not
        # constrain the outer body (borrows never cross functions).
        check("let mut x = [1]\n"
              "let f = func() { let r = &x\nreturn r }\n"
              "x = [9]\nprint(x)\n")


class TestBorrowRuntime(unittest.TestCase):
    def test_borrow_evaluates_as_value_vm(self):
        # The shared borrow must end (last use of r) before the
        # mutable borrow is created -- the checker enforces that.
        out = run_vm("let mut x = [1, 2]\nlet r = &x\nprint(r)\n"
                     "let m = &mut x\nprint(m)\n"
                     "print(&x[0])\n")
        self.assertEqual(out, "[1, 2]\n[1, 2]\n1\n")

    def test_borrow_evaluates_as_value_interp(self):
        out = run_interp("let mut x = [1, 2]\nlet r = &x\nprint(r)\n"
                         "let m = &mut x\nprint(m)\n"
                         "print(&x[0])\n")
        self.assertEqual(out, "[1, 2]\n[1, 2]\n1\n")

    def test_borrow_through_function_vm(self):
        # f returns the borrowed number itself at runtime.
        out = run_vm("func f(a: &number) -> &number { return a }\n"
                     "let n = 41\nprint(f(&n))\n")
        self.assertEqual(out, "41\n")

    def test_mut_borrow_mutation_visible_vm(self):
        # A borrow IS the value: writing through it changes x.
        out = run_vm("let mut x = [1]\nlet m = &mut x\n"
                     "m[0] = 9\nprint(x)\n")
        self.assertEqual(out, "[9]\n")


if __name__ == "__main__":
    unittest.main()
