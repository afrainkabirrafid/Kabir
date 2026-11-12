"""Async/await + tasks/channels tests (Kabir 4.0.0 Wave 1).

Covers: `async func` / `await` parsing (and that async/await are now
keywords), compile-time rejection of `await` outside an async func,
the VM scheduler (round-robin quanta, tasks, channels, send/recv
ordering, blocking, deadlock, error propagation), the interpreter's
clear VM-path errors, native mode's clear VM-only rejection, and the
examples/async-demo.kbr end-to-end run.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir import ast_nodes as ast
from kabir.checker import check_program
from kabir.codegen import transpile
from kabir.compile import compile_source
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse as parse_source
from kabir.vm import VM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = "python3"


def run_vm(source, filename="<test>"):
    """Run source on the VM, returning (stdout, vm)."""
    vm = VM(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm.run_source(source, filename=filename)
    return buf.getvalue(), vm


def run_interp(source, filename="<test>"):
    interp = Interpreter(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename=filename)
    return buf.getvalue()


def run_cli(args, stdin_text=""):
    return subprocess.run(
        [PYTHON, "-m", "kabir"] + args,
        input=stdin_text, capture_output=True, text=True, cwd=ROOT)


def write_temp_kbr(source):
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".kbr", delete=False, encoding="utf-8")
    handle.write(source)
    handle.close()
    return handle.name


class TestParser(unittest.TestCase):
    def test_async_func_decl_parses(self):
        program = parse_source("async func f(a, b) {\nreturn a + b\n}\n")
        decl = program.statements[0]
        self.assertIsInstance(decl, ast.FuncDecl)
        self.assertTrue(decl.is_async)
        self.assertEqual(decl.name, "f")
        self.assertEqual(decl.params, ["a", "b"])

    def test_sync_func_decl_is_not_async(self):
        program = parse_source("func f() {\nreturn 1\n}\n")
        self.assertFalse(program.statements[0].is_async)

    def test_async_anonymous_func_parses(self):
        program = parse_source("let f = async func(x) {\nreturn x\n}\n")
        value = program.statements[0].value
        self.assertIsInstance(value, ast.FuncDecl)
        self.assertTrue(value.is_async)
        self.assertIsNone(value.name)

    def test_await_parses_as_expression(self):
        program = parse_source(
            "async func f() {\nlet t = f()\nreturn await t + 1\n}\n")
        body = program.statements[0].body.statements
        ret = body[1].value
        self.assertIsInstance(ret, ast.BinaryOp)
        self.assertIsInstance(ret.left, ast.AwaitExpr)
        self.assertIsInstance(ret.left.expr, ast.Var)
        self.assertEqual(ret.left.expr.name, "t")

    def test_async_without_func_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            parse_source("async f() {\n}\n")
        self.assertIn("'func' after 'async'", str(ctx.exception))

    def test_async_and_await_are_keywords_now(self):
        # Pre-4.0.0 code that used them as names no longer parses:
        # `async` cannot start a statement, `await` cannot be a name.
        with self.assertRaises(KabirError):
            parse_source("let async = 5\n")
        with self.assertRaises(KabirError):
            parse_source("let await = 5\n")

    def test_async_func_with_types_parses(self):
        program = parse_source(
            "async func add(a: number, b: number) -> number {\n"
            "return a + b\n}\n")
        decl = program.statements[0]
        self.assertTrue(decl.is_async)
        self.assertEqual(decl.return_type.name, "number")


class TestCompiler(unittest.TestCase):
    def test_await_outside_async_func_is_a_compile_error(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("let x = await 5\n", "<test>")
        self.assertIn("'await' can only be used inside an 'async func'",
                      str(ctx.exception))

    def test_await_inside_sync_func_is_a_compile_error(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("func f() {\nreturn await 5\n}\n", "<test>")
        self.assertIn("'await' can only be used inside an 'async func'",
                      str(ctx.exception))

    def test_await_inside_async_func_compiles(self):
        code = compile_source(
            "async func f() {\nlet t = f()\nreturn await t\n}\n", "<test>")
        inner = code["consts"][0]["__code__"]
        self.assertTrue(inner["is_async"])
        self.assertIn(["AWAIT", None], inner["instrs"])

    def test_await_inside_nested_sync_func_is_a_compile_error(self):
        # An async func containing a sync func: await in the inner
        # (sync) one is still illegal.
        with self.assertRaises(KabirError) as ctx:
            compile_source(
                "async func outer() {\n"
                "func inner() {\nreturn await 5\n}\n"
                "return inner()\n}\n", "<test>")
        self.assertIn("'await' can only be used inside an 'async func'",
                      str(ctx.exception))

    def test_spawn_call_emits_spawn_task(self):
        code = compile_source(
            "async func work() {\nreturn 1\n}\n"
            "let t = spawn(work())\n", "<test>")
        ops = [op for op, _arg in code["instrs"]]
        self.assertIn("SPAWN_TASK", ops)

    def test_spawn_of_bare_function_emits_spawn_task(self):
        code = compile_source(
            "async func work() {\nreturn 1\n}\n"
            "let t = spawn(work)\n", "<test>")
        ops = [op for op, _arg in code["instrs"]]
        self.assertIn("SPAWN_TASK", ops)

    def test_top_level_code_is_not_async(self):
        code = compile_source("let x = 1\n", "<test>")
        self.assertFalse(code.get("is_async", False))

    def test_checker_passes_async_programs(self):
        # The checker is permissive here (await/spawn/chan type as
        # "any"); the point is `kabir run` must not fail at check time.
        program = parse_source(
            "async func f() {\nreturn await f()\n}\n"
            "let ch = chan()\nlet t = spawn(f())\n", "<test>")
        check_program(program, filename="<test>",
                      lines=["async func f() {", "return await f()", "}",
                             "let ch = chan()", "let t = spawn(f())"])


class TestVmBasics(unittest.TestCase):
    def test_calling_async_func_returns_a_task(self):
        out, vm = run_vm(
            "async func f() {\nreturn 7\n}\n"
            "let t = f()\n"
            "print(type(t))\n"
            "print(t)\n")
        self.assertIn("task", out)
        self.assertRegex(out, r"<task \d+>")

    def test_await_returns_the_tasks_value(self):
        out, _vm = run_vm(
            "async func double(x) {\nreturn x * 2\n}\n"
            "async func main() {\n"
            "let t = double(21)\n"
            "print(await t)\n}\n"
            "let m = main()\n")
        self.assertIn("42", out)

    def test_await_chains_through_async_funcs(self):
        out, _vm = run_vm(
            "async func inner() {\nreturn 7\n}\n"
            "async func outer() {\n"
            "let t = inner()\n"
            "return await t + 1\n}\n"
            "async func top() {\n"
            "let t = outer()\n"
            "print(await t)\n}\n"
            "let m = top()\n")
        self.assertIn("8", out)

    def test_async_func_with_no_return_awaits_null(self):
        out, _vm = run_vm(
            "async func noop() {\n}\n"
            "async func top() {\n"
            "let t = noop()\n"
            "print(await t)\n}\n"
            "let m = top()\n")
        self.assertIn("null", out)

    def test_async_func_can_call_sync_funcs(self):
        out, _vm = run_vm(
            "func add(a, b) {\nreturn a + b\n}\n"
            "async func top() {\n"
            "print(add(3, 4))\n}\n"
            "let m = top()\n")
        self.assertIn("7", out)

    def test_sync_func_can_call_async_func_and_get_a_task(self):
        out, _vm = run_vm(
            "async func work() {\nreturn 5\n}\n"
            "func get_task() {\nreturn work()\n}\n"
            "async func top() {\n"
            "let t = get_task()\n"
            "print(await t)\n}\n"
            "let m = top()\n")
        self.assertIn("5", out)

    def test_async_func_wrong_arity_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("async func f(a) {\nreturn a\n}\nlet t = f()\n")
        self.assertIn("takes 1 argument, but got 0", str(ctx.exception))

    def test_await_on_a_non_task_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "async func top() {\n"
                "print(await 5)\n}\n"
                "let m = top()\n")
        self.assertIn("'await' needs a task", str(ctx.exception))

    def test_await_on_oneself_is_an_error(self):
        # A task can only hold its own handle via shared state: the
        # task reads its handle out of an array the top level filled
        # in after spawning it. Awaiting it must fail, not hang.
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "let holder = []\n"
                "async func selfish() {\n"
                "print(await holder[0])\n}\n"
                "let t = selfish()\n"
                "push(holder, t)\n")
        self.assertIn("cannot await itself", str(ctx.exception))

    def test_awaiting_the_same_task_twice_works(self):
        out, _vm = run_vm(
            "async func work() {\nreturn 9\n}\n"
            "async func waiter(t) {\nprint(await t)\n}\n"
            "let shared = work()\n"
            "let a = waiter(shared)\n"
            "let b = waiter(shared)\n")
        self.assertEqual(out.count("9"), 2)

    def test_task_equality_is_identity(self):
        out, _vm = run_vm(
            "async func f() {\nreturn 1\n}\n"
            "let a = f()\n"
            "let b = f()\n"
            "print(a == a)\n"
            "print(a == b)\n")
        self.assertIn("true", out)
        self.assertIn("false", out)


class TestChannels(unittest.TestCase):
    def test_send_recv_in_order(self):
        out, _vm = run_vm(
            "async func producer(ch) {\n"
            "send(ch, 1)\nsend(ch, 2)\nsend(ch, 3)\n}\n"
            "let ch = chan()\n"
            "let p = spawn(producer(ch))\n"
            "print(recv(ch))\n"
            "print(recv(ch))\n"
            "print(recv(ch))\n")
        self.assertEqual(out, "1\n2\n3\n")

    def test_recv_blocks_until_send_arrives(self):
        # The receiver starts first and blocks; the sender only
        # sends after a busy loop, so recv must have waited.
        out, _vm = run_vm(
            "async func slow_sender(ch) {\n"
            "let i = 0\n"
            "while i < 5000 {\ni = i + 1\n}\n"
            "send(ch, \"late\")\n}\n"
            "async func receiver(ch) {\n"
            "print(recv(ch))\n}\n"
            "let ch = chan()\n"
            "let r = receiver(ch)\n"
            "let s = slow_sender(ch)\n")
        self.assertIn("late", out)

    def test_channel_is_unbounded(self):
        # Sends with no receiver yet are queued, not lost.
        out, _vm = run_vm(
            "let ch = chan()\n"
            "send(ch, \"a\")\n"
            "send(ch, \"b\")\n"
            "print(recv(ch))\n"
            "print(recv(ch))\n")
        self.assertEqual(out, "a\nb\n")

    def test_two_receivers_each_get_one_value(self):
        _out, vm = run_vm(
            "async func taker(ch, out) {\n"
            "push(out, recv(ch))\n}\n"
            "let ch = chan()\n"
            "let got = []\n"
            "let a = taker(ch, got)\n"
            "let b = taker(ch, got)\n"
            "send(ch, 1)\n"
            "send(ch, 2)\n")
        got = vm.globals.lookup("got")
        self.assertEqual(sorted(got), [1, 2])

    def test_channel_type_and_print(self):
        out, _vm = run_vm(
            "let ch = chan()\n"
            "print(type(ch))\n"
            "print(ch)\n")
        self.assertIn("channel", out)
        self.assertIn("<channel>", out)

    def test_send_to_a_non_channel_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("send(5, 1)\n")
        self.assertIn("send() needs a channel", str(ctx.exception))

    def test_recv_from_a_non_channel_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("print(recv(\"nope\"))\n")
        self.assertIn("recv() needs a channel", str(ctx.exception))


class TestScheduler(unittest.TestCase):
    def test_two_busy_tasks_both_progress(self):
        # Cooperative quanta: neither task may starve the other.
        _out, vm = run_vm(
            "let log = []\n"
            "async func worker(tag, n) {\n"
            "let i = 0\n"
            "while i < n {\n"
            "push(log, tag)\n"
            "i = i + 1\n}\n}\n"
            "let a = worker(\"a\", 3000)\n"
            "let b = worker(\"b\", 3000)\n")
        log = vm.globals.lookup("log")
        self.assertEqual(len(log), 6000)
        self.assertEqual(log.count("a"), 3000)
        self.assertEqual(log.count("b"), 3000)
        # Not all of one tag before the other: they interleaved.
        first_b = log.index("b")
        last_a = len(log) - 1 - log[::-1].index("a")
        self.assertLess(first_b, last_a)

    def test_run_waits_for_spawned_tasks(self):
        # Main ends at once; the run must still wait for the task.
        out, _vm = run_vm(
            "async func slow() {\n"
            "let i = 0\n"
            "while i < 2000 {\ni = i + 1\n}\n"
            "print(\"slow done\")\n}\n"
            "let t = spawn(slow())\n"
            "print(\"main done\")\n")
        self.assertIn("main done", out)
        self.assertIn("slow done", out)

    def test_task_can_spawn_another_task(self):
        out, _vm = run_vm(
            "async func leaf() {\nprint(\"leaf\")\n}\n"
            "async func middle() {\n"
            "let t = spawn(leaf())\n"
            "print(\"middle\")\n}\n"
            "let m = middle()\n")
        self.assertIn("middle", out)
        self.assertIn("leaf", out)

    def test_spawn_through_a_variable_works_for_zero_arg_funcs(self):
        out, _vm = run_vm(
            "async func work() {\nprint(\"via var\")\n}\n"
            "let s = spawn\n"
            "let t = s(work)\n")
        self.assertIn("via var", out)

    def test_spawn_of_a_non_function_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("let t = spawn(5)\n")
        self.assertIn("spawn() needs a call", str(ctx.exception))

    def test_spawn_with_wrong_arg_count_is_an_error(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("async func f(a) {\nreturn a\n}\n"
                   "let t = spawn(f())\n")
        self.assertIn("takes 1 argument, but got 0", str(ctx.exception))


class TestAsyncErrors(unittest.TestCase):
    def test_deadlock_on_recv_with_no_sender(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm("let ch = chan()\nprint(recv(ch))\n")
        self.assertIn("Deadlock", str(ctx.exception))
        self.assertIn("channel", str(ctx.exception))

    def test_deadlock_when_sender_finished_without_sending(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "async func useless() {\nreturn 1\n}\n"
                "let ch = chan()\n"
                "let t = useless()\n"
                "print(recv(ch))\n")
        self.assertIn("Deadlock", str(ctx.exception))

    def test_task_error_propagates_through_await(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "async func boom() {\n"
                "let x = [1]\n"
                "return x[5]\n}\n"
                "async func top() {\n"
                "let t = boom()\n"
                "print(await t)\n}\n"
                "let m = top()\n")
        self.assertIn("out of range", str(ctx.exception))

    def test_unawaited_task_error_fails_the_run(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "async func boom() {\n"
                "let x = [1]\n"
                "return x[5]\n}\n"
                "let t = boom()\n"
                "print(\"main done\")\n")
        self.assertIn("out of range", str(ctx.exception))

    def test_error_inside_spawned_task_fails_the_run(self):
        with self.assertRaises(KabirError) as ctx:
            run_vm(
                "async func boom() {\n"
                "let x = [1]\n"
                "return x[5]\n}\n"
                "let t = spawn(boom())\n")
        self.assertIn("out of range", str(ctx.exception))


class TestInterpreterErrors(unittest.TestCase):
    def test_async_func_decl_errors_in_interp(self):
        with self.assertRaises(KabirError) as ctx:
            run_interp("async func f() {\nreturn 1\n}\n")
        self.assertIn("async needs the VM path", str(ctx.exception))
        self.assertIn("kabir run", str(ctx.exception))

    def test_anonymous_async_func_errors_in_interp(self):
        with self.assertRaises(KabirError) as ctx:
            run_interp("let f = async func() {\nreturn 1\n}\n")
        self.assertIn("async needs the VM path", str(ctx.exception))

    def test_await_errors_in_interp(self):
        # The await must actually execute: a bare top-level await.
        # (An await hidden inside an uncalled function body never
        # runs, in either runtime.)
        with self.assertRaises(KabirError) as ctx:
            run_interp("let x = await 5\n")
        self.assertIn("needs the VM path", str(ctx.exception))

    def test_spawn_chan_send_recv_error_in_interp(self):
        for source, name in [
                ("let t = spawn(1)\n", "spawn()"),
                ("let ch = chan()\n", "chan()"),
                ("send(1, 2)\n", "send()"),
                ("print(recv(1))\n", "recv()")]:
            with self.assertRaises(KabirError) as ctx:
                run_interp(source)
            self.assertIn(name + " needs the VM path",
                          str(ctx.exception), source)

    def test_sync_code_still_runs_in_interp(self):
        out = run_interp("func add(a, b) {\nreturn a + b\n}\n"
                         "print(add(2, 3))\n")
        self.assertIn("5", out)


class TestNativeRejection(unittest.TestCase):
    def test_transpile_rejects_async_func(self):
        with self.assertRaises(KabirError) as ctx:
            transpile("async func f() {\nreturn 1\n}\n")
        self.assertIn("VM-only", str(ctx.exception))

    def test_transpile_rejects_await(self):
        with self.assertRaises(KabirError) as ctx:
            transpile("async func f() {\nreturn await f()\n}\n")
        self.assertIn("VM-only", str(ctx.exception))

    def test_transpile_rejects_spawn_chan_send_recv(self):
        for source, name in [
                ("let t = spawn(f())\n", "spawn()"),
                ("let ch = chan()\n", "chan()"),
                ("send(ch, 1)\n", "send()"),
                ("print(recv(ch))\n", "recv()")]:
            with self.assertRaises(KabirError) as ctx:
                transpile(source)
            self.assertIn("VM-only", str(ctx.exception), source)


class TestCliAndDemo(unittest.TestCase):
    def test_demo_runs_via_cli(self):
        proc = run_cli(["run", os.path.join(ROOT, "examples",
                                            "async-demo.kbr")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("main done", proc.stdout)
        self.assertIn("received: [10, 11, 12]", proc.stdout)
        self.assertIn("sum: 42", proc.stdout)

    def test_cli_interp_rejects_async_clearly(self):
        path = write_temp_kbr(
            "async func f() {\nreturn 1\n}\nprint(\"nope\")\n")
        try:
            proc = run_cli(["run", "--interp", path])
        finally:
            os.remove(path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("async needs the VM path", proc.stderr)

    def test_cli_native_rejects_async_clearly(self):
        path = write_temp_kbr(
            "async func f() {\nreturn 1\n}\nprint(\"nope\")\n")
        try:
            proc = run_cli(["run", "--native", path])
        finally:
            os.remove(path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("VM-only", proc.stderr)

    def test_cli_vm_runs_async_program(self):
        path = write_temp_kbr(
            "async func double(x) {\nreturn x * 2\n}\n"
            "async func top() {\n"
            "let t = double(21)\n"
            "print(await t)\n}\n"
            "let m = top()\n")
        try:
            proc = run_cli(["run", path])
        finally:
            os.remove(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("42", proc.stdout)


if __name__ == "__main__":
    unittest.main()
