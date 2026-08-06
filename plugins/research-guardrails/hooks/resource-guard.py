#!/usr/bin/env python3
"""Resource Guard Hook (PreToolUse)

Refuses to start work when the machine is already low on disk or memory,
so a long simulation run or a large checkout fails fast with a clear
message instead of dying halfway through and leaving a half-written
result set behind.

What this DOES do: check *currently available* disk and memory before a
command starts, and deny when either is below threshold.

What this does NOT do: predict a command's resource usage, or stop a
runaway process once started. A Monte Carlo run that balloons past
available RAM is a `ulimit`/timeout problem, not a pre-flight-check
problem. Do not read a pass here as "this command will fit."

Diagnostic commands (df, du, ls, free, vm_stat, git status, ps, top) are
always allowed through, so a denial is investigable rather than a
deadlock.

Two tiers, because one threshold cannot serve both cases. A bar high
enough to protect a pair-construction run would block trivial doc edits
whenever a browser is eating RAM; a bar low enough never to annoy would
never fire at all.

  FLOOR  - applies to everything. The machine is in trouble; don't start.
  HEAVY  - applies only to recognizably expensive work: by default anything
           under simulations/, or a sim_/benchmark_ script. A Monte Carlo
           run that materializes large pair sets and saves per-replication
           output makes "expensive" real, not notional. Projects whose heavy
           work lives elsewhere override the pattern (see below).

Thresholds and scope are overridable:
  CLAUDE_MIN_DISK_MB        floor, default 10240 (10 GB)
  CLAUDE_MIN_MEM_MB         floor, default 4096  (4 GB)
  CLAUDE_HEAVY_DISK_MB      heavy, default 25600 (25 GB)
  CLAUDE_HEAVY_MEM_MB       heavy, default 8192  (8 GB)
  CLAUDE_HEAVY_PATTERN      regex selecting heavy work; default below.
                            An invalid regex falls back to the default.
  CLAUDE_RESOURCE_GUARD=0   disables the hook entirely

Decision protocol: exit 0 + JSON on stdout
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "deny", "permissionDecisionReason": "..."}}
Fail-open: any internal error -> exit 0, no decision (allow).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

DEFAULT_MIN_DISK_MB = 10240    # 10 GB — floor for any work
DEFAULT_MIN_MEM_MB = 4096      # 4 GB
DEFAULT_HEAVY_DISK_MB = 25600  # 25 GB — required to start expensive work
DEFAULT_HEAVY_MEM_MB = 8192    # 8 GB

# Work that materializes large intermediate sets or writes per-replication
# Monte Carlo output. Matched against the whole command string. Override per
# project with CLAUDE_HEAVY_PATTERN when heavy work lives somewhere else.
DEFAULT_HEAVY_PATTERN = r"simulations/|\bsim_\w+\.(py|R|r|do)|\bbenchmark_\w+\.(py|R|r|do)"

try:
    HEAVY = re.compile(
        os.environ.get("CLAUDE_HEAVY_PATTERN") or DEFAULT_HEAVY_PATTERN,
        re.IGNORECASE,
    )
except re.error:
    # A malformed override must not disable the guard, and must not crash it.
    HEAVY = re.compile(DEFAULT_HEAVY_PATTERN, re.IGNORECASE)

# Commands that must never be blocked -- they are how you diagnose a
# denial. Matched on the leading token of any pipeline segment.
DIAGNOSTIC = {
    "df", "du", "ls", "free", "vm_stat", "ps", "top", "uptime",
    "cat", "head", "tail", "wc", "echo", "pwd", "which", "stat",
}


def deny(reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def free_disk_mb(path: str) -> float | None:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except Exception:
        return None


def available_mem_mb() -> float | None:
    """Available (not merely free) memory, best-effort per platform."""
    try:
        if sys.platform == "darwin":
            # page-size * (free + inactive + speculative) approximates
            # what the OS can hand out without swapping.
            out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                 timeout=3).stdout
            m = re.search(r"page size of (\d+) bytes", out)
            page = int(m.group(1)) if m else 4096

            def pages(label: str) -> int:
                mm = re.search(rf"{label}:\s+(\d+)\.", out)
                return int(mm.group(1)) if mm else 0

            total = pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")
            return total * page / (1024 * 1024)

        # Linux and friends
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        return None
    return None


def is_diagnostic(command: str) -> bool:
    """True only when EVERY segment of the command is diagnostic.

    Checking whether *any* segment is diagnostic would let expensive work
    ride along behind a harmless one: `python3 simulations/big.py | cat`
    would skip the resource check entirely. The question this answers is
    "is this command purely diagnostic?", so every segment must qualify.
    """
    segments = [s.strip() for s in re.split(r"\|\||&&|[|;\n]", command)]
    segments = [s for s in segments if s]
    if not segments:
        return False

    for seg in segments:
        toks = seg.split()
        if toks and toks[0] in DIAGNOSTIC:
            continue
        # `git status` and friends
        if len(toks) >= 2 and toks[0] == "git" and toks[1] in {"status", "diff", "log"}:
            continue
        return False
    return True


def main() -> int:
    if os.environ.get("CLAUDE_RESOURCE_GUARD", "1") == "0":
        return 0

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    tool = data.get("tool_name", "")
    if tool not in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return 0

    cmd = ""
    if tool == "Bash":
        cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
        if is_diagnostic(cmd):
            return 0

    heavy = bool(HEAVY.search(cmd))

    def threshold(env: str, default: float) -> float:
        try:
            return float(os.environ.get(env, default))
        except ValueError:
            return default

    if heavy:
        min_disk = threshold("CLAUDE_HEAVY_DISK_MB", DEFAULT_HEAVY_DISK_MB)
        min_mem = threshold("CLAUDE_HEAVY_MEM_MB", DEFAULT_HEAVY_MEM_MB)
        tier, knob = "heavy-work", "CLAUDE_HEAVY"
    else:
        min_disk = threshold("CLAUDE_MIN_DISK_MB", DEFAULT_MIN_DISK_MB)
        min_mem = threshold("CLAUDE_MIN_MEM_MB", DEFAULT_MIN_MEM_MB)
        tier, knob = "floor", "CLAUDE_MIN"

    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    disk = free_disk_mb(project)
    if disk is not None and disk < min_disk:
        deny(
            f"Blocked by resource-guard ({tier} tier): only {disk:,.0f} MB free "
            f"on the volume holding {project}, below the {min_disk:,.0f} MB "
            f"required. Free space, or raise {knob}_DISK_MB if the threshold is "
            f"wrong for this machine. Diagnostic commands (df, du, ls) still run."
        )
        return 0

    mem = available_mem_mb()
    if mem is not None and mem < min_mem:
        deny(
            f"Blocked by resource-guard ({tier} tier): only {mem:,.0f} MB memory "
            f"available, below the {min_mem:,.0f} MB required. Close something, "
            f"or raise {knob}_MEM_MB. This checks available memory now; it "
            f"cannot predict what this command will consume."
        )
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open -- never block work because of a hook bug
