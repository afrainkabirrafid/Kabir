"""Ownership lite tests (Kabir 3.0.0 Wave 3, design-doc section 3).

`let mut` is opt-in only: plain `let` stays mutable and is never
move-tracked. The checker enforces moves at compile time; the VM and
the interpreter are unchanged, and the REPL runs without the checker,
so use-after-move is not enforced there.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.checker import check_program
from kabir.errors import KabirError
from kabir.formatter import format_source
from kabir.parser import parse


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


class TestLetMutParses(unittest.TestCase):
    def test_let_mut_flag_on_ast(self):
        program = parse("let mut x = [1, 2]\n", "<test>")
        stmt = program.statements[0]
        self.assertTrue(stmt.mutable)
        self.assertEqual(stmt.name, "x")

    def test_plain_let_not_mutable(self):
        program = parse("let x = [1, 2]\n", "<test>")
        self.assertFalse(program.statements[0].mutable)

    def test_let_mut_with_type_annotation(self):
        program = parse("let mut x: array = [1]\n", "<test>")
        stmt = program.statements[0]
        self.assertTrue(stmt.mutable)
        self.assertEqual(stmt.type_ann.name, "array")

    def test_mut_as_ordinary_identifier(self):
        # `mut` is contextual: it still works as a variable name,
        # a function name, and a call everywhere else.
        program = parse("let mut = 5\nprint(mut)\n", "<test>")
        self.assertFalse(program.statements[0].mutable)
        self.assertEqual(program.statements[0].name, "mut")
        check("let mut = 5\nprint(mut)\n")
        check("func mut(x) { return x }\nprint(mut(1))\n")
        check("let mut = [1]\nlet y = mut\n")

    def test_mut_as_member_and_value_name(self):
        check("let mut = 5\nlet x = mut + 1\n")


class TestFormatterRoundTrip(unittest.TestCase):
    def test_let_mut_formats_back(self):
        self.assertEqual(fmt("let mut x = [1, 2]\n"),
                         "let mut x = [1, 2]\n")
        self.assertEqual(fmt("let   mut   x=[1,2]\n"),
                         "let mut x = [1, 2]\n")

    def test_let_mut_with_annotation_formats_back(self):
        self.assertEqual(fmt("let mut x: array = [1]\n"),
                         "let mut x: array = [1]\n")

    def test_plain_let_unchanged(self):
        self.assertEqual(fmt("let x = [1, 2]\n"), "let x = [1, 2]\n")

    def test_let_mut_idempotent(self):
        source = "let mut x = [1, 2]\nlet mut d: dict = {\"k\": 1}\n"
        self.assertEqual(fmt(fmt(source)), source)


class TestMoveOnAssign(unittest.TestCase):
    def test_move_on_let_assign_then_use_errors(self):
        check_err("let mut x = [1, 2]\nlet y = x\nprint(x)\n",
                  "used after move")

    def test_move_on_assign_stmt_then_use_errors(self):
        check_err("let mut x = [1, 2]\nlet y = [0]\ny = x\nprint(x)\n",
                  "used after move")

    def test_move_then_use_in_expression_errors(self):
        check_err("let mut x = [1]\nlet y = x\nlet z = x + [2]\n",
                  "used after move")

    def test_move_then_index_errors(self):
        check_err("let mut x = [1]\nlet y = x\nprint(x[0])\n",
                  "used after move")

    def test_dict_moves_too(self):
        check_err('let mut d = {"k": 1}\nlet e = d\nprint(d)\n',
                  "used after move")

    def test_moved_binding_can_be_moved_again_only_after_revive(self):
        # Moving an already-moved binding is itself a use after move.
        check_err("let mut x = [1]\nlet y = x\nlet z = x\n",
                  "used after move")

    def test_move_into_plain_let_still_moves(self):
        # The target need not be `mut`: assigning to any binding moves.
        check_err("let mut x = [1]\nlet y = x\nprint(x)\n",
                  "used after move")


class TestMoveOnCallAndReturn(unittest.TestCase):
    def test_move_on_call_arg_then_use_errors(self):
        check_err("let mut x = [1, 2]\nprint(len(x))\nprint(x)\n",
                  "used after move")

    def test_move_on_user_function_arg_then_use_errors(self):
        check_err("func f(a) { return a }\n"
                  "let mut x = [1]\nlet y = f(x)\nprint(x)\n",
                  "used after move")

    def test_move_on_return_then_use_errors(self):
        check_err("func f() {\nlet mut x = [1]\nreturn x\n"
                  "print(x)\n}\n",
                  "used after move")

    def test_call_without_move_target_still_passes(self):
        # Passing the moved-into binding is fine; the source is gone.
        check("func f(a) { return a }\n"
              "let mut x = [1]\nlet y = f(x)\nprint(y)\n")

    def test_method_receiver_is_not_a_move(self):
        # A method receiver is used, not moved: push() mutates x in
        # place, and a string method leaves the string usable.
        check('let mut s = "hi"\nprint(s.upper())\nprint(s)\n')
        check("let mut x = [1]\nlet y = x\nprint(y)\n")


class TestCopyValuesNeverMove(unittest.TestCase):
    def test_let_mut_number_never_moves(self):
        check("let mut n = 5\nlet m = n\nprint(n)\nprint(n + m)\n")

    def test_let_mut_string_never_moves(self):
        check('let mut s = "hi"\nlet t = s\nprint(s)\nprint(t)\n')

    def test_let_mut_bool_and_null_never_move(self):
        check("let mut b = true\nlet c = b\nprint(b)\n"
              "let mut u = null\nlet v = u\nprint(u)\n")

    def test_let_mut_copy_passed_and_returned(self):
        check("func f(a) { return a }\n"
              "let mut n = 5\nlet m = f(n)\nprint(n)\nprint(m)\n")


class TestPlainLetUnaffected(unittest.TestCase):
    def test_plain_let_array_never_moves(self):
        check("let x = [1, 2]\nlet y = x\nprint(x)\nprint(y)\n")

    def test_plain_let_dict_passed_and_returned(self):
        check("func f(a) { return a }\n"
              'let d = {"k": 1}\nlet e = f(d)\nprint(d)\nprint(e)\n')

    def test_plain_let_reassigned_freely(self):
        check("let x = [1]\nx = [2]\nlet y = x\nprint(x)\n")


class TestReassignmentRevives(unittest.TestCase):
    def test_reassign_after_move_revives(self):
        check("let mut x = [1]\nlet y = x\nx = [9]\nprint(x)\n")

    def test_reassign_after_move_can_move_again(self):
        check("let mut x = [1]\nlet y = x\nx = [9]\nlet z = x\n"
              "print(y)\nprint(z)\n")

    def test_reassign_after_move_then_use_after_second_move_errors(self):
        check_err("let mut x = [1]\nlet y = x\nx = [9]\n"
                  "let z = x\nprint(x)\n",
                  "used after move")

    def test_reassign_to_copy_unowns(self):
        # After `x = 5`, x holds a Copy value: no more move tracking.
        check("let mut x = [1]\nlet y = x\nx = 5\n"
              "let z = x\nprint(x)\nprint(z)\n")

    def test_compound_assign_on_moved_binding_errors(self):
        check_err("let mut x = [1]\nlet y = x\nx += [2]\n",
                  "used after move")

    def test_index_assign_on_moved_binding_errors(self):
        check_err("let mut x = [1]\nlet y = x\nx[0] = 9\n",
                  "used after move")


class TestMoveScopeAndUnknownTypes(unittest.TestCase):
    def test_move_inside_block_marks_moved_after(self):
        # Flow-insensitive: a move in a block still counts afterwards.
        check_err("let mut x = [1]\nif true {\nlet y = x\n}\nprint(x)\n",
                  "used after move")

    def test_shadowed_binding_is_separate(self):
        check("let mut x = [1]\nif true {\nlet mut x = [2]\n"
              "let y = x\nprint(y)\n}\nprint(x)\n")

    def test_unknown_type_never_tracked(self):
        # `f()` returns "any": no static type, so no move tracking.
        check("func f() { return [1] }\n"
              "let mut x = f()\nlet y = x\nprint(x)\n")

    def test_annotation_makes_unknown_value_owned(self):
        check_err("func f() { return [1] }\n"
                  "let mut x: array = f()\nlet y = x\nprint(x)\n",
                  "used after move")

    def test_non_move_uses_do_not_move(self):
        # Indexing, arithmetic, and conditions use x without moving it.
        check("let mut x = [1, 2]\nprint(x[0])\n"
              "let y = x + [3]\nprint(x)\nprint(y)\n")
        check("let mut x = [1]\nif x[0] == 1 {\nprint(x)\n}\n")


if __name__ == "__main__":
    unittest.main()
