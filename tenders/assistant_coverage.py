"""Discover user-triggered write operations and compare them with assistant tools."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
import inspect
from pathlib import Path
import re
import subprocess
import textwrap
from typing import Callable, Iterable

from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver

from .assistant_tools import available_capability_exclusions, available_tools


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MUTATION_CALLS = {
    "add", "bulk_create", "clear", "create", "delete", "get_or_create",
    "remove", "save", "set", "update", "update_or_create",
}
IGNORED_MODULE_PREFIXES = (
    "django.", "django.contrib.admin", "django.contrib.auth",
)
IGNORED_CALLBACK_MODULES = {
    "tenders.assistant_ui",
    "tenders.debug_tree",
}


@dataclass(frozen=True)
class UserOperation:
    identifier: str
    route_name: str
    route: str
    callback: str
    executor: str
    source_file: str
    action: str = ""
    reachable_executors: tuple[str, ...] = ()
    source_locations: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class CoverageResult:
    operation: UserOperation
    tool_ids: tuple[str, ...]
    exclusion_reason: str = ""

    @property
    def covered(self) -> bool:
        return bool(self.tool_ids or self.exclusion_reason)


def _callable_name(value: Callable) -> str:
    target = inspect.unwrap(value)
    return f"{target.__module__}.{target.__qualname__}"


def _project_function(value) -> Callable | None:
    if not inspect.isfunction(value):
        return None
    target = inspect.unwrap(value)
    module = getattr(target, "__module__", "")
    if not module or module.startswith(IGNORED_MODULE_PREFIXES):
        return None
    try:
        path = Path(inspect.getsourcefile(target) or "").resolve()
        path.relative_to(Path(settings.BASE_DIR).resolve())
    except (OSError, TypeError, ValueError):
        return None
    return target


@lru_cache(maxsize=None)
def _function_tree(value: Callable) -> ast.AST | None:
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(value)))
    except (OSError, TypeError):
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _resolve_call(function: Callable, node: ast.AST):
    namespace = function.__globals__
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if not isinstance(node, ast.Attribute):
        return None
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    value = namespace.get(current.id)
    for part in reversed(parts):
        value = getattr(value, part, None)
        if value is None:
            break
    return value


def _local_imports(function: Callable, tree: ast.AST) -> dict[str, object]:
    imports = {}
    package = function.__module__.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module_name = "." * node.level + (node.module or "")
            try:
                module = import_module(module_name, package=package)
            except (ImportError, TypeError, ValueError):
                continue
            for alias in node.names:
                value = getattr(module, alias.name, None)
                if value is not None:
                    imports[alias.asname or alias.name] = value
        elif isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    imports[alias.asname or alias.name.split(".", 1)[0]] = import_module(alias.name)
                except ImportError:
                    continue
    return imports


@lru_cache(maxsize=None)
def _direct_project_calls(function: Callable) -> tuple[Callable, ...]:
    tree = _function_tree(function)
    if tree is None:
        return ()
    local_imports = _local_imports(function, tree)
    calls = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolve_call(function, node.func)
        if resolved is None and isinstance(node.func, ast.Name):
            resolved = local_imports.get(node.func.id)
        target = _project_function(resolved)
        if target is not None and target is not inspect.unwrap(function):
            calls[_callable_name(target)] = target
    return tuple(calls.values())


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


@lru_cache(maxsize=None)
def _is_write_view(function: Callable) -> bool:
    tree = _function_tree(function)
    if tree is None:
        return False
    definition = next((node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if definition is not None:
        for decorator in definition.decorator_list:
            name = _decorator_name(decorator)
            if name == "require_POST":
                return True
            if name == "require_http_methods" and isinstance(decorator, ast.Call):
                if any(isinstance(value, ast.Constant) and value.value in WRITE_METHODS for value in ast.walk(decorator)):
                    return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"POST", "FILES"}:
            return True
        if isinstance(node, ast.Constant) and node.value in WRITE_METHODS:
            return True
    return False


@lru_cache(maxsize=None)
def _mutates(function: Callable, depth: int = 0) -> bool:
    if depth > 6:
        return False
    tree = _function_tree(function)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATION_CALLS:
                return True
            if _decorator_name(node.func) == "atomic":
                return True
    return any(_mutates(called, depth + 1) for called in _direct_project_calls(function))


def _service_executors(callback: Callable) -> tuple[Callable, ...]:
    """Return the first mutating project services reached from a URL callback."""
    callback = inspect.unwrap(callback)
    found = {}
    seen = set()

    def visit(function: Callable, depth: int = 0):
        name = _callable_name(function)
        if name in seen or depth > 6:
            return
        seen.add(name)
        for called in _direct_project_calls(function):
            called_module = called.__module__
            if called_module != callback.__module__ and _mutates(called):
                found[_callable_name(called)] = called
            elif called_module == callback.__module__:
                visit(called, depth + 1)

    visit(callback)
    if found:
        return tuple(found.values())
    return (callback,) if _mutates(callback) else ()


def _reachable_executors(callback: Callable) -> tuple[str, ...]:
    found = {}
    seen = set()

    def visit(function: Callable, depth: int = 0):
        name = _callable_name(function)
        if name in seen or depth > 8:
            return
        seen.add(name)
        found[name] = function
        for called in _direct_project_calls(function):
            visit(called, depth + 1)

    visit(inspect.unwrap(callback))
    return tuple(sorted(found))


def _source_location(function: Callable) -> tuple[str, int, int] | None:
    try:
        lines, start = inspect.getsourcelines(inspect.unwrap(function))
        path = Path(inspect.getsourcefile(inspect.unwrap(function)) or "").resolve()
        relative = str(path.relative_to(Path(settings.BASE_DIR).resolve()))
    except (OSError, TypeError, ValueError):
        return None
    return relative, start, start + len(lines) - 1


def _reachable_source_locations(callback: Callable) -> tuple[tuple[str, int, int], ...]:
    found = set()
    seen = set()

    def visit(function: Callable, depth: int = 0):
        name = _callable_name(function)
        if name in seen or depth > 8:
            return
        seen.add(name)
        location = _source_location(function)
        if location is not None:
            found.add(location)
        for called in _direct_project_calls(function):
            visit(called, depth + 1)

    visit(inspect.unwrap(callback))
    return tuple(sorted(found))


def _action_values(function: Callable) -> tuple[str, ...]:
    tree = _function_tree(function)
    if tree is None:
        return ()
    values = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(isinstance(item, ast.Name) and item.id == "action" for item in operands):
            continue
        values.update(
            item.value for item in operands
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return tuple(sorted(values))


def _walk_urlpatterns(patterns: Iterable, prefix: str = ""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _walk_urlpatterns(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            yield route, pattern


def discover_user_operations() -> list[UserOperation]:
    operations = []
    base_dir = Path(settings.BASE_DIR).resolve()
    for route, pattern in _walk_urlpatterns(get_resolver().url_patterns):
        callback = inspect.unwrap(pattern.callback)
        module = getattr(callback, "__module__", "")
        if module in IGNORED_CALLBACK_MODULES or module.startswith(IGNORED_MODULE_PREFIXES):
            continue
        if not _project_function(callback) or not _is_write_view(callback):
            continue
        route_name = pattern.name or _callable_name(callback)
        reachable_executors = _reachable_executors(callback)
        source_locations = _reachable_source_locations(callback)
        for executor in _service_executors(callback):
            source_path = Path(inspect.getsourcefile(executor) or "").resolve()
            try:
                source_file = str(source_path.relative_to(base_dir))
            except ValueError:
                source_file = str(source_path)
            actions = _action_values(executor) or ("",)
            for action in actions:
                executor_name = _callable_name(executor)
                suffix = f":{action}" if action else ""
                operations.append(UserOperation(
                    identifier=f"{route_name}:{executor_name}{suffix}",
                    route_name=route_name,
                    route=route,
                    callback=_callable_name(callback),
                    executor=executor_name,
                    source_file=source_file,
                    action=action,
                    reachable_executors=reachable_executors,
                    source_locations=source_locations,
                ))
    return sorted(operations, key=lambda item: item.identifier)


def audit_user_operation_coverage(user) -> list[CoverageResult]:
    tools_by_source = {}
    for tool in available_tools(user):
        source = tool.get("source")
        if source:
            tools_by_source.setdefault(source, []).append(tool["id"])
    exclusions = available_capability_exclusions()
    results = []
    for operation in discover_user_operations():
        tool_ids = {
            tool_id
            for source in operation.reachable_executors
            for tool_id in tools_by_source.get(source, ())
        }
        exclusion_reason = next((
            exclusions[key]
            for source in operation.reachable_executors
            for key in (f"{source}:{operation.action}", source)
            if key in exclusions
        ), "")
        results.append(CoverageResult(operation, tuple(sorted(tool_ids)), exclusion_reason))
    return results


DIFF_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
DIFF_RANGE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class BranchChanges:
    line_ranges: dict[str, tuple[tuple[int, int], ...]]
    added_text: dict[str, str]


def _parse_diff(diff: str) -> BranchChanges:
    ranges: dict[str, list[tuple[int, int]]] = {}
    added_lines: dict[str, list[str]] = {}
    current_file = ""
    for line in diff.splitlines():
        header = DIFF_HEADER.match(line)
        if header:
            current_file = header.group(1)
            continue
        changed = DIFF_RANGE.match(line)
        if changed and current_file:
            start = int(changed.group(1))
            count = int(changed.group(2) or "1")
            if count:
                ranges.setdefault(current_file, []).append((start, start + count - 1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.setdefault(current_file, []).append(line[1:])
    return BranchChanges(
        {path: tuple(values) for path, values in ranges.items()},
        {path: "\n".join(lines) for path, lines in added_lines.items()},
    )


def _git_diff(base_ref: str) -> str:
    repository = str(Path(settings.BASE_DIR).resolve())
    commands = (
        ["git", "diff", "--unified=0", "--no-color", f"{base_ref}...HEAD"],
        ["git", "diff", "--unified=0", "--no-color", "HEAD"],
    )
    output = []
    for command in commands:
        result = subprocess.run(command, cwd=repository, text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"Cannot compare with {base_ref}")
        output.append(result.stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repository, text=True, capture_output=True, check=False,
    )
    if untracked.returncode:
        raise ValueError(untracked.stderr.strip() or "Cannot inspect untracked files")
    for relative in untracked.stdout.splitlines():
        path = Path(repository, relative)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        if not lines:
            continue
        output.append(
            "\n".join([
                f"diff --git a/{relative} b/{relative}",
                "--- /dev/null",
                f"+++ b/{relative}",
                f"@@ -0,0 +1,{len(lines)} @@",
                *(f"+{line}" for line in lines),
            ])
        )
    return "\n".join(output)


def _overlaps(changed: tuple[int, int], source: tuple[int, int]) -> bool:
    return changed[0] <= source[1] and source[0] <= changed[1]


def changed_user_operations(base_ref: str, operations: list[UserOperation] | None = None) -> list[UserOperation]:
    """Return operations touched by branch code or newly referenced by UI/URL changes."""
    operations = discover_user_operations() if operations is None else operations
    changes = _parse_diff(_git_diff(base_ref))
    changed = []
    for operation in operations:
        source_changed = any(
            _overlaps(line_range, (start, end))
            for path, start, end in operation.source_locations
            for line_range in changes.line_ranges.get(path, ())
        )
        ui_text = "\n".join(
            text for path, text in changes.added_text.items()
            if path.endswith((".html", ".js", "urls.py"))
        )
        referenced_by_added_code = bool(operation.route_name and re.search(
            rf"(?<![\w-]){re.escape(operation.route_name)}(?![\w-])", ui_text,
        ))
        if source_changed or referenced_by_added_code:
            changed.append(operation)
    return changed
