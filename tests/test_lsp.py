"""LSP tests: framing, handshake, diagnostics, completion, hover,
definition, formatting, and unknown-method handling.

The server is driven in-process: framed messages are fed through
``LanguageServer.handle_message`` (or through ``serve`` over BytesIO
for the full-loop tests) and framed replies are inspected. No real
editor or subprocess is needed.
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.lsp import (
    LanguageServer,
    encode_message,
    read_message,
)

URI = "file:///test/main.kbr"
GOOD = "let x: number = 1\nprint(x)\n"
BROKEN = "let x = \n"


def make_server():
    server = LanguageServer()
    server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"processId": None, "rootUri": None,
                   "capabilities": {}},
    })
    server.handle_message({"jsonrpc": "2.0", "method": "initialized",
                           "params": {}})
    return server


def open_doc(server, uri=URI, text=GOOD):
    """didOpen a document; returns the publishDiagnostics notification."""
    outgoing = server.handle_message({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "languageId": "kabir",
                                    "version": 1, "text": text}},
    })
    return outgoing


def diagnostics_of(outgoing):
    """The diagnostics list from a publishDiagnostics notification."""
    for message in outgoing:
        if message.get("method") == "textDocument/publishDiagnostics":
            return message["params"]["diagnostics"]
    return None


def request(server, msg_id, method, params):
    """Send one request; returns the result (or raises on error)."""
    outgoing = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params,
    })
    assert len(outgoing) == 1, outgoing
    reply = outgoing[0]
    assert "error" not in reply, reply
    return reply.get("result")


def run_server(messages):
    """Feed framed messages through serve() over BytesIO.

    Returns (exit_code, [decoded replies]).
    """
    server = LanguageServer()
    in_buf = io.BytesIO(b"".join(encode_message(m) for m in messages))
    out_buf = io.BytesIO()
    code = server.serve(in_buf, out_buf)
    out_buf.seek(0)
    replies = []
    while True:
        try:
            message = read_message(out_buf)
        except ValueError:
            break
        if message is None:
            break
        replies.append(message)
    return code, replies


class TestFraming(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        message = {"jsonrpc": "2.0", "id": 7, "method": "initialize",
                   "params": {"capabilities": {}}}
        framed = encode_message(message)
        self.assertTrue(framed.startswith(b"Content-Length: "))
        self.assertIn(b"\r\n\r\n", framed)
        decoded = read_message(io.BytesIO(framed))
        self.assertEqual(decoded, message)

    def test_read_multiple_messages_from_one_stream(self):
        first = {"jsonrpc": "2.0", "id": 1, "method": "shutdown"}
        second = {"jsonrpc": "2.0", "method": "exit"}
        stream = io.BytesIO(encode_message(first) + encode_message(second))
        self.assertEqual(read_message(stream), first)
        self.assertEqual(read_message(stream), second)
        self.assertIsNone(read_message(stream))

    def test_malformed_header_raises(self):
        stream = io.BytesIO(b"Garbage\r\n\r\n{}")
        with self.assertRaises(ValueError):
            read_message(stream)


class TestInitialize(unittest.TestCase):
    def test_initialize_handshake(self):
        server = LanguageServer()
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"processId": None, "rootUri": None,
                       "capabilities": {}},
        })
        self.assertEqual(len(outgoing), 1)
        result = outgoing[0]["result"]
        caps = result["capabilities"]
        self.assertEqual(caps["textDocumentSync"], 1)
        self.assertIn("completionProvider", caps)
        self.assertTrue(caps["hoverProvider"])
        self.assertTrue(caps["definitionProvider"])
        self.assertTrue(caps["documentFormattingProvider"])
        self.assertEqual(result["serverInfo"]["name"], "kabir-lsp")

    def test_serve_loop_shutdown_then_exit_returns_zero(self):
        code, replies = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"capabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
            {"jsonrpc": "2.0", "method": "exit"},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(replies[1]["result"], None)

    def test_exit_without_shutdown_returns_one(self):
        code, _replies = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"capabilities": {}}},
            {"jsonrpc": "2.0", "method": "exit"},
        ])
        self.assertEqual(code, 1)


class TestDiagnostics(unittest.TestCase):
    def test_did_open_broken_file_publishes_diagnostic(self):
        server = make_server()
        diags = diagnostics_of(open_doc(server, text=BROKEN))
        self.assertIsNotNone(diags)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["severity"], 1)
        self.assertEqual(diags[0]["range"]["start"]["line"], 0)
        self.assertTrue(diags[0]["message"])

    def test_did_open_good_file_has_no_diagnostics(self):
        server = make_server()
        diags = diagnostics_of(open_doc(server, text=GOOD))
        self.assertEqual(diags, [])

    def test_did_open_type_error_publishes_diagnostic(self):
        server = make_server()
        diags = diagnostics_of(
            open_doc(server, text='let x: number = "hi"\n'))
        self.assertEqual(len(diags), 1)
        self.assertIn("number", diags[0]["message"])
        self.assertEqual(diags[0]["range"]["start"]["line"], 0)

    def test_did_change_updates_diagnostics(self):
        server = make_server()
        open_doc(server, text=GOOD)
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": URI, "version": 2},
                "contentChanges": [{"text": BROKEN}]},
        })
        self.assertEqual(len(diagnostics_of(outgoing)), 1)
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": URI, "version": 3},
                "contentChanges": [{"text": GOOD}]},
        })
        self.assertEqual(diagnostics_of(outgoing), [])

    def test_did_change_range_edit(self):
        server = make_server()
        open_doc(server, text="let x = 1\n")
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": URI, "version": 2},
                "contentChanges": [{
                    "range": {"start": {"line": 0, "character": 8},
                              "end": {"line": 0, "character": 9}},
                    "text": "2"}]},
        })
        self.assertEqual(diagnostics_of(outgoing), [])
        self.assertEqual(server.documents[URI]["text"], "let x = 2\n")

    def test_did_close_forgets_document(self):
        server = make_server()
        open_doc(server, text=GOOD)
        server.handle_message({
            "jsonrpc": "2.0", "method": "textDocument/didClose",
            "params": {"textDocument": {"uri": URI}},
        })
        self.assertNotIn(URI, server.documents)
        result = request(server, 9, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 6},
        })
        self.assertIsNone(result)


class TestCompletion(unittest.TestCase):
    def test_completion_returns_keywords_and_builtins(self):
        server = make_server()
        open_doc(server, text="")
        result = request(server, 2, "textDocument/completion", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 0},
        })
        labels = [item["label"] for item in result["items"]]
        for expected in ("let", "func", "if", "return", "print", "len",
                         "range", "randint", "floor"):
            self.assertIn(expected, labels)

    def test_completion_filters_by_prefix(self):
        server = make_server()
        open_doc(server, text="le")
        result = request(server, 2, "textDocument/completion", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 2},
        })
        labels = [item["label"] for item in result["items"]]
        self.assertIn("let", labels)
        self.assertNotIn("func", labels)
        self.assertNotIn("print", labels)

    def test_completion_includes_in_scope_names(self):
        server = make_server()
        open_doc(server, text="let myCounter = 1\nmy")
        result = request(server, 2, "textDocument/completion", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 2},
        })
        labels = [item["label"] for item in result["items"]]
        self.assertIn("myCounter", labels)

    def test_completion_after_dot_returns_string_methods(self):
        server = make_server()
        open_doc(server, text='let s = "hi"\ns.')
        result = request(server, 2, "textDocument/completion", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 2},
        })
        labels = [item["label"] for item in result["items"]]
        for expected in ("upper", "lower", "split", "contains"):
            self.assertIn(expected, labels)


class TestHover(unittest.TestCase):
    def test_hover_returns_declared_type(self):
        server = make_server()
        open_doc(server, text=GOOD)
        result = request(server, 3, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 7},
        })
        self.assertIsNotNone(result)
        value = result["contents"]["value"]
        self.assertIn("let x: number", value)
        self.assertIn("line 1", value)

    def test_hover_returns_inferred_type(self):
        server = make_server()
        open_doc(server, text='let greeting = "hi"\nprint(greeting)\n')
        result = request(server, 3, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 8},
        })
        self.assertIn("let greeting: string",
                      result["contents"]["value"])

    def test_hover_on_builtin(self):
        server = make_server()
        open_doc(server, text="print(1)\n")
        result = request(server, 3, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 2},
        })
        self.assertIn("builtin print", result["contents"]["value"])

    def test_hover_unknown_name_returns_null(self):
        server = make_server()
        open_doc(server, text="print(nope)\n")
        result = request(server, 3, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 7},
        })
        self.assertIsNone(result)

    def test_hover_on_empty_position_returns_null(self):
        server = make_server()
        open_doc(server, text=GOOD)
        result = request(server, 3, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 0},
        })
        self.assertIsNone(result)


class TestDefinition(unittest.TestCase):
    def test_definition_resolves_let(self):
        server = make_server()
        open_doc(server, text=GOOD)
        result = request(server, 4, "textDocument/definition", {
            "textDocument": {"uri": URI},
            "position": {"line": 1, "character": 7},
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["uri"], URI)
        self.assertEqual(result["range"]["start"], {"line": 0,
                                                    "character": 4})
        self.assertEqual(result["range"]["end"], {"line": 0,
                                                  "character": 5})

    def test_definition_resolves_function(self):
        server = make_server()
        source = ("func greet(name) {\n    print(name)\n}\n\ngreet(\"Kat\")\n")
        open_doc(server, text=source)
        result = request(server, 4, "textDocument/definition", {
            "textDocument": {"uri": URI},
            "position": {"line": 4, "character": 2},
        })
        self.assertEqual(result["range"]["start"]["line"], 0)

    def test_definition_unknown_name_returns_null(self):
        server = make_server()
        open_doc(server, text="print(nope)\n")
        result = request(server, 4, "textDocument/definition", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 7},
        })
        self.assertIsNone(result)


class TestFormatting(unittest.TestCase):
    def test_formatting_returns_formatted_text(self):
        server = make_server()
        open_doc(server, text="let x=1+2\n")
        result = request(server, 5, "textDocument/formatting", {
            "textDocument": {"uri": URI},
            "options": {"tabSize": 4, "insertSpaces": True},
        })
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["newText"], "let x = 1 + 2\n")
        self.assertEqual(result[0]["range"]["start"],
                         {"line": 0, "character": 0})

    def test_formatting_already_formatted_returns_no_edits(self):
        server = make_server()
        open_doc(server, text=GOOD)
        result = request(server, 5, "textDocument/formatting", {
            "textDocument": {"uri": URI},
            "options": {"tabSize": 4, "insertSpaces": True},
        })
        self.assertEqual(result, [])

    def test_formatting_broken_file_returns_error(self):
        server = make_server()
        open_doc(server, text=BROKEN)
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "textDocument/formatting",
            "params": {"textDocument": {"uri": URI},
                       "options": {"tabSize": 4, "insertSpaces": True}},
        })
        self.assertIn("error", outgoing[0])
        self.assertEqual(outgoing[0]["error"]["code"], -32602)


class TestUnknownMethods(unittest.TestCase):
    def test_unknown_method_returns_method_not_found(self):
        server = make_server()
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "id": 42,
            "method": "textDocument/unknownThing", "params": {},
        })
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]["error"]["code"], -32601)
        self.assertIn("unknownThing", outgoing[0]["error"]["message"])

    def test_unknown_notification_does_not_crash(self):
        server = make_server()
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "method": "textDocument/didSave",
            "params": {"textDocument": {"uri": URI}},
        })
        self.assertEqual(outgoing, [])
        # The server still answers afterwards.
        result = request(server, 6, "textDocument/hover", {
            "textDocument": {"uri": URI},
            "position": {"line": 0, "character": 0},
        })
        self.assertIsNone(result)


class TestJsonRpcShape(unittest.TestCase):
    def test_responses_carry_jsonrpc_and_id(self):
        server = make_server()
        outgoing = server.handle_message({
            "jsonrpc": "2.0", "id": "abc", "method": "shutdown",
            "params": None,
        })
        self.assertEqual(outgoing[0]["jsonrpc"], "2.0")
        self.assertEqual(outgoing[0]["id"], "abc")

    def test_did_open_notification_has_no_response(self):
        server = make_server()
        outgoing = open_doc(server, text=GOOD)
        self.assertEqual(len(outgoing), 1)
        self.assertNotIn("id", outgoing[0])
        self.assertEqual(outgoing[0]["method"],
                         "textDocument/publishDiagnostics")


if __name__ == "__main__":
    unittest.main()
