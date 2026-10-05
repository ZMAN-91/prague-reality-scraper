"""Guard jobs install nothing: every module they run must work on a bare
Python with only the standard library and this repository's own code.

tools/ruian_due.py imported tools.build_ruian_index inside main(), which
imports common.net, which imports `requests`. Importing the module was fine;
running it failed - on every attempt from 1 October, 29 failure e-mails -
because the only September build had been forced and skipped the guard.

So this walks the imports statically, including the ones inside functions,
through every project module they reach, and it finds the guard jobs from
the workflow files themselves rather than from a list someone has to keep.
"""

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROJECT = {"common", "tools", "scrapers", "run"}


def jobs_without_install():
    found = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        spec = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for name, job in (spec.get("jobs") or {}).items():
            runs = [str(step.get("run", "")) for step in job.get("steps", [])]
            if any("pip install" in r for r in runs):
                continue
            for run in runs:
                for module in re.findall(r"python3? -m ([\w.]+)", run):
                    found.append((workflow.name, name, module))
    return found


def module_path(name):
    parts = name.split(".")
    file = ROOT.joinpath(*parts).with_suffix(".py")
    if file.exists():
        return file
    package = ROOT.joinpath(*parts, "__init__.py")
    return package if package.exists() else None


def third_party_imports(module, seen=None):
    seen = seen if seen is not None else set()
    if module in seen:
        return set()
    seen.add(module)
    path = module_path(module)
    if path is None:
        return set()
    bad = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for name in names:
            top = name.split(".")[0]
            if top in PROJECT:
                bad |= {f"{module} -> {b}" if "->" not in b else b
                        for b in third_party_imports(name, seen)}
            elif top not in sys.stdlib_module_names and top != "__future__":
                bad.add(f"{module} imports {top}")
    return bad


def test_there_are_guard_jobs_to_check():
    modules = {m for _, _, m in jobs_without_install()}
    assert {"tools.ruian_due", "tools.night_due", "tools.report_due"} <= modules


def test_no_guard_job_needs_a_package_it_did_not_install():
    problems = {f"{wf}:{job} {problem}"
                for wf, job, module in jobs_without_install()
                for problem in third_party_imports(module)}
    assert not problems, sorted(problems)
