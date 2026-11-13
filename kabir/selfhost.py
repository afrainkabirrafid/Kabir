"""Bootstrap driver for the self-hosted Kabir parser (Kabir 5.0.0,
Leap 1).

The Kabir parser lives in ``kabir/selfhost/parser.kbr``. It does NOT lex:
it takes a token stream (the format ``kabir/selfhost/lexer.kbr`` prints,
as JSON) and produces the program's AST as a JSON object. This
module is the Python side of that bootstrap:

* :func:`ast_to_dict` -- the canonical AST serialization. A Python
  AST node and the Kabir parser's dict for the same construct
  serialize to the SAME JSON object: every node is
  ``{"type": <Name>, "line": <int>, "col": <int>, ...fields}``
  with keys in a fixed order, literal values kept as JSON
  ``int`` / ``float`` / ``string`` / ``bool`` / ``null`` (so the
  ``2`` vs ``2.0`` distinction survives), and ``None`` for absent
  optional parts (``type_ann``, ``guard``, ``else_body``, ...).
  The comparison in the tests is a plain string comparison of
  ``json.dumps`` output on both sides -- ``json_stringify`` in
  Kabir is the same ``json.dumps`` call, so the two byte streams
  must agree exactly.
* :func:`tokens_to_json` -- serialize the Python lexer's tokens
  into the JSON the Kabir parser reads (``kind`` / ``value`` /
  ``line`` / ``col`` / ``is_float`` per token; a STRING value is
  the list of ``["lit", text]`` / ``["expr", source]`` parts).
* :func:`run_kabir_parser` / :func:`kabir_ast_json` -- feed those
  tokens to ``kabir/selfhost/parser.kbr`` through the VM (``kabir run``,
  token-file path on stdin, exactly like ``kabir/selfhost/lexer.kbr``)
  and return the AST JSON it prints.

The Python parser stays the bootstrap compiler: nothing here
replaces ``kabir/parser.py``.
"""

import json
import os
import subprocess
import sys
import tempfile

from . import ast_nodes as ast
from .compile import compile_source
from .errors import KabirError
from .lexer import tokenize
from .parser import parse

# The package directory: the .kbr files live in kabir/selfhost/ so
# they ship with the installed package (package-data) and
# `kabir run --selfhost` works from a pip install, not just a checkout.
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
# Parent of the package dir: the repo root in a checkout (so
# `python -m kabir` resolves when the subprocess runs with cwd=PKG_DIR),
# site-packages in an install (already on sys.path; harmless).
_PKG_PARENT = os.path.dirname(PKG_DIR)
PARSER_KBR = os.path.join(PKG_DIR, "selfhost", "parser.kbr")
COMPILER_KBR = os.path.join(PKG_DIR, "selfhost", "compiler.kbr")


def _subprocess_env():
    """Env for the `python -m kabir run` subprocesses below: the
    parent of the package dir on PYTHONPATH, so the `kabir` module
    resolves even though cwd is PKG_DIR (needed for the .kbr files'
    relative self-location checks)."""
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PKG_PARENT + (os.pathsep + old if old else "")
    return env


def ast_to_dict(node):
    """Serialize one AST node (or None) to the canonical dict form.

    Field order is part of the contract: the Kabir parser builds
    its dicts with keys in exactly this order, so ``json.dumps``
    agrees byte-for-byte.
    """
    if node is None:
        return None
    if isinstance(node, ast.Program):
        return {"type": "Program", "line": node.line, "col": node.col,
                "statements": [ast_to_dict(s) for s in node.statements]}
    if isinstance(node, ast.Block):
        # end_line is the closing brace's line; the compiler uses it
        # for the EXIT_SCOPE line (blocks built by hand lack it).
        end_line = getattr(node, "end_line", None) or node.line
        return {"type": "Block", "line": node.line, "col": node.col,
                "end_line": end_line,
                "statements": [ast_to_dict(s) for s in node.statements]}
    if isinstance(node, ast.LetStmt):
        return {"type": "LetStmt", "line": node.line, "col": node.col,
                "name": node.name, "value": ast_to_dict(node.value),
                "type_ann": ast_to_dict(node.type_ann),
                "mutable": node.mutable}
    if isinstance(node, ast.AssignStmt):
        return {"type": "AssignStmt", "line": node.line, "col": node.col,
                "target": ast_to_dict(node.target), "op": node.op,
                "value": ast_to_dict(node.value)}
    if isinstance(node, ast.ExprStmt):
        return {"type": "ExprStmt", "line": node.line, "col": node.col,
                "expr": ast_to_dict(node.expr)}
    if isinstance(node, ast.FuncDecl):
        return {"type": "FuncDecl", "line": node.line, "col": node.col,
                "name": node.name, "params": list(node.params),
                "param_types": [ast_to_dict(t)
                                for t in (node.param_types or [])],
                "return_type": ast_to_dict(node.return_type),
                "is_async": node.is_async,
                "body": ast_to_dict(node.body)}
    if isinstance(node, ast.ReturnStmt):
        return {"type": "ReturnStmt", "line": node.line, "col": node.col,
                "value": ast_to_dict(node.value)}
    if isinstance(node, ast.IfStmt):
        return {"type": "IfStmt", "line": node.line, "col": node.col,
                "branches": [[ast_to_dict(c), ast_to_dict(b)]
                             for c, b in node.branches],
                "else_body": ast_to_dict(node.else_body)}
    if isinstance(node, ast.WhileStmt):
        return {"type": "WhileStmt", "line": node.line, "col": node.col,
                "condition": ast_to_dict(node.condition),
                "body": ast_to_dict(node.body)}
    if isinstance(node, ast.ForStmt):
        return {"type": "ForStmt", "line": node.line, "col": node.col,
                "var": node.var, "iterable": ast_to_dict(node.iterable),
                "body": ast_to_dict(node.body)}
    if isinstance(node, ast.BreakStmt):
        return {"type": "BreakStmt", "line": node.line, "col": node.col}
    if isinstance(node, ast.ContinueStmt):
        return {"type": "ContinueStmt", "line": node.line, "col": node.col}
    if isinstance(node, ast.ImportStmt):
        return {"type": "ImportStmt", "line": node.line, "col": node.col,
                "path": node.path}
    if isinstance(node, ast.Literal):
        return {"type": "Literal", "line": node.line, "col": node.col,
                "value": node.value}
    if isinstance(node, ast.StringLiteral):
        return {"type": "StringLiteral", "line": node.line,
                "col": node.col,
                "parts": [[kind, text] for kind, text in node.parts]}
    if isinstance(node, ast.Var):
        return {"type": "Var", "line": node.line, "col": node.col,
                "name": node.name}
    if isinstance(node, ast.ArrayLiteral):
        return {"type": "ArrayLiteral", "line": node.line,
                "col": node.col,
                "elements": [ast_to_dict(e) for e in node.elements]}
    if isinstance(node, ast.DictLiteral):
        return {"type": "DictLiteral", "line": node.line, "col": node.col,
                "pairs": [[key, ast_to_dict(v)]
                          for key, v in node.pairs]}
    if isinstance(node, ast.BinaryOp):
        return {"type": "BinaryOp", "line": node.line, "col": node.col,
                "op": node.op, "left": ast_to_dict(node.left),
                "right": ast_to_dict(node.right)}
    if isinstance(node, ast.UnaryOp):
        return {"type": "UnaryOp", "line": node.line, "col": node.col,
                "op": node.op, "operand": ast_to_dict(node.operand)}
    if isinstance(node, ast.AwaitExpr):
        return {"type": "AwaitExpr", "line": node.line, "col": node.col,
                "expr": ast_to_dict(node.expr)}
    if isinstance(node, ast.Call):
        return {"type": "Call", "line": node.line, "col": node.col,
                "callee": ast_to_dict(node.callee),
                "args": [ast_to_dict(a) for a in node.args]}
    if isinstance(node, ast.Index):
        return {"type": "Index", "line": node.line, "col": node.col,
                "target": ast_to_dict(node.target),
                "index": ast_to_dict(node.index)}
    if isinstance(node, ast.Member):
        return {"type": "Member", "line": node.line, "col": node.col,
                "object": ast_to_dict(node.object), "name": node.name}
    if isinstance(node, ast.BorrowExpr):
        return {"type": "BorrowExpr", "line": node.line, "col": node.col,
                "expr": ast_to_dict(node.expr),
                "mutable": node.mutable}
    if isinstance(node, ast.TypeAnn):
        return {"type": "TypeAnn", "line": node.line, "col": node.col,
                "name": node.name, "borrow": node.borrow}
    if isinstance(node, ast.Wildcard):
        return {"type": "Wildcard", "line": node.line, "col": node.col}
    if isinstance(node, ast.MatchArm):
        return {"type": "MatchArm", "line": node.line, "col": node.col,
                "pattern": ast_to_dict(node.pattern),
                "guard": ast_to_dict(node.guard),
                "body": ast_to_dict(node.body)}
    if isinstance(node, ast.MatchExpr):
        return {"type": "MatchExpr", "line": node.line, "col": node.col,
                "scrutinee": ast_to_dict(node.scrutinee),
                "arms": [ast_to_dict(a) for a in node.arms]}
    raise TypeError("ast_to_dict: unknown AST node %r" % (node,))


def canonical_ast_json(source, filename="<test>"):
    """The Python parser's AST for *source*, as canonical JSON."""
    return json.dumps(ast_to_dict(parse(source, filename)))


def tokens_to_json(source, filename="<test>"):
    """The Python lexer's tokens for *source*, as JSON for parser.kbr.

    One object per token: ``kind`` / ``value`` / ``line`` / ``col`` /
    ``is_float``. A NUMBER value is the int/float itself; a STRING
    value is its ``[kind, text]`` parts; EOF's value is null.
    """
    out = []
    for tok in tokenize(source, filename):
        value = tok.value
        if tok.kind == "STRING":
            value = [[kind, text] for kind, text in value]
        out.append({"kind": tok.kind, "value": value,
                    "line": tok.line, "col": tok.col,
                    "is_float": isinstance(tok.value, float)
                    if tok.kind == "NUMBER" else False})
    return json.dumps(out)


def run_kabir_parser(source, filename="<test>"):
    """Run kabir/selfhost/parser.kbr over *source*; return the proc.

    The tokens (from the Python lexer) are written to a temp JSON
    file whose path is handed to parser.kbr on stdin, mirroring
    how kabir/selfhost/lexer.kbr takes its target path.
    """
    tokens_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".tokens.json",
                                         delete=False,
                                         encoding="utf-8") as handle:
            handle.write(tokens_to_json(source, filename))
            tokens_path = handle.name
        return subprocess.run(
            [sys.executable, "-m", "kabir", "run", PARSER_KBR],
            input=tokens_path + "\n", capture_output=True, text=True,
            cwd=PKG_DIR, env=_subprocess_env())
    finally:
        if tokens_path is not None:
            try:
                os.unlink(tokens_path)
            except OSError:
                pass


def kabir_ast_json(source, filename="<test>"):
    """The Kabir parser's AST JSON for *source* (raises on failure)."""
    proc = run_kabir_parser(source, filename)
    assert proc.returncode == 0, "parser.kbr crashed: %s" % proc.stderr
    assert proc.stderr == "", "parser.kbr wrote to stderr: %s" % proc.stderr
    out = proc.stdout
    if out.endswith("\n"):
        out = out[:-1]
    assert not out.startswith("ERROR\t"), \
        "parser.kbr reported a parse error: %s" % out
    return out


# --- self-hosted compiler (kabir/selfhost/compiler.kbr) --------------------
# compiler.kbr is a self-contained front end + compiler: it embeds
# the lexer and the parser and prints the main code dict as JSON,
# byte-identical to compile_source(). The source file's path is
# handed to it on stdin, and it is also the code dict's "filename",
# so both sides compile the same file at the same path.

def canonical_bytecode_json(source, filename):
    """The Python compiler's code dict for *source*, as JSON."""
    return json.dumps(compile_source(source, filename))


def run_kabir_compiler(path):
    """Run kabir/selfhost/compiler.kbr over the .kbr file at *path*."""
    return subprocess.run(
        [sys.executable, "-m", "kabir", "run", COMPILER_KBR],
        input=path + "\n", capture_output=True, text=True, cwd=PKG_DIR,
        env=_subprocess_env())


def compile_source_with_kabir(path):
    """Compile the .kbr file at *path* with the Kabir compiler.

    Returns the code dict (the same shape ``compile_source`` returns),
    ready for ``VM.run_code``. Raises KabirError if the Kabir compiler
    crashes, writes to stderr, or reports an error. The type checker
    still runs on the Python side (see ``kabir run --selfhost``) — the
    Kabir compiler does the lexing, parsing, and compiling.
    """
    proc = run_kabir_compiler(path)
    if proc.returncode != 0:
        raise KabirError(
            "the Kabir compiler failed: %s"
            % (proc.stderr.strip() or "unknown error"),
            filename=path)
    if proc.stderr:
        raise KabirError(
            "the Kabir compiler wrote to stderr: %s" % proc.stderr.strip(),
            filename=path)
    out = proc.stdout
    if out.endswith("\n"):
        out = out[:-1]
    if out.startswith("ERROR\t"):
        raise KabirError(out[len("ERROR\t"):], filename=path)
    return json.loads(out)


def run_kabir_compiler_on_source(source):
    """Compile *source* with the Kabir compiler; return (kabir, py).

    The source is written to a temp .kbr file and that path is the
    filename on both sides, so the "filename" field in the code
    dict agrees too. Returns the two JSON strings (Kabir compiler,
    Python compiler) for a plain string comparison.
    """
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".kbr", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(source)
            path = handle.name
        proc = run_kabir_compiler(path)
        assert proc.returncode == 0, \
            "compiler.kbr crashed: %s" % proc.stderr
        assert proc.stderr == "", \
            "compiler.kbr wrote to stderr: %s" % proc.stderr
        out = proc.stdout
        if out.endswith("\n"):
            out = out[:-1]
        assert not out.startswith("ERROR\t"), \
            "compiler.kbr reported an error: %s" % out
        return out, canonical_bytecode_json(source, path)
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
