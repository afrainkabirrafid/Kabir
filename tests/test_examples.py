"""End-to-end tests: every example file runs green via the CLI."""

import os
import subprocess
import sys
import unittest

PYTHON = "python3"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def run_example(name, stdin_text=""):
    proc = subprocess.run(
        [PYTHON, "-m", "kabir", "run",
         os.path.join(EXAMPLES, name)],
        input=stdin_text, capture_output=True, text=True, cwd=ROOT)
    return proc


class TestExamples(unittest.TestCase):
    def test_hello(self):
        proc = run_example("hello.kbr")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Hello, Rafid!", proc.stdout)
        self.assertIn("Welcome to Kabir version 1.0.0.", proc.stdout)
        self.assertIn("2 + 3 = 5", proc.stdout)

    def test_fizzbuzz(self):
        proc = run_example("fizzbuzz.kbr")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().split("\n")
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[2], "Fizz")       # 3
        self.assertEqual(lines[4], "Buzz")       # 5
        self.assertEqual(lines[14], "FizzBuzz")  # 15
        self.assertEqual(lines[99], "Buzz")      # 100

    def test_fibonacci(self):
        proc = run_example("fibonacci.kbr")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().split("\n")
        self.assertEqual(lines[0], "First 15 Fibonacci numbers:")
        self.assertEqual(lines[1:], ["0", "1", "1", "2", "3", "5", "8",
                                     "13", "21", "34", "55", "89", "144",
                                     "233", "377"])

    def test_primes(self):
        proc = run_example("primes.kbr")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().split("\n")
        self.assertEqual(lines[0], "Primes up to 100:")
        primes = [int(x) for x in lines[1:]]
        self.assertEqual(primes[:5], [2, 3, 5, 7, 11])
        self.assertEqual(primes[-1], 97)
        self.assertEqual(len(primes), 25)

    def test_guessing_game(self):
        proc = run_example("guessing-game.kbr", "10\n50\n42\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Too low", proc.stdout)
        self.assertIn("Too high", proc.stdout)
        self.assertIn("You got it in 3 tries! The number was 42.",
                      proc.stdout)

    def test_sorting(self):
        proc = run_example("sorting.kbr")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Before: [64, 34, 25, 12, 22, 11, 90]", proc.stdout)
        self.assertIn("After:  [11, 12, 22, 25, 34, 64, 90]", proc.stdout)

    def test_todo(self):
        proc = run_example("todo.kbr",
                           "add Buy milk\nadd Learn Kabir\nlist\n"
                           "done 1\nlist\nquit\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Added: Buy milk", proc.stdout)
        self.assertIn("2. [ ] Learn Kabir", proc.stdout)
        self.assertIn("1. [x] Buy milk", proc.stdout)
        self.assertIn("Goodbye! You have 2 todos", proc.stdout)

    def test_calculator(self):
        proc = run_example("calculator.kbr", "2 + 3\n10 / 4\n7 * 6\nquit\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 + 3 = 5", proc.stdout)
        self.assertIn("10 / 4 = 2.5", proc.stdout)
        self.assertIn("7 * 6 = 42", proc.stdout)
        self.assertIn("Goodbye!", proc.stdout)


class TestCli(unittest.TestCase):
    def test_version(self):
        proc = subprocess.run([PYTHON, "-m", "kabir", "--version"],
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "kabir 1.0.0")

    def test_help(self):
        proc = subprocess.run([PYTHON, "-m", "kabir", "--help"],
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("kabir run", proc.stdout)

    def test_repl_smoke(self):
        proc = subprocess.run(
            [PYTHON, "-m", "kabir", "repl"],
            input="let x = 2 + 3\nx * 10\n"
                  "func add(a, b) {\nreturn a + b\n}\nadd(4, 5)\n",
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("50", proc.stdout)
        self.assertIn("9", proc.stdout)

    def test_error_exit_code_is_nonzero_without_traceback(self):
        bad = os.path.join(ROOT, "examples", "__bad_tmp.kbr")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("print(undefined_var)\n")
        try:
            proc = subprocess.run(
                [PYTHON, "-m", "kabir", "run", bad],
                capture_output=True, text=True, cwd=ROOT)
        finally:
            os.remove(bad)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("__bad_tmp.kbr:1:", proc.stderr)
        self.assertIn("^", proc.stderr)


if __name__ == "__main__":
    unittest.main()
