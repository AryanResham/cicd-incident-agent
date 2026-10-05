"""Rule-based diagnosis (PLAN §B3 ③): category + SIMPLE / CORE / issue-only.

Rules decide first; Gemini may only make the decision *safer* (SIMPLE -> CORE).
When in doubt -> CORE. After a fix is written, `patch_kind()` looks at the patch
itself and upgrades a "SIMPLE" incident to CORE if the fix changes app logic.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field

from agent.collect import Evidence

# Kinds: what the agent is allowed to do.
SIMPLE = "simple"  # fix + auto-merge
CORE = "core"  # suggest a fix, a human merges
ISSUE_ONLY = "issue_only"  # no code fix possible (secrets, outages, ...)
RERUN = "rerun"  # flaky infrastructure: re-run the failed jobs once

CATEGORIES = ("test_failure", "code_error", "dependency", "docker_build", "deploy_failure", "config_secret", "flaky")

MAX_SIMPLE_LINES = 30  # a fix bigger than this is CORE


@dataclass
class Classification:
    category: str
    kind: str
    reason: str
    signals: list[str] = field(default_factory=list)


def _search(patterns: list[str], text: str) -> str | None:
    """First matching line for any of the patterns (case-insensitive)."""
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.M)
        if m:
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            return text[start: end if end != -1 else len(text)].strip()[:200]
    return None


# ---- patterns ----------------------------------------------------------------
SECRET_PATTERNS = [
    r"RENDER_DEPLOY_HOOK_URL\W.*(not set|empty|missing|required)",
    r"(secret|token|api key)\s+\S*\s*(is )?(not set|missing|empty)",
    r"Input required and not supplied",
    r"Bad credentials",
    r"Resource not accessible by integration",
    r"denied: (permission_denied|installation not allowed)",
    r"unauthorized: authentication required",
]
OUTAGE_PATTERNS = [r"payment required", r"billing", r"exceeded your .*quota", r"spending limit",
                   r"api\.render\.com.*\b5\d\d\b", r"\b5\d\d\b.*render"]
FLAKY_PATTERNS = [
    r"toomanyrequests", r"TLS handshake timeout", r"Connection reset by peer",
    r"Temporary failure in name resolution", r"runner has received a shutdown signal",
    r"No space left on device", r"lost communication with the server", r"ReadTimeoutError",
    r"i/o timeout", r"503 Service Unavailable", r"The operation was canceled",
]
DEPENDENCY_PATTERNS = [
    r"No matching distribution found", r"Could not find a version that satisfies",
    r"ResolutionImpossible", r"ERROR: Could not install", r"ZoneInfoNotFoundError",
    r"No time zone found with key",
]
CODE_ERROR_PATTERNS = [
    r"\bSyntaxError\b", r"\bIndentationError\b", r"\bTabError\b", r"NameError: name '\w+' is not defined",
    r"ImportError: cannot import name", r"AttributeError: module '[\w.]+' has no attribute",
    r"No module named '(app|tests)(\.\w+)*'",
]
DOCKER_PATTERNS = [
    r"failed to solve", r"COPY failed", r"failed to compute cache key", r'"/[^"]*": not found',
    r"dockerfile parse error", r"unknown instruction", r"failed to read dockerfile",
]
TEST_PATTERNS = [r"AssertionError", r"^FAILED tests/", r"\d+ failed", r"assert .* ==", r"E\s+assert"]
MISSING_MODULE = re.compile(r"No module named '([\w.]+)'")

INFRA_FILE = re.compile(r"(Dockerfile|\.dockerignore|requirements[\w-]*\.txt|render\.ya?ml|.*\.(toml|ini|cfg|env))$")
CONFIG_LINE = re.compile(
    r"^\s*(import\s|from\s+\S+\s+import\s|#|$)|os\.environ|getenv|^\s*[A-Z][A-Z0-9_]*\s*(:\s*\w+\s*)?="
)


# ---- diff helpers --------------------------------------------------------------
def changed_lines_by_file(diff: str) -> dict[str, list[str]]:
    """Parse Evidence.diff ('--- path (status)' + unified patch) into {path: [changed lines]}."""
    files: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        header = re.match(r"^--- (\S+) \(\w+\)$", line)
        if header:
            current = header.group(1)
            files[current] = []
        elif current and line[:1] in "+-" and not line.startswith(("+++", "---")):
            files[current].append(line[1:])
    return files


def diff_is_config_only(ev: Evidence) -> bool:
    """True if the stable..broken change only touched infra files or config-like lines in app/."""
    if not ev.changed_files:
        return False
    by_file = changed_lines_by_file(ev.diff)
    for path in ev.changed_files:
        if INFRA_FILE.match(path) or path.startswith("app/static/"):
            continue
        if path.startswith("app/") and path in by_file and all(CONFIG_LINE.search(x) for x in by_file[path]):
            continue
        return False
    return True


# ---- the rules ---------------------------------------------------------------
def classify(ev: Evidence) -> Classification:
    text = ev.failure_text() + "\n" + "\n".join(ev.notes)
    jobs = [j.lower() for j in ev.failed_job_names]
    production = ev.trigger in ("deploy_failure", "health")

    # 1. Things only a human can fix: secrets, permissions, billing, provider outages.
    hit = _search(SECRET_PATTERNS, text)
    if hit:
        return Classification("config_secret", ISSUE_ONLY, "a secret or permission is missing/wrong; "
                              "the agent is not allowed to set secrets", [hit])
    hit = _search(OUTAGE_PATTERNS, text)
    if hit:
        return Classification("deploy_failure", ISSUE_ONLY, "provider outage, quota or billing problem", [hit])

    # 2. Flaky CI infrastructure -> re-run once instead of "fixing" code.
    if ev.trigger == "ci_failure":
        hit = _search(FLAKY_PATTERNS, text)
        if hit:
            return Classification("flaky", RERUN, "transient CI infrastructure error", [hit])

    # 3. Dependencies: a third-party module that isn't installed, or pip can't resolve.
    for m in MISSING_MODULE.finditer(text):
        if not m.group(1).split(".")[0] in ("app", "tests"):
            return Classification("dependency", SIMPLE, f"package for module '{m.group(1)}' is missing "
                                  "from requirements.txt", [m.group(0)])
    hit = _search(DEPENDENCY_PATTERNS, text)
    if hit:
        return Classification("dependency", SIMPLE, "dependency cannot be installed/resolved", [hit])

    # 4. Python errors that stop the code from even loading.
    hit = _search(CODE_ERROR_PATTERNS, text)
    if hit:
        return Classification("code_error", SIMPLE, "syntax/import/name error", [hit])

    # 5. Docker build problems.
    hit = _search(DOCKER_PATTERNS, text)
    if hit or (any(j.startswith("build") for j in jobs) and not any(j.startswith("test") for j in jobs)):
        return Classification("docker_build", SIMPLE, "the Docker image does not build", [hit or "build job failed"])

    # 6. Failing assertions = the app does the wrong thing = business logic -> CORE.
    hit = _search(TEST_PATTERNS, text)
    if ev.junit_failures or hit or any(j.startswith("test") for j in jobs):
        signals = [t.name for t in ev.junit_failures[:5]] or [hit or "test job failed"]
        return Classification("test_failure", CORE, "tests fail on assertions: app behaviour is wrong", signals)

    # 7. Production failures (deploy/verify/health).
    if production:
        return _classify_production(ev, jobs)

    return Classification("test_failure" if not jobs else "code_error", CORE,
                          "unrecognised failure (when in doubt: CORE)", jobs)


def _classify_production(ev: Evidence, jobs: list[str]) -> Classification:
    failed_checks = [c["name"] for c in (ev.smoke or {}).get("checks", []) if not c.get("ok")]
    if any(j.startswith("deploy") for j in jobs):
        return Classification("deploy_failure", ISSUE_ONLY, "the deploy step itself failed (Render hook/API); "
                              "no code change can fix that", jobs)
    if ev.trigger == "health" and not ev.changed_files:
        return Classification("deploy_failure", ISSUE_ONLY, "the live app went down outside a deploy "
                              "(no code changed since the last stable version)", failed_checks)
    if failed_checks == ["latency"]:
        return Classification("deploy_failure", ISSUE_ONLY, "only the latency check failed (slow host, cold start)",
                              failed_checks)
    api_logic = {"round trip", "GET /api/todos"} & set(failed_checks) and "GET /health" not in failed_checks
    if diff_is_config_only(ev):
        return Classification("deploy_failure", SIMPLE, "the app breaks only when deployed and the change "
                              "touched only Docker/requirements/config", ev.changed_files + failed_checks)
    if api_logic:
        return Classification("deploy_failure", CORE, "the API answers but behaves wrongly in production",
                              failed_checks)
    return Classification("deploy_failure", CORE, "deployed app is unhealthy and the change touched app code "
                          "(when in doubt: CORE)", ev.changed_files + failed_checks)


def combine(rule: Classification, llm_kind: str | None) -> str:
    """Gemini only refines: it can turn SIMPLE into CORE, never the other way round."""
    if rule.kind == SIMPLE and llm_kind == CORE:
        return CORE
    return rule.kind


# ---- patch-based upgrade -------------------------------------------------------
def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    found = {}

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    found[name] = child
                walk(child, name + ".")
    walk(tree)
    return found


class _ForgetNames(ast.NodeTransformer):
    """Replace every identifier with '_' so a pure rename/typo fix compares equal."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = "_"
        return node

    def visit_arg(self, node):
        node.arg = "_"
        return node


def _same_ignoring_names(a: ast.AST, b: ast.AST) -> bool:
    import copy
    return ast.dump(_ForgetNames().visit(copy.deepcopy(a))) == ast.dump(_ForgetNames().visit(copy.deepcopy(b)))


def _is_config_stmt(stmt: ast.stmt) -> bool:
    """Import, or `NAME = <literal or env lookup>`."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
        src = ast.unparse(stmt.value)
        if "environ" in src or "getenv" in src:
            return True
        try:
            ast.literal_eval(stmt.value)
            return True
        except ValueError:
            return False
    return False


def _changed_statements(old: list[ast.stmt], new: list[ast.stmt]) -> list[ast.stmt]:
    old_src = [ast.unparse(s) for s in old]
    new_src = [ast.unparse(s) for s in new]
    by_src = {ast.unparse(s): s for s in old + new}
    changed = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_src, b=new_src).get_opcodes():
        if tag != "equal":
            changed += [by_src[s] for s in old_src[i1:i2] + new_src[j1:j2]]
    return changed


def _changed_line_count(old: str, new: str) -> int:
    return sum(1 for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def patch_kind(changes: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Look at a proposed fix {path: (old, new)} and decide whether it is still SIMPLE.

    CORE if: > 30 changed lines, a function's logic changes (beyond renames/typos,
    imports or env/config lookups), functions are added/removed, or > 2 functions change.
    """
    total = sum(_changed_line_count(old, new) for old, new in changes.values())
    if total > MAX_SIMPLE_LINES:
        return CORE, f"the fix changes {total} lines (> {MAX_SIMPLE_LINES})"

    touched: list[str] = []
    for path, (old, new) in changes.items():
        if not (path.startswith("app/") and path.endswith(".py")):
            continue  # requirements, Dockerfile, config, static files: fine for SIMPLE
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            return CORE, f"the fix leaves {path} with a syntax error"
        try:
            old_tree = ast.parse(old)
        except SyntaxError:
            if _changed_line_count(old, new) <= 5:
                continue  # fixing a syntax error with a tiny edit
            return CORE, f"large rewrite of {path} while fixing a syntax error"

        old_funcs, new_funcs = _functions(old_tree), _functions(new_tree)
        if set(old_funcs) != set(new_funcs):
            return CORE, f"the fix adds/removes functions in {path}"
        for name in old_funcs:
            a, b = old_funcs[name], new_funcs[name]
            if ast.dump(a) == ast.dump(b):
                continue
            touched.append(name)
            if _same_ignoring_names(a, b):
                continue  # only a name changed: typo / NameError fix
            stmts = _changed_statements(a.body, b.body)
            if a.decorator_list and ast.dump(ast.Module(a.decorator_list, [])) != \
                    ast.dump(ast.Module(b.decorator_list, [])):
                return CORE, f"the fix changes the route/decorator of {name}() in {path}"
            if not stmts or not all(_is_config_stmt(s) or "environ" in ast.unparse(s) or "getenv" in ast.unparse(s)
                                    for s in stmts):
                return CORE, f"the fix changes the logic of {name}() in {path}"

        top_old = [s for s in old_tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                  ast.ClassDef))]
        top_new = [s for s in new_tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                  ast.ClassDef))]
        for stmt in _changed_statements(top_old, top_new):
            if not (_is_config_stmt(stmt) or isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)):
                return CORE, f"the fix changes module-level code in {path}: {ast.unparse(stmt)[:80]}"

    if len(touched) > 2:
        return CORE, f"the fix touches several functions ({', '.join(touched)})"
    return SIMPLE, "the fix only touches imports, config, names or non-code files"
