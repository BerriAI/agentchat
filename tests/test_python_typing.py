"""Public native exports retain declaration-only Python typing information."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "python" / "agentchat"


def test_native_public_modules_have_companion_stubs():
    assert (PACKAGE / "py.typed").is_file()
    for path in PACKAGE.rglob("*.py"):
        if path.name.startswith("_") or path.name == "models.py":
            continue
        tree = ast.parse(path.read_text())
        native_exports = any(
            isinstance(node, ast.ImportFrom) and node.module == "agentchat._native"
            for node in tree.body
        )
        if native_exports:
            assert path.with_suffix(".pyi").is_file(), path


def test_public_stubs_parse_and_contain_only_method_declarations():
    stubs = sorted(PACKAGE.rglob("*.pyi"))
    assert stubs
    for path in stubs:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.returns is not None, (path, node.name)
                assert len(node.body) == 1, (path, node.name)
                assert isinstance(node.body[0], ast.Expr), (path, node.name)
                assert isinstance(node.body[0].value, ast.Constant), (path, node.name)
                assert node.body[0].value.value is Ellipsis, (path, node.name)
