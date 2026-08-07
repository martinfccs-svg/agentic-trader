#!/usr/bin/env python3
"""preship_check.py — the checks that must pass before anything is deployed.

LOCAL TOOL. Never deployed.

Every check here exists because something got through without it.

  undefined locals          the `_alloc` NameError, 25 dead cycles (2026-08-03)
  cross-scope imports       `system_state` used in run() while imported only
                            inside a helper — a BOOT CRASH that the previous
                            scan called clean (2026-08-07)
  format arity              the GATES banner, 24 placeholders against 23 args
  format ORDER              same banner: counts matched while the argument
                            order was wrong, feeding a string to a %d

WHY THE IMPORT CHECK IS ITS OWN THING

The earlier scan collected imports with ast.walk, which descends into
functions. So a `import system_state` nested inside one function's try block
made the name look available everywhere — including at module scope, where it
was not. The check reported CLEAN on code that could not boot.

Python's scoping is the point: a function-local import binds a LOCAL name.
Another function referencing it gets a NameError. The fix is to resolve names
against the scope that will actually be in effect, which means not flattening
the tree.
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

BUILTINS = set(dir(builtins))


def _module_bindings(tree: ast.Module) -> set:
    """Names available at MODULE scope — top-level statements only."""
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            # Tuple/list unpacking counts: `EMA_FAST, EMA_SLOW = 50, 200`
            # binds BOTH names. Missing this produced 17 false positives on
            # regime_allocation alone — and false positives are how a check
            # gets muted, which costs more than the bugs it would catch.
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(n.target, ast.Name):
                out.add(n.target.id)
        elif isinstance(n, (ast.Try, ast.If, ast.With)):
            # top-level try/if blocks still bind at module scope
            for sub in ast.walk(n):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        out.add((a.asname or a.name).split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        out.add(a.asname or a.name)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx,
                                                              ast.Store):
                    out.add(sub.id)
    return out


def _local_bindings(fn) -> set:
    """Names bound inside one function, NOT visible to any other."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
    return out


def check_names(path: Path) -> list:
    """Names used in a function that nothing in scope binds."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [f"SYNTAX ERROR line {e.lineno}: {e.msg}"]
    mod = _module_bindings(tree)
    problems = []

    def visit(node, enclosing: set, label: str):
        """Walk one scope, carrying what ENCLOSING scopes bind.

        Closures were the first false positives this produced: a nested class
        inside _timed() referenced `_stage` from the enclosing function, and a
        flat per-function check called it undefined. Three warnings on correct
        code — and a check that cries wolf is a check people stop reading, so
        the scoping has to be right or the tool is worse than nothing.
        """
        own = _local_bindings(node) if not isinstance(node, ast.Module) else mod
        visible = enclosing | own
        nested = [n for n in ast.walk(node)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)) and n is not node]
        nested_ids = {id(n) for n in nested}

        def in_nested(n):
            for m in nested:
                for x in ast.walk(m):
                    if x is n:
                        return True
            return False

        for n in ast.walk(node):
            if id(n) in nested_ids:
                continue
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                continue
            if n.id in visible or n.id in BUILTINS or n.id == "__file__":
                continue
            if in_nested(n):
                continue
            problems.append(
                f"line {n.lineno} in {label}: '{n.id}' is not bound at module "
                f"scope, in an enclosing scope, or here")
        # A def nested inside a function is visible to code AFTER it in that
        # same function — including other nested defs. autopsy defines
        # system_of() inside a function and calls it from a sibling block;
        # treating each nested scope as isolated flagged that as undefined.
        sibling_names = {m.name for m in nested}
        for m in nested:
            child_scope = visible | sibling_names
            if isinstance(m, ast.ClassDef):
                visit(m, child_scope, f"class {m.name}")
            else:
                visit(m, child_scope, f"{m.name}()")

    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(fn, mod, f"{fn.name}()")
        elif isinstance(fn, ast.ClassDef):
            visit(fn, mod, f"class {fn.name}")
    return sorted(set(problems))


def check_use_before_assign(path: Path) -> list:
    """Names USED at a line before any assignment to them in the same scope.

    The check that was missing. `_age` was assigned 13 lines BELOW its use in
    a dict literal, so every swing_v2 route raised UnboundLocalError — caught
    by the enclosing try, which meant a valid setup was discarded silently on
    18 consecutive cycles. Nothing crashed and nothing traded.

    The scope check could not see it: `_age` IS bound in that function, just
    too late. Line order is the missing dimension.

    Deliberately conservative — it reports only when EVERY assignment in the
    scope is textually below the first use, and skips names that are also
    module-level or bound in a loop/comprehension, because those are the
    shapes where line order legitimately does not imply execution order. A
    check that guesses produces noise, and noise is how a check gets muted.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    mod = _module_bindings(tree)
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        loads, stores = {}, {}
        looped = set()
        for n in ast.walk(fn):
            if isinstance(n, (ast.For, ast.AsyncFor, ast.While,
                              ast.comprehension)):
                tgt = getattr(n, "target", None)
                for sub in ast.walk(n):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx,
                                                                ast.Store):
                        looped.add(sub.id)
                if tgt is not None:
                    for sub in ast.walk(tgt):
                        if isinstance(sub, ast.Name):
                            looped.add(sub.id)
            elif isinstance(n, ast.Name):
                bucket = stores if isinstance(n.ctx, ast.Store) else loads
                bucket.setdefault(n.id, []).append(n.lineno)
            elif isinstance(n, ast.arg):
                stores.setdefault(n.arg, []).append(n.lineno)
        for name, use_lines in loads.items():
            if name in mod or name in BUILTINS or name in looped:
                continue
            if name not in stores:
                continue                    # the scope check owns this case
            if min(stores[name]) > min(use_lines):
                out.append(
                    f"line {min(use_lines)} in {fn.name}(): '{name}' is used "
                    f"before its only assignment (line {min(stores[name])}) — "
                    f"UnboundLocalError at runtime")
    return sorted(set(out))


def check_formats(path: Path) -> list:
    """%-format calls whose placeholder count differs from the arg count."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        fmt = args = None
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("debug", "info", "warning", "error",
                                    "critical", "exception")
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            fmt, args = n.args[0].value, len(n.args) - 1
        elif (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)
              and isinstance(n.left, ast.Constant)
              and isinstance(n.left.value, str)):
            fmt = n.left.value
            args = (len(n.right.elts) if isinstance(n.right, ast.Tuple) else 1)
        if fmt is None or "%" not in fmt:
            continue
        # `log.info("%02d:%02d", *PAIR)` supplies an unknown number of args at
        # runtime. Counting the AST nodes gives 1 and the format wants 2 —
        # a false positive on correct code, found on intraday_scoring and
        # crypto_trader. When the count cannot be known statically, say
        # nothing rather than something wrong.
        if isinstance(n, ast.Call) and any(isinstance(a, ast.Starred)
                                           for a in n.args):
            continue
        if (isinstance(n, ast.BinOp) and isinstance(n.right, ast.Tuple)
                and any(isinstance(e, ast.Starred) for e in n.right.elts)):
            continue
        ph = len(re.findall(r"%(?:[-+ #0]*\d*(?:\.\d+)?)[sdfgexr]",
                            fmt.replace("%%", "")))
        if ph and args is not None and ph != args:
            out.append(f"line {n.lineno}: {ph} placeholders vs {args} args")
    return out


def main():
    paths = ([Path(p) for p in sys.argv[1:]] or
             sorted(Path(".").glob("*.py")))
    bad = 0
    for p in paths:
        issues = (check_names(p) + check_use_before_assign(p)
                  + check_formats(p))
        if issues:
            bad += 1
            print(f"\n{p}")
            for i in issues:
                print(f"  {i}")
    print(f"\n{len(paths)} file(s) checked, {bad} with problems")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
