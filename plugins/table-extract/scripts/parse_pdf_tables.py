#!/usr/bin/env python3
"""
parse_pdf_tables — extract regression tables from a PDF into the same
coefficient schema `parse_tables.py` produces, for papers where no LaTeX
source exists.

**Confidence here is genuinely lower and must stay visible.** A LaTeX table
carries its own structure: `&` says where a column ends. A PDF carries only
glyphs at coordinates, so columns are INFERRED from whitespace geometry. A
value that lands in the wrong column is the failure mode, and unlike the
LaTeX path it cannot be ruled out by construction. Everything emitted is
tagged `source_type: "pdf"` with a confidence below 1.0, so a projection can
never present it as though it were read from source.

Uses `pdftotext -layout` (poppler), which preserves column geometry as runs
of spaces. No Python PDF dependency.

Scanned PDFs are out of scope: with no text layer there is nothing to parse,
and this reports that rather than attempting OCR.

Usage:
    parse_pdf_tables.py PAPER.pdf [--pages 1-40] [--json out.json]
    parse_pdf_tables.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_tables import is_stat_label  # noqa: E402  (shared, so it cannot drift)

SPEC_ROW = re.compile(r"^\s*(\(\s*\d+\s*\)\s*){2,}\s*$")
NUM = re.compile(r"^[-+(\[]?\s*\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?\s*[)\]]?\*{0,5}$")
STARS = re.compile(r"\*{1,5}$")
UNC_WRAPPED = re.compile(r"^\s*[\(\[].*[\)\]]\s*$")
UNC_LABEL = re.compile(
    r"^\s*\(?\s*(robust|clustered|driscoll[- ]?kraay|newey[- ]?west|bootstrap\w*)?"
    r"\s*\(?\s*(s\.?\s?e\.?s?|std\.?\s*(err|error|dev)\w*|standard\s+errors?"
    r"|t[-\s]?stats?\.?|z[-\s]?stats?\.?|p[-\s]?values?)\.?\s*\)?\.?\s*$", re.I)

UNC_PATTERNS = [
    ("se", re.compile(r"standard error|std\.? ?err", re.I)),
    ("tstat", re.compile(r"\bt[-\s]?stat|\bt[-\s]?value|\bz[-\s]?stat", re.I)),
    ("pval", re.compile(r"p[-\s]?value", re.I)),
    ("ci", re.compile(r"confidence interval", re.I)),
]
DELIM_WORD = re.compile(r"parenthes|bracket", re.I)


def pdf_to_layout_text(pdf: Path, pages: str | None) -> str:
    """Render the PDF preserving column geometry. Raises on a missing tool."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext not found (install poppler)")
    cmd = ["pdftotext", "-layout"]
    if pages:
        first, _, last = pages.partition("-")
        cmd += ["-f", first, "-l", last or first]
    cmd += [str(pdf), "-"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {out.stderr.strip()[:200]}")
    return out.stdout


def column_centres(spec_line: str) -> list[tuple[int, int]]:
    """Column spans from the `(1) (2) ...` header row: (start, end) per column."""
    return [(m.start(), m.end()) for m in re.finditer(r"\(\s*\d+\s*\)", spec_line)]


def stub_boundary(spans: list[tuple[int, int]]) -> float:
    """Where the row label ends and the first column begins.

    Derived from the column geometry — half the inter-column pitch to the left
    of the first column's centre — rather than a fixed margin. A constant
    margin either truncates long row labels or steals the first number from
    its column, depending on the paper's layout.
    """
    centres = [(a + b) / 2 for a, b in spans]
    if len(centres) < 2:
        return spans[0][0]
    return centres[0] - (centres[1] - centres[0]) / 2


def split_by_columns(line: str, spans: list[tuple[int, int]]) -> list[str]:
    """Assign each whitespace-delimited token to the nearest column centre.

    Tokens are placed by character midpoint, which is what `-layout`
    preserves. This is the inference LaTeX does not require, and the reason
    PDF confidence is capped below 1.0.
    """
    cells = [""] * len(spans)
    counts = [0] * len(spans)
    centres = [(a + b) / 2 for a, b in spans]
    left = stub_boundary(spans)
    for m in re.finditer(r"\S+", line):
        mid = (m.start() + m.end()) / 2
        if mid < left:                  # left of column one: part of the stub
            continue
        i = min(range(len(centres)), key=lambda k: abs(centres[k] - mid))
        cells[i] = (cells[i] + " " + m.group(0)).strip()
        if re.search(r"\d", m.group(0)):
            counts[i] += 1
    # C1: two numeric tokens in one column means the geometry inferred from the
    # header does not fit this row -- typically a page break where the layout
    # changed. Silently, this destroyed values AND shifted the survivors onto
    # the wrong specification, with est and unc misaligned differently from
    # each other. Report it; the caller refuses.
    return cells, any(c > 1 for c in counts)


def stub_of(line: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return line.strip()
    left = stub_boundary(spans)
    return " ".join(m.group(0) for m in re.finditer(r"\S+", line)
                    if (m.start() + m.end()) / 2 < left).strip()


def parse_value(cell: str) -> dict:
    raw = cell.strip()
    if not raw:
        return {"value": None, "stars": 0, "wrapped": False}
    stars = 0
    m = STARS.search(raw)
    if m:
        stars = len(m.group(0))
        raw = raw[:m.start()].strip()
    wrapped = bool(UNC_WRAPPED.match(raw))
    body = raw.strip("()[]").replace(",", "").replace("−", "-").strip()
    try:
        return {"value": float(body), "stars": stars, "wrapped": wrapped}
    except ValueError:
        return {"value": None, "stars": stars, "wrapped": wrapped, "text": raw}


def detect_uncertainty_type(notes: str) -> str:
    hits = [(n, m.start()) for n, p in UNC_PATTERNS if (m := p.search(notes))]
    if not hits:
        return "unknown"
    if len(hits) == 1:
        return hits[0][0]
    anchor = DELIM_WORD.search(notes)
    if anchor:
        return min(hits, key=lambda h: abs(h[1] - anchor.start()))[0]
    order = [n for n, _ in UNC_PATTERNS]
    return min(hits, key=lambda h: order.index(h[0]))[0]


def parse_tables(text: str, src: str) -> tuple[list[dict], list[dict]]:
    """Find and parse every `(1) (2) ...`-headed table. Returns (tables, residue)."""
    lines = text.splitlines()
    tables, residue = [], []
    starts = [i for i, l in enumerate(lines) if SPEC_ROW.match(l)]
    for si in starts:
        spans = column_centres(lines[si])
        if len(spans) < 2:
            continue
        # Header block: text rows between the spec row and the first data row.
        header: list[list[str]] = []
        terms, est, unc, stars = [], [], [], []
        geometry_broken = False
        notes_parts, pending = [], None
        i = si + 1
        blanks = 0
        while i < len(lines) and blanks < 3:
            line = lines[i]
            i += 1
            if not line.strip():
                blanks += 1
                continue
            blanks = 0
            if SPEC_ROW.match(line) or "\x0c" in line:
                break
            stub = stub_of(line, spans)
            raw_cells, merged = split_by_columns(line, spans)
            if merged:
                geometry_broken = True
            cells = [parse_value(c) for c in raw_cells]
            filled = [c for c in cells if c["value"] is not None or c.get("text")]
            nums = [c for c in filled if c["value"] is not None]
            if not filled:
                if stub:
                    notes_parts.append(stub)
                continue
            wrapped = [c for c in nums if c["wrapped"]]
            if nums and len(wrapped) == len(nums) and (
                    not stub or (pending is not None and UNC_LABEL.match(stub))):
                # C6: a LABELLED uncertainty row ("Robust s.e.") was being
                # reclassified as a note, losing every standard error.
                if pending is not None:
                    if any(v is not None for v in unc[pending]):
                        geometry_broken = True   # C3: a second uncertainty row
                    else:
                        unc[pending] = [c["value"] for c in cells]
                    pending = None               # C3: consume it exactly once
                continue
            # C2: a stub-less row holding exactly one bare integer and no
            # stars is a page number, not an estimate.
            if (not stub and len(nums) == 1 and not wrapped
                    and not any(c["stars"] for c in cells)
                    and float(nums[0]["value"]).is_integer()):
                continue
            # C5: a stub-less row whose numbers are MOSTLY wrapped is a
            # malformed uncertainty row, not a coefficient row.
            if not stub and wrapped and len(wrapped) < len(nums):
                geometry_broken = True
                continue
            if nums and len(wrapped) < len(nums):
                if is_stat_label(stub.lower().rstrip(":").strip()):
                    pending = None      # Observations / R^2 / N are not coefficients
                    continue
                terms.append(stub or f"__unlabelled_{len(terms)}")
                est.append([c["value"] for c in cells])
                stars.append([c["stars"] for c in cells])
                unc.append([None] * len(spans))
                pending = len(terms) - 1
                continue
            if not terms:
                header.append([c.get("text", "") or "" for c in cells])
                continue
            # Text-only row after the coefficients: a note or an annotation.
            # Without this it matched no branch and was silently dropped.
            notes_parts.append(" ".join(line.split()))
        # Published tables put the note above as often as below, so scan both.
        # C4: an unbounded upward scan inherited the PREVIOUS table's note,
        # and a wrong inherited type scored higher than an honest "unknown".
        lo = max(0, si - 8)
        for j in range(si - 1, lo - 1, -1):
            if SPEC_ROW.match(lines[j]) or "\x0c" in lines[j]:
                lo = j + 1
                break
        head = " ".join(l.strip() for l in lines[lo:si])
        tail = " ".join(l.strip() for l in lines[i:i + 6])
        notes = " ".join(notes_parts + [head, tail])
        if geometry_broken:
            residue.append({
                "table_id": f"{src}#p{si + 1}", "src": src, "line": si + 1,
                "reason": "column geometry does not fit every row (two numeric "
                          "tokens landed in one column) — values would be lost "
                          "and survivors misattributed",
                "raw": lines[si][:200]})
            continue
        if not terms:
            residue.append({"table_id": f"{src}#p{si + 1}", "src": src,
                            "line": si + 1,
                            "reason": "no coefficient rows under the column header",
                            "raw": lines[si][:200]})
            continue
        n = len(spans)
        conf = 0.80                                   # PDF ceiling: columns inferred
        flags = ["columns inferred from whitespace geometry, not delimiters"]
        utype = detect_uncertainty_type(notes)
        if utype == "unknown":
            conf -= 0.10
            flags.append("uncertainty type not stated")
        if not header:
            conf -= 0.05
            flags.append("no dependent-variable header row")
        tables.append({
            "table_id": f"{src}#p{si + 1}",
            "src": {"file": src, "line": si + 1},
            "spec_labels": [lines[si][a:b].strip() for a, b in spans],
            "dep_vars": ([" ".join(x).strip() for x in zip(*header)] if header
                         else [""] * n),
            "terms": terms, "est": est, "unc": unc, "stars": stars,
            "n_cols": n, "notes": notes[:400], "uncertainty_type": utype,
            "source_type": "pdf", "confidence": round(conf, 2), "flags": flags,
        })
    return tables, residue


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="parse_pdf_tables")
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--pages", help="page range, e.g. 20-40")
    ap.add_argument("--json", help="write extracted tables here")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.pdf:
        ap.print_help()
        return 1

    pdf = Path(args.pdf)
    try:
        text = pdf_to_layout_text(pdf, args.pages)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if len(text.strip()) < 200:
        print("This PDF has no usable text layer — it is probably a scan. "
              "OCR is out of scope; nothing was extracted.", file=sys.stderr)
        return 2

    tables, residue = parse_tables(text, pdf.name)
    print(f"pdf-extract: {len(tables)} table(s), {len(residue)} residue "
          f"(from {len(text.splitlines()):,} laid-out lines)")
    for t in tables:
        ncoef = sum(1 for r in t["est"] for v in r if v is not None)
        print(f"\n  {t['table_id']}  cols={t['n_cols']}  terms={len(t['terms'])}  "
              f"coefs={ncoef}  unc={t['uncertainty_type']}  conf={t['confidence']}")
        print(f"    dep vars: {[d[:26] for d in t['dep_vars']][:5]}")
        for k, term in enumerate(t["terms"][:3]):
            vals = " ".join("." if v is None else f"{v:+.4f}{'*' * s}"
                            for v, s in zip(t["est"][k], t["stars"][k]))
            print(f"    {term[:34]:34s} {vals[:88]}")
    for r in residue[:3]:
        print(f"\n  RESIDUE line {r['line']}: {r['reason']}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"schema": 1, "tables": tables, "residue": residue}, indent=1))
        print(f"\n  written to {args.json}")
    return 0


def self_test() -> int:
    sample = "\n".join([
        "                              (1)          (2)          (3)",
        " Dependent Variable:         Rate         Rate          Emp",
        " Rel Exist x MP Shock    -0.747***    -0.418**     -0.712***",
        "                           (-4.46)      (-2.08)      (-3.84)",
        " Rel Exist               -0.194***    -0.051***                ",
        "                          (-22.34)      (-3.01)                ",
        " Observations              12,345       12,345       11,000",
        "",
        "Notes: t-statistics clustered by bank in parentheses.",
    ])
    tables, residue = parse_tables(sample, "sample.pdf")
    fails = 0
    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" +
              ("" if ok else f"  want {want!r}"))
    if not tables:
        print("  FAIL no table parsed"); return 1
    t = tables[0]
    check("n_cols", t["n_cols"], 3)
    check("terms", t["terms"][:2], ["Rel Exist x MP Shock", "Rel Exist"])
    check("row 1 estimates", t["est"][0], [-0.747, -0.418, -0.712])
    check("row 1 stars", t["stars"][0], [3, 2, 3])
    check("row 1 uncertainty", t["unc"][0], [-4.46, -2.08, -3.84])
    check("sparse row 2", t["est"][1], [-0.194, -0.051, None])
    check("uncertainty type", t["uncertainty_type"], "tstat")
    check("source_type", t["source_type"], "pdf")
    check("confidence below 1", t["confidence"] < 1.0, True)
    print(f"\nself-test: {9 - fails}/9 passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
