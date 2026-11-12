"""CLI integration tests for Kabir 4.0.0 waves (4.1.0 release).

Covers the wiring the coordinator owns: `kabir lsp` and
`kabir build --target wasm`. The WASM run check needs Node; it is
skipped when Node is absent.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import main  # noqa: E402
from kabir.lsp import LanguageServer, encode_message  # noqa: E402

NODE = shutil.which("node")
NEEDS_NODE = unittest.skipIf(NODE is None, "no Node.js")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _run_cli(argv):
    """Run main(argv), capturing stdout/stderr. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _lsp_exchange(messages):
    """Feed *messages* (dicts) to a fresh server; return (exit, replies)."""
    server = LanguageServer()
    in_stream = io.BytesIO(b"".join(encode_message(m) for m in messages))
    out_stream = io.BytesIO()
    code = server.serve(in_stream=in_stream, out_stream=out_stream)
    raw = out_stream.getvalue()
    replies = []
    while raw:
        head, _, rest = raw.partition(b"\r\n\r\n")
        n = int(head.split(b":")[1].strip())
        replies.append(json.loads(rest[:n]))
        raw = rest[n:]
    return code, replies


class TestBuildTargetWasm(unittest.TestCase):
    def test_build_wasm_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(
                    ["build", "--target", "wasm",
                     str(EXAMPLES / "fibonacci.kbr")])
                self.assertEqual(code, 0, err)
                self.assertIn("(wasm)", out)
                self.assertTrue((Path(tmp) / "target"
                                 / "fibonacci.wasm").is_file())
                self.assertTrue((Path(tmp) / "target"
                                 / "fibonacci.html").is_file())
            finally:
                os.chdir(cwd)

    @NEEDS_NODE
    def test_build_wasm_output_matches_vm(self):
        from kabir.wasm import run_wasm
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(
                    ["build", "--target", "wasm",
                     str(EXAMPLES / "fibonacci.kbr")])
                self.assertEqual(code, 0, err)
                wasm_out = run_wasm(
                    (Path(tmp) / "target" / "fibonacci.wasm").read_bytes())
                vm = subprocess.run(
                    [sys.executable, "-m", "kabir", "run",
                     str(EXAMPLES / "fibonacci.kbr")],
                    capture_output=True, text=True,
                    cwd=str(EXAMPLES.parent), env=dict(os.environ))
                self.assertEqual(wasm_out, vm.stdout)
            finally:
                os.chdir(cwd)

    def test_build_wasm_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(["new", "demo"])
                self.assertEqual(code, 0, err)
                code, out, err = _run_cli(["build", "--target", "wasm",
                                           "demo"])
                self.assertEqual(code, 0, err)
                self.assertIn("Built demo v0.1.0 (wasm)", out)
                self.assertTrue((Path(tmp) / "demo" / "target"
                                 / "demo.wasm").is_file())
                self.assertTrue((Path(tmp) / "demo" / "target"
                                 / "demo.html").is_file())
            finally:
                os.chdir(cwd)

    def test_build_wasm_unsupported_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = Path(tmp) / "d.kbr"
            prog.write_text('let d = {"a": 1}\nprint(d)\n', encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = _run_cli(["build", "--target", "wasm",
                                           str(prog)])
                self.assertEqual(code, 1)
                self.assertIn("dicts are not supported", err)
                self.assertFalse((Path(tmp) / "target").exists())
            finally:
                os.chdir(cwd)

    def test_build_unknown_target_is_usage_error(self):
        code, out, err = _run_cli(["build", "--target", "foo", "."])
        self.assertEqual(code, 2)
        self.assertIn("unknown build target", err)

    def test_build_target_without_value_is_usage_error(self):
        code, out, err = _run_cli(["build", "--target"])
        self.assertEqual(code, 2)
        self.assertIn("needs a target", err)


class TestLspCommand(unittest.TestCase):
    def test_lsp_initialize_shutdown_exit(self):
        code, replies = _lsp_exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"processId": None, "rootUri": None,
                        "capabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "shutdown",
             "params": None},
            {"jsonrpc": "2.0", "method": "exit"},
        ])
        self.assertEqual(code, 0)
        by_id = {r["id"]: r for r in replies if "id" in r}
        self.assertIn("serverInfo", by_id[1]["result"])
        self.assertIsNone(by_id[2]["result"])

    def test_lsp_hover_shows_borrow_type(self):
        src = ("func f(a: &number) -> &number { return a }\n"
               "let x = 41\n"
               "print(f(&x))\n")
        uri = "file:///tmp/lsp-borrow.kbr"
        code, replies = _lsp_exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"processId": None, "rootUri": None,
                        "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "textDocument/didOpen",
             "params": {"textDocument": {"uri": uri, "languageId": "kabir",
                                         "version": 1, "text": src}}},
            {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
             "params": {"textDocument": {"uri": uri},
                        "position": {"line": 0, "character": 6}}},
            {"jsonrpc": "2.0", "id": 3, "method": "shutdown",
             "params": None},
            {"jsonrpc": "2.0", "method": "exit"},
        ])
        self.assertEqual(code, 0)
        by_id = {r["id"]: r for r in replies if "id" in r}
        value = by_id[2]["result"]["contents"]["value"]
        self.assertIn("func f(a: &number) -> &number", value)

    def test_lsp_command_rejects_arguments(self):
        code, out, err = _run_cli(["lsp", "extra"])
        self.assertEqual(code, 2)
        self.assertIn("takes no arguments", err)


if __name__ == "__main__":
    unittest.main()
