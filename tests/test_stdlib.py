"""Standard-library tests: random (5), math (7), pi/e globals."""

import io
import math
import os
import random
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse


def run(source, inputs=None):
    interp = Interpreter(source, filename="<test>")
    buf = io.StringIO()
    if inputs is not None:
        import builtins as _b
        it = iter(inputs)
        orig = _b.input
        _b.input = lambda prompt="": next(it)
        try:
            with redirect_stdout(buf):
                interp.run_source(source, filename="<test>")
        finally:
            _b.input = orig
    else:
        with redirect_stdout(buf):
            interp.run_source(source, filename="<test>")
    return buf.getvalue()


def value(source):
    interp = Interpreter(source, filename="<test>")
    return _value_on(interp, source)


def _value_on(interp, source):
    program = parse(source, "<test>")
    interp.source = source
    interp.lines = source.split("\n")
    last = None
    for stmt in program.statements:
        last = interp.run_stmt(stmt)
    return last


def _seeded_randint_sequence(seed, count):
    """randint(1, 100) sequence from a fresh interpreter seeded once."""
    interp = Interpreter("", filename="<test>")
    _value_on(interp, "seed(%d)" % seed)
    return [_value_on(interp, "randint(1, 100)") for _ in range(count)]


class TestRandom(unittest.TestCase):
    def test_randint_deterministic_with_seed(self):
        expected = random.Random(42).randint(1, 100)
        self.assertEqual(expected, 82)
        self.assertEqual(value("seed(42)\nrandint(1, 100)"), 82)
        self.assertEqual(value("seed(42)\nrandint(1, 100)"),
                         value("seed(42)\nrandint(1, 100)"))

    def test_randint_range_and_bounds(self):
        xs = value("seed(42)\nlet xs = []\n"
                   "for i in range(20) { push(xs, randint(1, 10)) }\nxs")
        self.assertEqual(len(xs), 20)
        self.assertTrue(all(1 <= x <= 10 for x in xs))
        self.assertEqual(value("randint(5, 5)"), 5)
        with self.assertRaises(KabirError):
            value("randint(5, 4)")
        with self.assertRaises(KabirError):
            value("randint(1.5, 10)")
        with self.assertRaises(KabirError):
            value("randint(true, 10)")

    def test_randrange(self):
        x = value("seed(42)\nrandrange(10)")
        self.assertTrue(0 <= x < 10)
        x = value("seed(42)\nrandrange(5, 10)")
        self.assertTrue(5 <= x < 10)
        with self.assertRaises(KabirError):
            value("randrange(5, 5)")
        with self.assertRaises(KabirError):
            value("randrange(0)")
        with self.assertRaises(KabirError):
            value("randrange(1.5)")

    def test_choice(self):
        self.assertEqual(value("choice([7])"), 7)
        with self.assertRaises(KabirError):
            value("choice([])")
        with self.assertRaises(KabirError):
            value('choice("abc")')

    def test_shuffle(self):
        result = value("seed(42)\nshuffle([1, 2, 3, 4, 5])")
        self.assertEqual(sorted(result), [1, 2, 3, 4, 5])
        with self.assertRaises(KabirError):
            value('shuffle("ab")')

    def test_shuffle_returns_same_array(self):
        out = run("let a = [1, 2, 3]\nlet b = shuffle(a)\nprint(a == b)")
        self.assertEqual(out, "true\n")

    def test_seed_returns_null(self):
        self.assertIsNone(value("seed(7)"))
        with self.assertRaises(KabirError):
            value("seed(1.5)")
        with self.assertRaises(KabirError):
            value("seed(true)")

    def test_rng_independence(self):
        # Same seed -> identical sequences; different seeds -> differ.
        # (Deterministic formulation: the plan's unseeded-vs-seeded
        # comparison was flaky, failing ~1% of the time by chance.)
        self.assertEqual(_seeded_randint_sequence(1, 5),
                         _seeded_randint_sequence(1, 5))
        rng = random.Random(1)
        self.assertEqual(_seeded_randint_sequence(1, 5),
                         [rng.randint(1, 100) for _ in range(5)])
        seq_a = _seeded_randint_sequence(1, 5)
        seq_b = _seeded_randint_sequence(2, 5)
        self.assertNotEqual(seq_a, seq_b)


class TestMath(unittest.TestCase):
    def test_floor_ceil(self):
        self.assertEqual(value("floor(3.7)"), 3)
        self.assertIsInstance(value("floor(3.7)"), int)
        self.assertEqual(value("floor(-3.2)"), -4)
        self.assertEqual(value("ceil(3.2)"), 4)
        self.assertEqual(value("ceil(-3.7)"), -3)
        self.assertIsInstance(value("ceil(3.2)"), int)
        with self.assertRaises(KabirError):
            value('floor("x")')
        with self.assertRaises(KabirError):
            value("ceil(true)")

    def test_trig(self):
        self.assertEqual(value("sin(0)"), 0.0)
        self.assertEqual(value("cos(0)"), 1.0)
        self.assertEqual(value("tan(0)"), 0.0)
        self.assertAlmostEqual(value("sin(pi / 2)"), 1.0)

    def test_log(self):
        self.assertEqual(value("log(1)"), 0.0)
        self.assertAlmostEqual(value("log(e)"), 1.0)
        with self.assertRaises(KabirError):
            value("log(0)")
        with self.assertRaises(KabirError):
            value("log(-1)")

    def test_exp(self):
        self.assertEqual(value("exp(0)"), 1.0)
        self.assertAlmostEqual(value("exp(1)"), math.e)
        with self.assertRaises(KabirError) as ctx:
            value("exp(1000)")
        self.assertNotIn("Traceback", str(ctx.exception))


class TestPiAndE(unittest.TestCase):
    def test_pi(self):
        self.assertEqual(value("pi"), math.pi)
        self.assertEqual(value("type(pi)"), "number")
        self.assertEqual(run("print(pi)"), "3.141592653589793\n")

    def test_e(self):
        self.assertEqual(value("e"), math.e)
        self.assertEqual(value("type(e)"), "number")

    def test_pi_shadowable(self):
        self.assertEqual(value("let pi = 3\npi"), 3)


if __name__ == "__main__":
    unittest.main()
