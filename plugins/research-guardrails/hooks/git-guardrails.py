#!/usr/bin/env python3
"""
Git & Path Guardrails Hook (PreToolUse)

Blocks the small set of operations that are destructive or that the
template's own conventions forbid — before they run, not after. Adapted
from the `git-guardrails` pattern in mattpocock/skills, scoped so the
normal workflow (`/commit` pushes, stages specific files) still works.

Three checks, by tool:

  Bash — deny piping a download straight into a shell:
    - curl/wget ... | bash|sh|zsh  (executes unreviewed remote code)
    This cannot be expressed as a permission rule: Claude Code splits a
    compound command on `|` and matches each subcommand independently, so
    `Bash(curl * | bash)` never matches. The hook sees the raw string.

  Bash — deny destructive / convention-violating git:
    - git reset --hard            (silently discards work)
    - git clean -f / -fd / -fdx   (deletes UNTRACKED files — incl. data)
    - git push --force / -f       (clobbers remote history; --force-with-lease is allowed)
    - git add -A / --all / git add .   (the commit skill forbids blanket staging)
    - git checkout -- . / git restore .   (mass discard of working changes)

  Write/Edit/MultiEdit — warn on hardcoded machine paths in code:
    - absolute home paths (/Users/<u>, /home/<u>, C:\\Users\\) written into
      .R / .qmd / .do / .py files break replication packages. Warn by
      default; set CLAUDE_STRICT_PATHS=1 to hard-deny.

Decision protocol (modern PreToolUse): exit 0 + JSON
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "deny", "permissionDecisionReason": "..."}}.
Fail-open: any error → exit 0 with no decision (allow).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Optional git GLOBAL options that can sit between `git` and the subcommand —
# `git -C <dir>`, `git -c k=v`, `--git-dir=`, `--work-tree=`, `--no-pager`,
# `--paginate`. Without this prefix, `git -C /repo reset --hard` (or `git -c x=y
# clean -fd`) silently bypasses every guardrail.
_GO = (r"(?:-C\s+\S+\s+|-c\s+\S+\s+|--git-dir(?:=\S+\s+|\s+\S+\s+)|"
       r"--work-tree(?:=\S+\s+|\s+\S+\s+)|--no-pager\s+|--paginate\s+|-p\s+)*")

# (compiled pattern, human reason, safe alternative)
GIT_DENY = [
    (re.compile(r"\bgit\s+" + _GO + r"reset\s+--hard\b"),
     "git reset --hard discards uncommitted work irrecoverably.",
     "Use `git stash` (recoverable) or reset specific paths."),
    (re.compile(r"\bgit\s+" + _GO + r"clean\b.*(--force\b|(?<![\w-])-[a-z]*f)"),
     "git clean -f/--force deletes UNTRACKED files — including data not yet committed.",
     "Inspect with `git clean -n` first; delete specific paths by hand."),
    (re.compile(r"\bgit\s+" + _GO + r"push\b.*(--force(?![\w-])|(?<!-)\s-f\b)"),
     "git push --force clobbers remote history.",
     "Use `git push --force-with-lease` if you truly must rewrite a branch."),
    (re.compile(r"\bgit\s+" + _GO + r"add\s+(?:--\s+)?(-A\b|--all\b|\.(?:\s|$)|:/)"),
     "Blanket staging (git add -A / . / -- . / :/) can stage data, secrets, or settings.local.json. "
     "The /commit skill forbids it.",
     "Stage specific files: `git add path/to/file ...`."),
    (re.compile(r"\bgit\s+" + _GO + r"(checkout|restore)\s+(--\s+)?\.(?:\s|$)"),
     "Mass discard of working-tree changes is irreversible.",
     "Discard specific files, or `git stash` to keep them recoverable."),
    # NOTE: merge / rebase / cherry-pick / revert / branch -D are deliberately
    # NOT denied here. They are `ask` rules in ~/.claude/settings.json, so they
    # prompt with the exact command instead of being refused outright. A hook
    # deny beats an ask rule, so re-adding them here would silently kill those
    # prompts. Gate them there, not here.
    (re.compile(r"\bgit\s+" + _GO + r"filter-branch\b|\bgit\s+" + _GO + r"filter-repo\b"),
     "History rewriting across the whole repository is effectively irreversible "
     "once pushed.",
     "Do this by hand, outside Claude Code, with a backup clone."),
]

HARDCODED_PATH = re.compile(r"(/Users/[^/\s'\")]+|/home/[^/\s'\")]+|[A-Za-z]:\\\\Users\\\\[^\\\s'\"]+)")
CODE_EXT = {".R", ".r", ".qmd", ".do", ".py", ".Rmd"}

# Download piped straight into a shell. Permission rules cannot catch this
# (compound commands are split on `|` and matched per-subcommand), so it is
# enforced here, where the whole command string is visible.
PIPE_TO_SHELL = re.compile(
    r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(bash|sh|zsh|ksh|dash|python3?|perl|ruby)\b"
)


def decide(decision: str, reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def deny(reason: str) -> None:
    decide("deny", reason)


def ask(reason: str) -> None:
    decide("ask", reason)


def is_recursive_rm(command: str) -> bool:
    """True when any segment deletes a tree (-r / -R / --recursive).

    Backstop for the `Bash(rm -r*)` ask rules, which match a literal prefix and
    so miss split flags (`rm -f -r x`), the long form (`rm --recursive x`), and
    unusual orderings. A plain `rm file.txt` is deliberately not caught — the
    recursive flag is what turns a mistake into a lost directory.
    """
    for seg in re.split(r"\|\||&&|[|;\n]", command):
        toks = seg.strip().split()
        if "rm" not in toks:
            continue
        for t in toks[toks.index("rm") + 1:]:
            if t == "--":          # end of options; rest are paths
                break
            if t == "--recursive":
                return True
            if re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", t):
                return True
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if PIPE_TO_SHELL.search(cmd):
            deny(
                "Blocked by git-guardrails: piping a download directly into a shell "
                "executes remote code that nobody has read. Download to a file, read "
                "it, then run it deliberately. "
                "(override: run it in a terminal yourself, outside Claude Code.)"
            )
            return 0
        for pat, reason, alt in GIT_DENY:
            if pat.search(cmd):
                deny(f"Blocked by git-guardrails: {reason} {alt} "
                     f"(override: run it in a terminal yourself, outside Claude Code.)")
                return 0
        if is_recursive_rm(cmd):
            ask("Recursive delete: this removes a directory tree, not a single "
                "file. Check the target path before approving.")
            return 0
        return 0

    if tool in ("Write", "Edit", "MultiEdit"):
        fp = ti.get("file_path", "") or ""
        if Path(fp).suffix not in CODE_EXT:
            return 0
        # Collect every string that becomes file content: Write.content,
        # Edit.new_string, and EACH MultiEdit edit's new_string (a list).
        candidates = []
        for k in ("content", "new_string"):
            v = ti.get(k)
            if isinstance(v, str):
                candidates.append(v)
        for e in ti.get("edits", []) or []:
            v = (e or {}).get("new_string")
            if isinstance(v, str):
                candidates.append(v)
        for content in candidates:
            m = HARDCODED_PATH.search(content)
            if m:
                msg = (f"Hardcoded machine path '{m.group(0)}' in {Path(fp).name} "
                       f"breaks replication packages. Use here::here(), relative paths, "
                       f"or a config variable.")
                if os.environ.get("CLAUDE_STRICT_PATHS", "") == "1":
                    deny(f"Blocked (CLAUDE_STRICT_PATHS=1): {msg}")
                else:
                    sys.stderr.write(f"[git-guardrails] WARNING: {msg}\n")
                break  # one finding per call is enough
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open
