#!/usr/bin/env python3
"""
locate_sources — map a produced table file back to the code line that wrote it.

Answers "which script made this table, and where?" so you can read the right
forty lines instead of hunting through a corpus of analysis code.

**This is a locator, not a dictionary.** It will not tell you what a variable
means. Analysis scripts are programs, not data: names are assembled at runtime
from macros, so a line reading

    esttab using "$OUTPUT/Mechanism_Capital_NO_`i'_$EXPOSURE.tex"

never contains the literal string it produces. Resolving `i` would require
interpreting control flow, which is not extraction. What IS mechanical is the
reverse direction: turn that path into a pattern and match real filenames
against it. So this reports WHERE a table came from, and leaves WHAT the
macros meant to you — with the surrounding code now cheap to read.

Standard library only.

Usage:
    locate_sources.py CODE_DIR [CODE_DIR ...] --tables TABLES_DIR
    locate_sources.py CODE_DIR --tables TABLES_DIR --json out.json
    locate_sources.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CODE_SUFFIXES = {".do", ".R", ".r", ".py"}

# Commands that write a file out. Anchored at the start of a statement so a
# mention inside a comment or a string is far less likely to match.
WRITERS = [
    ("esttab",    re.compile(r"^\s*\.?\s*esttab\b.*?\busing\b", re.I)),
    ("estout",    re.compile(r"^\s*\.?\s*estout\b.*?\busing\b", re.I)),
    ("outreg2",   re.compile(r"^\s*\.?\s*outreg2\b.*?\busing\b", re.I)),
    ("outsheet",  re.compile(r"^\s*\.?\s*outsheet\b.*?\busing\b", re.I)),
    ("export",    re.compile(r"^\s*\.?\s*export\s+\w+\b.*?\busing\b", re.I)),
    ("graph",     re.compile(r"^\s*\.?\s*graph\s+export\b", re.I)),
    ("file",      re.compile(r"^\s*\.?\s*file\s+(?:open|write)\b", re.I)),
    ("stargazer", re.compile(r"stargazer\s*\(.*\bout\s*=", re.I)),
    ("modelsum",  re.compile(r"modelsummary\s*\(.*\boutput\s*=", re.I)),
    ("kable",     re.compile(r"save_kable\s*\(", re.I)),
    ("writetex",  re.compile(r"\bwrite(?:Lines|_file|\.table|\.csv)\s*\(", re.I)),
]

# A quoted path, or an unquoted token after `using`.
QUOTED = re.compile(r'"([^"]+)"')
AFTER_USING = re.compile(r"\busing\s+([^\s,\"]+)", re.I)

# Stata macros: `local' and $global / ${global}. Also R paste()/file.path() and
# python f-string braces, treated the same way — an unresolvable hole.
MACRO = re.compile(r"`[^'`]*'|\$\{[^}]*\}|\$\w+|\{[^{}]*\}")


def path_to_pattern(raw: str) -> tuple[re.Pattern | None, int]:
    """Turn a possibly macro-laden output path into a filename regex.

    Returns (compiled pattern on the BASENAME, number of macro holes).
    A path that is entirely macro is useless as a matcher and returns None.
    """
    base = re.split(r"[\\/]", raw.strip())[-1]
    if not base:
        return None, 0
    holes = 0
    out = []
    pos = 0
    for m in MACRO.finditer(base):
        out.append(re.escape(base[pos:m.start()]))
        out.append(r"([^\\/]*?)")   # capturing: we score by how much it absorbs
        holes += 1
        pos = m.end()
    out.append(re.escape(base[pos:]))
    body = "".join(out)
    # M8: adjacent wildcards with nothing between them cause catastrophic
    # backtracking (9s per filename at eight holes). They are also redundant.
    body = re.sub(r"(?:\(\[\^\\\\/\]\*\?\)){2,}", r"([^\\/]*?)", body)
    # C7: the extension is not a literal anchor. "$OUT/`f'.tex" would otherwise
    # match every .tex file and be reported as a confident unique match.
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", MACRO.sub("", base))
    if not re.search(r"[A-Za-z0-9_]", stem):
        return None, holes
    return re.compile(r"^" + body + r"$"), holes


BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"(^|\s)(//+|\*|#).*$", re.M)
CONTINUATION = re.compile(r"\s*///.*?\n\s*")


def preprocess(text: str, suffix: str) -> list[tuple[int, str]]:
    """Return [(original_line_no, logical_line)].

    C9: matching line-at-a-time scanned `/* */` dead code while missing every
    `///`-continued live writer -- a complete inversion on realistic Stata.
    M6: comment stripping also stops commented-out R from registering.
    """
    # Blank out block comments, preserving line count so numbers stay right.
    text = BLOCK_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    lines = text.split("\n")
    out: list[tuple[int, str]] = []
    buf, start = "", None
    delim_semi = False
    for i, raw in enumerate(lines, 1):
        line = LINE_COMMENT.sub("", raw) if suffix in {".R", ".r", ".py"} else raw
        if re.match(r"\s*#delimit\s*;", line):
            delim_semi = True
            continue
        if re.match(r"\s*#delimit\s*cr", line):
            delim_semi = False
            continue
        if start is None:
            start = i
        buf += " " + line.strip()
        if line.rstrip().endswith("///"):
            buf = buf.rstrip()[:-3]
            continue
        if delim_semi and ";" not in line:
            continue
        out.append((start, buf.strip().rstrip(";")))
        buf, start = "", None
    if buf.strip():
        out.append((start or 1, buf.strip()))
    return out


def scan_code(paths: list[Path]) -> list[dict]:
    """Find every output-writing line. Returns one record per (line, path)."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix in CODE_SUFFIXES))
        elif p.is_file():
            files.append(p)
    writers, refused = [], []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in preprocess(text, f.suffix):
            kind = next((k for k, pat in WRITERS if pat.search(line)), None)
            if not kind:
                continue
            # M7: only the token after `using` is an output path. Every quoted
            # string on the line is not -- title("... .tex") and the payload of
            # `file write` were both being taken as paths.
            m_using = re.search(r'\busing\s+"([^"]+)"', line, re.I)
            if m_using:
                cands = [m_using.group(1)]
            elif re.search(r"\bfile\s+(?:open|write)\b", line, re.I):
                cands = []
            else:
                cands = QUOTED.findall(line)[:1] or AFTER_USING.findall(line)[:1]
            for raw in cands:
                pattern, holes = path_to_pattern(raw)
                if pattern is None:
                    refused.append({"file": str(f), "line": lineno,
                                    "raw_path": raw, "command": kind})
                    continue
                writers.append({
                    "file": str(f), "line": lineno, "command": kind,
                    "raw_path": raw, "pattern": pattern, "macro_holes": holes,
                    "source_line": " ".join(line.split())[:200],
                    "dir": re.split(r"[\\/]", raw.strip())[:-1],
                })
    if refused:
        # M9: a recognised writer whose path could not be turned into a usable
        # pattern used to vanish with no accounting.
        print(f"locate: {len(refused)} writer line(s) had an unusable output "
              f"path (too little literal text to match on):", file=sys.stderr)
        for r in refused[:5]:
            print(f"    {Path(r['file']).name}:{r['line']}  {r['raw_path']}",
                  file=sys.stderr)
    return writers


def match(writers: list[dict], tables: list[Path]) -> dict:
    """Join produced files to producing lines. Reports ambiguity, never hides it.

    Specificity is measured by how much of the filename the wildcards had to
    absorb, NOT by how many macros a path contains.

    No tiebreak is applied beyond that. A directory-proximity heuristic was
    tried and dropped: on a corpus where the same filename is written by
    scripts in several version directories, the candidates sit at equal tree
    distance, so it resolved nothing while adding a failure mode. Genuine
    ambiguity is reported as ambiguous.

    Why absorbed-length and not hole-count: a generic `..._$EXPOSURE.tex` has
    FEWER holes than `..._NO_`i'_$EXPOSURE.tex`, yet is the less specific
    match, because its single wildcard swallows "NO_0_EXP_COMMITTED" whole.
    Counting holes picks the wrong producer.
    """
    hits: dict[str, list[dict]] = {}
    for t in tables:
        scored = []
        for w in writers:
            m = w["pattern"].match(t.name)
            if not m:
                continue
            # C8: a fully literal directory is a checkable, falsifiable
            # constraint -- unlike a macro one. If it cannot be a suffix of
            # where the file actually sits, this writer did not produce it.
            lit = [d for d in w["dir"] if d not in ("", ".", "..")]
            if lit and not any(MACRO.search(d) for d in lit):
                parts = list(t.resolve().parts)[:-1]
                if lit != parts[-len(lit):]:
                    continue
            scored.append((sum(len(g or "") for g in m.groups()), w))
        if scored:
            best = min(s for s, _ in scored)
            # C10: a near-tie is ambiguity, not a winner. Collapsing it hid the
            # true producer behind an unrelated writer with more literal text.
            found = [w for s, w in scored if s - best <= 2]
        else:
            found = []
        hits[str(t)] = found
    used = {id(w) for v in hits.values() for w in v}
    return {
        "matched": {k: v for k, v in hits.items() if len(v) == 1},
        "ambiguous": {k: v for k, v in hits.items() if len(v) > 1},
        "unmatched": [k for k, v in hits.items() if not v],
        "unused_writers": [w for w in writers if id(w) not in used],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="locate_sources",
        description="Map produced tables back to the code line that wrote them.")
    ap.add_argument("code", nargs="*", help="directories or files of analysis code")
    ap.add_argument("--tables", help="directory of produced tables to match")
    ap.add_argument("--ext", default=".tex", help="produced-file extension (default .tex)")
    ap.add_argument("--json", help="write the full mapping here")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.code or not args.tables:
        ap.print_help()
        return 1

    writers = scan_code([Path(c) for c in args.code])
    tables = sorted(Path(args.tables).rglob("*" + args.ext))
    if not writers:
        print("locate: no output-writing lines found in the given code.")
        return 1
    res = match(writers, tables)

    n_m, n_a, n_u = len(res["matched"]), len(res["ambiguous"]), len(res["unmatched"])
    print(f"locate: {len(writers)} output-writing line(s) across the code; "
          f"{len(tables)} produced file(s)")
    print(f"  matched to exactly one writer : {n_m}")
    print(f"  ambiguous (>1 writer matches) : {n_a}")
    print(f"  no writer found               : {n_u}")
    print(f"  writers that produced nothing : {len(res['unused_writers'])}")

    for path, ws in sorted(res["matched"].items())[:12]:
        w = ws[0]
        holes = f"  [{w['macro_holes']} macro hole(s)]" if w["macro_holes"] else ""
        print(f"\n  {Path(path).name}")
        print(f"    -> {Path(w['file']).name}:{w['line']}  ({w['command']}){holes}")
        print(f"       {w['raw_path']}")
    if n_m > 12:
        print(f"\n  ... {n_m - 12} more matched (use --json for the full mapping)")

    if res["ambiguous"]:
        print(f"\n  AMBIGUOUS — more than one writer could have produced these:")
        for path, ws in sorted(res["ambiguous"].items())[:5]:
            print(f"    {Path(path).name}")
            for w in ws[:3]:
                print(f"      {Path(w['file']).name}:{w['line']}  {w['raw_path']}")
    if res["unmatched"]:
        print(f"\n  NO WRITER FOUND ({n_u}) — produced by code not scanned, "
              f"by hand, or by an unrecognised command:")
        for path in sorted(res["unmatched"])[:5]:
            print(f"    {Path(path).name}")

    if args.json:
        out = {
            "matched": {k: [{kk: vv for kk, vv in w.items() if kk != "pattern"}
                            for w in v] for k, v in res["matched"].items()},
            "ambiguous": {k: [{kk: vv for kk, vv in w.items() if kk != "pattern"}
                              for w in v] for k, v in res["ambiguous"].items()},
            "unmatched": res["unmatched"],
        }
        Path(args.json).write_text(json.dumps(out, indent=1))
        print(f"\n  mapping written to {args.json}")
    return 0


def self_test() -> int:
    cases = [
        # (raw path, filename, should_match)
        (r"$OUTPUT\Mechanism_Capital_NO_`i'_$EXPOSURE.tex",
         "Mechanism_Capital_NO_1_EXP_COMMITTED.tex", True),
        (r"$OUTPUT\Mechanism_Capital_NO_`i'_$EXPOSURE.tex",
         "Mechanism_Opacity_NO_1_EXP_COMMITTED.tex", False),
        (r"results/tab_main.tex", "tab_main.tex", True),
        (r"results/tab_main.tex", "tab_other.tex", False),
        (r"$OUT/w`c'_bothdw_s`c'.tex", "w20_bothdw_s20.tex", True),
        # A macro hole must not span a path separator.
        (r"$OUT/`sub'/tab.tex", "tab.tex", True),
    ]
    fails = 0
    # Regression: a generic pattern must LOSE to a more specific one whose
    # wildcards absorb less. Counting macro holes gets this backwards.
    generic, _ = path_to_pattern(r"$OUT\Mechanism_CREDIT_RISK_$EXPOSURE.tex")
    specific, _ = path_to_pattern(r"$OUT\Mechanism_CREDIT_RISK_NO_`i'_$EXPOSURE.tex")
    target = "Mechanism_CREDIT_RISK_NO_0_EXP_COMMITTED.tex"
    ga = sum(len(g or "") for g in generic.match(target).groups())
    sa = sum(len(g or "") for g in specific.match(target).groups())
    if sa < ga:
        print(f"  ok   specific writer beats generic ({sa} < {ga} absorbed)")
    else:
        print(f"  FAIL generic writer would win ({ga} vs {sa})"); fails += 1

    for raw, name, want in cases:
        pat, holes = path_to_pattern(raw)
        got = bool(pat and pat.match(name))
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {raw}  vs  {name}  -> {got} "
              f"({holes} hole(s))")
    # An all-macro path must be refused rather than matching everything.
    pat, _ = path_to_pattern("$OUTFILE")
    if pat is not None:
        print("  FAIL all-macro path was not refused"); fails += 1
    else:
        print("  ok   all-macro path refused (would match everything)")
    print(f"\nself-test: {len(cases) + 2 - fails}/{len(cases) + 2} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
