"""Статическая политика кода установщика: только stdlib ast, код не исполняется.

Это не замена рецензированию и полноценным анализаторам: проверяются лишь
явные запрещённые вызовы, shell и неиспользуемые импорты.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted([*ROOT.glob('shared/*.py'), *ROOT.glob('homensai_installer_client/*.py'),
                  *ROOT.glob('examples/demo_product/*.py')])
FORBIDDEN = {'eval', 'exec', 'compile', 'os.system', 'os.popen', 'pickle.load', 'pickle.loads',
             'marshal.load', 'marshal.loads', 'tempfile.mktemp', 'yaml.load', 'shelve.open'}
# Совместимый импорт старого пути клиента, на который опираются тесты.
REEXPORTS = {('examples/demo_product/installer_client.py', '_run_bounded')}


def _call_name(node):
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return function.value.id + '.' + function.attr
    return ''


@pytest.mark.parametrize('path', SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_no_dynamic_code_shell_or_unsafe_deserialization(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _call_name(node) in FORBIDDEN:
                findings.append((node.lineno, _call_name(node)))
            for keyword in node.keywords:
                if keyword.arg == 'shell' and not (isinstance(keyword.value, ast.Constant)
                                                   and keyword.value.value is False):
                    findings.append((node.lineno, 'shell'))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
            if any(name.split('.')[0] in ('pickle', 'marshal', 'shelve', 'yaml') for name in modules):
                findings.append((node.lineno, 'import'))
    assert findings == []


@pytest.mark.parametrize('path', SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_no_unused_imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    imported = {}
    exported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split('.')[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module != '__future__':
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__all__'
                                                  for target in node.targets):
            exported = {item.value for item in node.value.elts}
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    relative = path.relative_to(ROOT).as_posix()
    assert sorted(name for name in imported if name not in used and name not in exported
                  and (relative, name) not in REEXPORTS) == []
