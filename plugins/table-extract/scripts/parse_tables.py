#!/usr/bin/env python3
"""
parse_tables — deterministic extraction of regression tables from LaTeX
source (and, optionally, paired analysis logs) into a compact coefficient
store.

The point is context economy: a high-tier model reads a small *projection*
of the store instead of hundreds of kilobytes of table source. Parsing is
fully deterministic — no model is involved at any stage.

Core principle: FAIL LOUDLY, NEVER QUIETLY. This feeds numbers to a reader
that will not see the source. A silently mis-parsed cell is far worse than
an unparsed table, so every tabular block either yields a verified table or
lands in `residue[]` with its raw span. Counts must balance.

Standard library only — it must run in any project with zero setup.

Usage:
    parse_tables.py extract PATH [PATH ...]   # parse into the store
    parse_tables.py project --terms treat     # emit a small markdown pivot
    parse_tables.py verify --sample 50        # source round-trip check
    parse_tables.py stats                     # store summary
    parse_tables.py --self-test               # golden fixtures
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
# Minimum coefficients (and distinct values) needed to bind a log to a column.
MIN_FINGERPRINT_TERMS = 3
# Paths that LOOK superseded. Used only to REPORT, never to exclude:
# deciding a directory is stale is a relevance judgement, and this tool
# does not make those. Excluding by default also bypassed the conservation
# invariant entirely — a filtered file never entered the parsed/residue
# accounting at all, which is precisely the silent loss that rule forbids.
VERSIONISH_RE = re.compile(r"legacy|deprecated|_?old(?:$|[/_.])|_archive|backup",
                           re.I)

# Row-level LaTeX noise that carries no data.
RULE_RE = re.compile(
    r"\\(?:top|mid|bottom|cmid|)rule(?:\([a-z]+\))?(?:\{[^{}]*\})?"
    r"(?:\[[^\]]*\])?"
    r"|\\hline(?:\[[^\]]*\])?|\\addlinespace(?:\[[^\]]*\])?"
    r"|\\rule\{[^{}]*\}\{[^{}]*\}"
    r"|\\(?:noalign|vspace|smallskip|medskip|bigskip)(?:\{[^{}]*\})?"
)
LEADING_OPT_RE = re.compile(
    r"^\s*\[\s*-?[\d.]*\s*(?:pt|em|ex|ch|cm|mm|in|bp|dd|cc|sp|\\[a-zA-Z]+)?\s*\]")
# C2: an unescaped % comments out the rest of the line. Without this, a
# commented-out row is parsed as a live estimate and a comment without a
# trailing \\ swallows the label of the row beneath it.
COMMENT_RE = re.compile(r"(?<!\\)%.*?$", re.M)
SYM_RE = re.compile(r"\\sym\s*\{(\*+)\}")
OVERLAY_RE = re.compile(
    r"\\(?:onslide|uncover|visible|invisible|alt|only|pause)\b|<\d+[->]")
SPEC_LABEL_RE = re.compile(r"^\(\s*\d+\s*\)$")

# Trailing summary-statistic row labels (normalised, lowercase).
STAT_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"^n$", r"^observations?$", r"^obs\.?$", r"^no\.? ?of ?obs",
        r"^num\.? ?obs", r"^sample size$",
        r"^(adj\.?|adjusted|within|pseudo|overall)? ?r[\-\^ ]?2?(squared)?$",
        r"^r ?squared$", r"^mean ?(of )?dep", r"^s\.?d\.? ?(of )?dep",
        r"^std\.? ?dev", r"^log[\- ]?likelihood$", r"^ll$", r"^aic$", r"^bic$",
        r"^f[\- ]?stat", r"^f$", r"^rmse$", r"^clusters?$",
        r"^no\.? ?of ?clusters", r"^first[\- ]stage f", r"^kp ?f", r"^df$",
    )
]


AMBIGUOUS_STAT = {"n", "r", "f", "df", "ll", "obs"}


def is_stat_label(norm: str, seen_coefficients: bool = True) -> bool:
    """True when a row label names a summary statistic, not a coefficient.

    M9: single letters are also perfectly good regressor names (an interest
    rate `r`, a population `N`). They only count as summary labels once
    coefficient rows have been seen, mirroring the fixed-effect guard.
    """
    if norm.strip() in AMBIGUOUS_STAT and not seen_coefficients:
        return False
    flat = re.sub(r"[\s.]+", " ", norm.replace("$", "").replace("^", "")).strip()
    return any(p.match(norm) or p.match(flat) for p in STAT_PATTERNS)


YESNO = {
    "yes": 1, "y": 1, "true": 1, "x": 1, "checkmark": 1,
    "no": 0, "n": 0, "false": 0, "-": 0, "": 0,
}


# --------------------------------------------------------------------------
# Brace-aware tokenisation
# --------------------------------------------------------------------------

def balanced_take(s: str, i: int) -> tuple[str, int]:
    """Take from `i` (already inside one brace) to the matching close."""
    depth, out = 1, []
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        out.append(c)
        i += 1
    return "".join(out), i


def split_rows(body: str) -> list[str]:
    """Split a tabular body on top-level `\\\\` row separators."""
    rows, buf, depth, i = [], [], 0, 0
    while i < len(body):
        if body[i:i + 2] == "\\\\" and depth == 0:
            rows.append("".join(buf))
            buf = []
            i += 2
            continue
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            buf.append(body[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        buf.append(c)
        i += 1
    tail = "".join(buf)
    if tail.strip():
        rows.append(tail)
    return rows


def split_cells(row: str) -> list[str]:
    """Split a row on top-level `&` (escaped `\\&` is not a separator)."""
    cells, buf, depth, i = [], [], 0, 0
    while i < len(row):
        c = row[i]
        if c == "\\" and i + 1 < len(row):
            buf.append(row[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "&" and depth == 0:
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    cells.append("".join(buf))
    return cells


def strip_multicolumn(cell: str) -> tuple[int, str]:
    """Return (span, inner). span == 1 when the cell is not a \\multicolumn."""
    m = re.match(r"\s*\\multicolumn\s*\{(\d+)\}\s*\{[^{}]*\}\s*\{", cell)
    if not m:
        return 1, cell
    inner, _ = balanced_take(cell, m.end())
    return int(m.group(1)), inner


TEXT_MACROS = re.compile(
    r"\\(?:textbf|textit|emph|texttt|textsf|textrm|mathrm|text|bm|mathbf|num|si)\s*\{"
)
DROP_MACROS = re.compile(
    r"\\(?:footnotesize|scriptsize|tiny|small|normalsize|large|Large|bfseries"
    r"|itshape|centering|raggedright|raggedleft|phantom|hspace|qquad|quad|,|;|:|!)"
    r"(?:\{[^{}]*\})?"
)


def clean_text(s: str) -> str:
    """Reduce a LaTeX cell to plain text (macros unwrapped, escapes undone)."""
    s = RULE_RE.sub(" ", s)
    # Unwrap font/format macros, keeping their argument.
    prev = None
    while prev != s:
        prev = s
        m = TEXT_MACROS.search(s)
        if m:
            inner, end = balanced_take(s, m.end())
            s = s[:m.start()] + inner + s[end:]
    s = DROP_MACROS.sub(" ", s)
    s = s.replace("$\\times$", " x ").replace("\\times", " x ")
    s = s.replace("\\(", "").replace("\\)", "").replace("$", "")
    s = re.sub(r"\\[,;:!]", " ", s)
    for esc, plain in (("\\_", "_"), ("\\%", "%"), ("\\&", "&"),
                       ("\\#", "#"), ("\\$", "$"), ("\\{", "{"), ("\\}", "}")):
        s = s.replace(esc, plain)
    s = s.replace("\u2212", "-").replace("\u2013", "-")
    s = re.sub(r"[{}]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_cell_value(raw: str) -> dict:
    """Parse one data cell into value / stars / delimiter.

    `delim` records whether the number was wrapped — an uncertainty row's
    signature. `text` is set when the cell is non-numeric.
    """
    stars = 0
    m = SYM_RE.search(raw)
    if m:
        stars = len(m.group(1))
        raw = SYM_RE.sub("", raw)
    s = clean_text(raw)
    bare = re.search(r"\*{1,5}", s)
    if bare and stars == 0:
        stars = len(bare.group(0))
    if bare:
        s = s.replace(bare.group(0), "")
    # C1: stargazer writes 0.342$^{***}$. clean_text drops $ and {} but not
    # the caret, so removing the stars leaves "0.342^", float() fails, the
    # cell becomes text, and an all-starred coefficient row is silently
    # reclassified as a header. Strip the superscript marker.
    s = re.sub(r"[\^_]+\s*$", "", s.strip())
    s = s.strip()
    delim = None
    for open_c, close_c in (("(", ")"), ("[", "]")):
        if s.startswith(open_c) and s.endswith(close_c) and len(s) > 1:
            delim, s = open_c, s[1:-1].strip()
            break
    # M14: only a comma separating groups of exactly three digits is a
    # thousands separator. "0,342" is a decimal comma and must NOT become 342.
    # A thousands group never begins with a zero, so "0,342" is a decimal
    # comma (342.0 would be 1000x wrong) while "41,022" is a separator.
    if re.match(r"^-?0,", s.strip()):
        s = s.replace(",", ".")
    else:
        s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    s = s.replace(" ", "")
    if not s or s in {"-", "--", "."}:
        return {"value": None, "stars": 0, "delim": delim, "text": ""}
    try:
        return {"value": float(s), "stars": stars, "delim": delim}
    except ValueError:
        return {"value": None, "stars": stars, "delim": delim, "text": clean_text(raw)}


def count_spec_columns(spec: str) -> int:
    """Count column slots declared in a tabular column specification."""
    prev = None
    while prev != spec:                       # nested: @{\extracolsep{\fill}}
        prev = spec
        spec = re.sub(r"@\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", spec)
    spec = re.sub(r"[<>]\{[^{}]*\}", "", spec)
    spec = re.sub(r"[pmb]\{[^{}]*\}", "p", spec)
    spec = re.sub(r"[SD]\[[^\]]*\]", "S", spec)
    prev = None
    while prev != spec:
        prev = spec
        spec = re.sub(
            r"\*\s*\{\s*(\d+)\s*\}\s*\{([^{}]*)\}",
            lambda m: m.group(2) * int(m.group(1)),
            spec,
        )
    return len(re.findall(r"[lcrpXSD]", spec))


# --------------------------------------------------------------------------
# Table parsing
# --------------------------------------------------------------------------

class Residue(Exception):
    """Raised when a tabular block cannot be parsed with confidence."""


UNC_PATTERNS = [
    ("se", re.compile(r"standard error|std\.? ?err|robust s\.?e\.?", re.I)),
    ("tstat", re.compile(r"\bt[-\s]?stat|\bt[-\s]?value", re.I)),
    ("zstat", re.compile(r"\bz[-\s]?stat|\bz[-\s]?value", re.I)),
    ("pval", re.compile(r"p[-\s]?value", re.I)),
    ("ci", re.compile(r"confidence interval", re.I)),
]
DELIM_WORD_RE = re.compile(r"parenthes|bracket", re.I)


def detect_uncertainty_type(notes: list[str]) -> str:
    """Infer what the value under each coefficient is.

    Visually identical, numerically incompatible — conflating an SE with a
    t-statistic silently corrupts any cross-table comparison, so this is
    parsed rather than assumed. When the note names several candidates the
    one nearest the delimiter word ("in parentheses") wins; with no
    delimiter word, priority order applies. Undeterminable -> "unknown".
    """
    text = " ".join(notes)
    if not text.strip():
        return "unknown"
    hits = [(name, m.start()) for name, pat in UNC_PATTERNS
            if (m := pat.search(text))]
    if not hits:
        return "unknown"
    if len(hits) == 1:
        return hits[0][0]
    anchor = DELIM_WORD_RE.search(text)
    if anchor:
        return min(hits, key=lambda h: abs(h[1] - anchor.start()))[0]
    order = [name for name, _ in UNC_PATTERNS]
    return min(hits, key=lambda h: order.index(h[0]))[0]


def _expand_row(cells: list[str]) -> list[str]:
    """Flatten \\multicolumn spans into one entry per physical column."""
    out = []
    for cell in cells:
        span, inner = strip_multicolumn(cell)
        out.append(inner)
        out.extend([""] * (span - 1))
    return out


def parse_tabular(body: str, ncols_spec: int) -> dict:
    """Parse one tabular body. Raises Residue when it cannot be trusted."""
    spec_labels: list[str] | None = None
    header_rows: list[list[str]] = []
    panels: list[str] = []
    notes: list[str] = []
    terms: list[str] = []
    est: list[list] = []
    unc: list[list] = []
    stars: list[list[int]] = []
    fe: dict[str, list] = {}
    stats: dict[str, list] = {}
    annotations: dict[str, list] = {}
    unclassified = 0
    stats_ambiguous = False
    unparsed_rows: list[str] = []
    coef_widths: set[int] = set()
    seen_delims: set[str] = set()
    row_panel: list[str] = []
    current_panel = ""
    pending: int | None = None

    raw_rows = split_rows(COMMENT_RE.sub("", body))
    parsed_rows = []
    for raw in raw_rows:
        # M12: rules first — \midrule[\heavyrulewidth] leaves an optional
        # argument that would otherwise glue onto the next row's label.
        row = RULE_RE.sub(" ", raw)
        row = LEADING_OPT_RE.sub("", row)
        if not row.strip():
            continue
        cells = split_cells(row)
        if len(cells) == 1:
            span, inner = strip_multicolumn(cells[0])
            text = clean_text(inner)
            if not text:
                continue
            if re.match(r"^\s*panel\b", text, re.I):
                panels.append(text)
                current_panel = text
            else:
                (notes if span > 1 else panels).append(text)
            continue
        parsed_rows.append((_expand_row(cells), current_panel))

    if OVERLAY_RE.search(body):
        # M4: a beamer overlay table can show different values on different
        # slides, so extracting one of them would be a quiet error. The docs
        # promised this refusal; the code did not implement it.
        raise Residue("beamer overlay macros in tabular — values are "
                      "slide-dependent and cannot be extracted unambiguously")
    if not parsed_rows:
        raise Residue("no data rows in tabular")

    # Column width is set by the widest row; a row wider than that is malformed.
    ncols = max(len(r) for r, _ in parsed_rows) - 1
    if ncols < 1:
        raise Residue("no data columns")

    for flat, row_panel_label in parsed_rows:
        label = clean_text(flat[0])
        values = flat[1:]
        raw_width = len(values)
        values = values + [""] * (ncols - len(values))
        cells = [parse_cell_value(v) for v in values]
        filled = [c for c in cells if c["value"] is not None or c.get("text")]
        norm = label.lower().rstrip(":").strip()

        # Header: specification numbers "(1) (2) ...". These parse as
        # parenthesised integers, not as text, so match on that shape.
        if (not terms and filled
                and all(c.get("delim") == "(" and c["value"] is not None
                        and float(c["value"]).is_integer() for c in filled)):
            spec_labels = [f"({int(c['value'])})" if c["value"] is not None else ""
                           for c in cells]
            continue

        numeric = [c for c in filled if c["value"] is not None]
        delimited = [c for c in numeric if c["delim"]]

        # Uncertainty row: unlabelled, every number wrapped in ( ) or [ ].
        if not label and numeric and len(delimited) == len(numeric):
            if pending is not None:
                # C6: two uncertainty rows under one coefficient (e.g. SE then
                # p-value). Silently keeping the last printed a p-value under a
                # column headed "standard errors". Refuse instead.
                if any(v is not None for v in unc[pending]):
                    raise Residue(
                        "two uncertainty rows under one coefficient — cannot "
                        "tell which quantity belongs in `unc`")
                unc[pending] = [c["value"] for c in cells]
                seen_delims.update(c["delim"] for c in delimited)
            continue

        if is_stat_label(norm, bool(terms)):
            # C5: a repeat means stacked panels in one tabular. Keeping the
            # last silently printed Panel B's N beside Panel A's coefficients.
            # The COEFFICIENTS remain individually correct, so refuse only the
            # summary rows rather than discarding the whole table.
            if label in stats:
                stats_ambiguous = True
            stats[label] = [c["value"] if c["value"] is not None
                            else c.get("text") or None for c in cells]
            pending = None
            continue

        # Fixed-effect / control indicator row. Requires a label AND at least
        # one coefficient row already seen: esttab emits indicator rows after
        # the coefficients, and without these guards a dependent variable
        # named "y" or "n" is misread as a Yes/No indicator.
        if label and terms and filled and not numeric and all(
                (c.get("text", "") or "").lower().strip(". ") in YESNO for c in filled):
            fe[label] = [YESNO.get((c.get("text", "") or "").lower().strip(". "), 0)
                         for c in cells]
            pending = None
            continue

        # Coefficient row: at least one bare (undelimited) number.
        if numeric and len(delimited) < len(numeric):
            if not label:
                label = f"__unlabelled_{len(terms)}"
            coef_widths.add(raw_width)
            row_panel.append(row_panel_label)
            terms.append(label)
            est.append([c["value"] for c in cells])
            stars.append([c["stars"] for c in cells])
            unc.append([None] * ncols)
            pending = len(terms) - 1
            continue

        # C1 guard: a pre-coefficient row carrying starred numeric cells is a
        # coefficient row we failed to parse, never a dependent-variable
        # header. Refuse rather than silently promote it.
        if not terms and any(c["stars"] for c in filled):
            raise Residue(
                "a header row carries significance stars — a coefficient row "
                "was not parsed (unrecognised star markup?)")

        # Pre-data text row: a header (dependent variables, group labels).
        if not terms and filled and not numeric:
            header_rows.append([c.get("text", "") or "" for c in cells])
            continue

        # Labelled all-text row after the coefficients: an annotation such as
        # "Controls | Loan | Loan | ...". Metadata, not an unparsed row.
        if label and terms and filled and not numeric:
            annotations[label] = [c.get("text", "") or "" for c in cells]
            pending = None
            continue

        if filled:
            unclassified += 1
            unparsed_rows.append(" ".join(
                (c.get("text") or ("" if c["value"] is None else str(c["value"])))
                for c in [{"text": label, "value": None}] + cells).strip()[:200])

    if not terms:
        raise Residue("no coefficient rows found")
    # Coefficient rows of unequal width make the column model ambiguous: a
    # short row could be missing a trailing cell or a middle one, and padding
    # the wrong end silently shifts values into neighbouring columns. Refuse.
    if len(coef_widths) > 1:
        msg = "inconsistent coefficient-row widths " + str(sorted(coef_widths))
        if ncols_spec and max(coef_widths) > ncols_spec - 1:
            msg += (f" — a row carries more fields than the tabular declares "
                    f"({ncols_spec - 1}); the usual cause is an unescaped '&' "
                    f"in a row label, which also breaks LaTeX compilation")
        raise Residue(msg)

    dep_vars = header_rows[-1] if header_rows else [""] * ncols
    if spec_labels is None:
        spec_labels = [f"({i + 1})" for i in range(ncols)]

    confidence = 1.0
    flags = []
    if stats_ambiguous:
        # Which panel each summary row belongs to cannot be recovered, and a
        # wrong N printed beside a coefficient is worse than none.
        stats = {}
        confidence -= 0.20
        flags.append("stacked panels: summary rows (N, R2) were ambiguous and "
                     "have been dropped; coefficients are per-panel, see `panel`")
    if ncols_spec and ncols_spec - 1 != ncols:
        confidence -= 0.15
        flags.append(f"column count {ncols} differs from tabular spec {ncols_spec - 1}")
    uncertainty_type = detect_uncertainty_type(notes)
    # The note names a delimiter; the rows show one. Disagreement means the
    # note describes a different row than the one parsed.
    if len(seen_delims) > 1:
        confidence -= 0.15
        flags.append(f"uncertainty rows mix delimiters {sorted(seen_delims)}")
    elif seen_delims and notes:
        stated = DELIM_WORD_RE.search(" ".join(notes))
        obs = next(iter(seen_delims))
        if stated:
            word = stated.group(0).lower()
            if (word.startswith("paren") and obs != "(") or \
               (word.startswith("bracket") and obs != "["):
                confidence -= 0.15
                flags.append(
                    f"notes say {word}… but the rows use {obs!r}")
    if uncertainty_type == "unknown" and any(any(u) for u in unc):
        confidence -= 0.10
        flags.append("uncertainty type not stated in table notes")
    if unclassified:
        confidence -= min(0.20, 0.05 * unclassified)
        flags.append(f"{unclassified} unclassified row(s)")
    if not header_rows:
        confidence -= 0.05
        flags.append("no dependent-variable header row")

    return {
        "spec_labels": spec_labels[:ncols],
        "dep_vars": dep_vars[:ncols],
        "terms": terms,
        "est": est,
        "unc": unc,
        "stars": stars,
        "fe": fe,
        "stats": stats,
        "panel": row_panel,
        "annotations": annotations,
        "panels": panels,
        "header_rows": header_rows,
        "notes": " ".join(notes),
        "uncertainty_type": uncertainty_type,
        "n_cols": ncols,
        "confidence": round(max(0.0, confidence), 2),
        "flags": flags,
        "unparsed_rows": unparsed_rows,
    }


TABULAR_RE = re.compile(
    r"\\begin\{(tabular\*?|tabularx|longtable|tabulary)\}"
    r"(?:\s*\{[^{}]*\})?"          # tabularx/tabular* width argument
    r"\s*\{",
    re.DOTALL,
)


def find_tabulars(text: str) -> list[tuple[int, str, int]]:
    """Locate tabular blocks. Returns (char_offset, body, ncols_spec)."""
    out = []
    for m in TABULAR_RE.finditer(text):
        spec, after = balanced_take(text, m.end())
        env = m.group(1)
        # C8: a nested tabular inside a cell used to close the outer one,
        # truncating it — the remaining rows appeared in neither the table nor
        # the residue while conservation still "passed".
        open_re = re.compile(r"\\begin\{" + re.escape(env) + r"\}")
        close_re = re.compile(r"\\end\{" + re.escape(env) + r"\}")
        depth, pos, end = 1, after, -1
        while pos < len(text):
            nxt_o = open_re.search(text, pos)
            nxt_c = close_re.search(text, pos)
            if not nxt_c:
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                pos = nxt_o.end()
                continue
            depth -= 1
            if depth == 0:
                end = nxt_c.start()
                break
            pos = nxt_c.end()
        if end == -1:
            out.append((m.start(), None, count_spec_columns(spec)))
            continue
        out.append((m.start(), text[after:end], count_spec_columns(spec)))
    return out


def parse_file(path: Path, rel: str) -> tuple[list[dict], list[dict]]:
    """Parse every tabular in one file. Returns (tables, residue, n_blocks)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    labels = re.findall(r"\\label\{([^{}]+)\}", text)
    blocks = find_tabulars(text)
    tables, residue = [], []
    seen_ids: dict[str, int] = {}
    for idx, (offset, body, ncols_spec) in enumerate(blocks):
        line = text.count("\n", 0, offset) + 1
        if len(blocks) == 1 and len(labels) == 1:
            anchor = labels[0]
        elif idx < len(labels) and len(labels) == len(blocks):
            anchor = labels[idx]
        else:
            anchor = f"t{idx}"
        table_id = f"{rel}#{anchor}"
        # C4: a duplicated \label would otherwise give two blocks the same id,
        # and the store's dict would keep only the last — two blocks in, one
        # table out, zero residue, conservation still "passing".
        if table_id in seen_ids:
            seen_ids[table_id] += 1
            table_id = f"{table_id}-{seen_ids[table_id]}"
        else:
            seen_ids[table_id] = 1
        try:
            if body is None:
                raise Residue("unterminated tabular — no matching \\end{}")
            parsed = parse_tabular(body, ncols_spec)
        except Residue as exc:
            residue.append({
                "table_id": table_id,
                "file": rel,
                "line": line,
                "reason": str(exc),
                "raw": body[:400],
            })
            continue
        parsed.update({
            "table_id": table_id,
            "src": {"file": rel, "line": line, "sha": sha},
            "source_type": "tex",
            "log": None,
            "est_full": None,
        })
        tables.append(parsed)
    return tables, residue, len(blocks)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")


def follow_inputs(path: Path, seen: set[Path]) -> list[Path]:
    """Expand \\input{}/\\include{} chains from a manuscript."""
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    out = [path]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for target in INPUT_RE.findall(text):
        child = (path.parent / target).resolve()
        if not child.suffix:
            child = child.with_suffix(".tex")
        out.extend(follow_inputs(child, seen))
    return out


def discover(paths, root: Path, exclude: str | None):
    """Resolve inputs to a de-duplicated file list.

    Nothing is excluded unless the caller passes an explicit `--exclude`
    pattern. If you point this at a directory, you get every table in it.
    """
    files: list[Path] = []
    unmatched: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.tex")))
        elif any(ch in raw for ch in "*?["):
            import glob as _glob
            files.extend(sorted(Path(x) for x in _glob.glob(raw, recursive=True)))
        elif p.is_file():
            files.extend(follow_inputs(p.resolve(), set()))
        else:
            unmatched.append(raw)
    uniq, seen = [], set()
    for f in files:
        r = f.resolve()
        if r not in seen and r.is_file():
            seen.add(r)
            uniq.append(r)
    if unmatched:
        print("extract: these arguments matched nothing: "
              + ", ".join(unmatched), file=sys.stderr)
    if not exclude:
        return uniq, []
    pat = re.compile(exclude, re.I)
    keep, dropped = [], []
    for f in uniq:
        (dropped if pat.search(relpath(f, root)) else keep).append(f)
    return keep, dropped


def observations(files, root: Path) -> list[str]:
    """Report structure the caller may care about. Reports; never acts."""
    notes = []
    versionish = [f for f in files if VERSIONISH_RE.search(relpath(f, root))]
    if versionish:
        notes.append(f"{len(versionish)} of {len(files)} file(s) sit under a "
                     f"path that reads as archived (legacy/deprecated/old/"
                     f"backup). They WERE parsed. Pass --exclude to drop them.")
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(f.name, []).append(relpath(f, root))
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if dupes:
        top = sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:3]
        notes.append(
            f"{len(dupes)} filename(s) appear in more than one directory "
            f"(e.g. {', '.join(f'{k} x{len(v)}' for k, v in top)}). These may be "
            f"versions of one table; the store keeps them separate by path.")
    return notes


def relpath(p: Path, root: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p.resolve())


# --------------------------------------------------------------------------
# Log enrichment (Stata-style estimation output)
# --------------------------------------------------------------------------

SMCL_RE = re.compile(r"\{[^{}]*\}")
CMD_RE = re.compile(
    r"^\s*\.\s+((?:xt|iv|)(?:reghdfe|regress|reg|areg|logit|probit|poisson|"
    r"ivreg2|ivregress|xtreg|didregress|csdid|drdid)\b.*)$")
# The coefficient-table header is the reliable anchor: many logs are written
# without command echo (regressions run quietly, or `set more off` output
# only), so requiring a "." line missed every block in practice.
LOG_HEADER_RE = re.compile(
    r"^\s*([A-Za-z_][\w.]*)\s*\|\s*(?:Coefficient|Coef\.)\s", re.I)
NUM = r"-?\d*\.?\d+(?:e[-+]?\d+)?"
LOG_COEF_RE = re.compile(
    rf"^\s*([A-Za-z_][\w.#]*(?:\s+[\w.#]+)*?)\s*\|\s*({NUM})\s+({NUM})")


MAX_LOG_BYTES = 50 * 1024 * 1024
LOG_PREFILTER_RE = re.compile(r"coefficient|coef\.", re.I)


def parse_log(path: Path, max_bytes: int = MAX_LOG_BYTES) -> list[dict]:
    """Extract estimation blocks from a Stata-style log.

    Returns [{cmd, dep_var, coefs: {term: (coef, se)}}]. Blocks are delimited
    by the coefficient-table header; a preceding command echo is attached when
    one is present, but is not required.

    Most logs in practice are do-file echo with no printed coefficient tables
    (regressions run quietly, with esttab writing straight to .tex), so a cheap
    substring pre-filter avoids a per-line scan over tens of megabytes.
    """
    try:
        size = path.stat().st_size
        if size > max_bytes:
            print(f"  warning: skipping {path.name} "
                  f"({size / 1048576:.0f} MB > {max_bytes // 1048576} MB cap); "
                  f"raise --max-log-mb to include it", file=sys.stderr)
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Whole-text pre-filter: most logs are do-file echo with no printed
    # coefficient tables, and skipping those avoids a per-line regex scan over
    # tens of megabytes. Scan case-insensitively in place — text.lower() would
    # materialise a second copy of a file that can be hundreds of megabytes.
    if not LOG_PREFILTER_RE.search(text):
        return []
    if path.suffix.lower() == ".smcl":
        # M7: Stata writes the column separator as {c |} and rules as
        # {hline N}. Stripping every brace group removed the separator the
        # coefficient-table parser keys on, so .smcl never parsed at all.
        text = re.sub(r"\{c ([|+\-])\}", r"\1", text)
        text = re.sub(r"\{hline( \d+)?\}", "-" * 8, text)
        text = SMCL_RE.sub("", text)

    blocks, current, last_cmd = [], None, None
    for line in text.splitlines():
        cmd = CMD_RE.match(line)
        if cmd:
            last_cmd = cmd.group(1).strip()
            continue
        head = LOG_HEADER_RE.match(line)
        if head:
            if current and current["coefs"]:
                blocks.append(current)
            current = {"cmd": last_cmd, "dep_var": head.group(1),
                       "coefs": {}}
            continue
        if current is None:
            continue
        if set(line.strip()) <= {"-", "+", " "} and current["coefs"]:
            blocks.append(current)
            current = None
            continue
        m = LOG_COEF_RE.match(line)
        if m:
            term = m.group(1).strip()
            if term.lower() not in {"coefficient", "coef.", "variable"}:
                try:
                    current["coefs"][term] = (float(m.group(2)), float(m.group(3)))
                except ValueError:
                    pass
    if current and current["coefs"]:
        blocks.append(current)
    return blocks


def _displayed_decimals(values) -> int:
    best = 0
    for v in values:
        if v is None:
            continue
        s = repr(float(v))
        if "." in s:
            best = max(best, len(s.split(".")[1].rstrip("0")))
    return min(best, 6) or 3


def fingerprint_match(table: dict, blocks: list[dict]) -> dict | None:
    """Bind a log regression to a table column by coefficient fingerprint.

    Deterministic: a log block matches when its coefficients, rounded to the
    table's displayed precision, agree with the column's on >= 2 terms and
    disagree on none.
    """
    best, matches = None, []
    for col in range(table["n_cols"]):
        col_vals = [(table["terms"][i], table["est"][i][col])
                    for i in range(len(table["terms"]))
                    if table["est"][i][col] is not None]
        # C7: value containment alone binds unrelated regressions. Require
        # enough coefficients, and enough DISTINCT ones — a column printing
        # 0.000 / 0.000 otherwise matched any log with two near-zero
        # coefficients.
        if len(col_vals) < MIN_FINGERPRINT_TERMS:
            continue
        dec = _displayed_decimals(v for _, v in col_vals)
        distinct = {round(v, dec) for _, v in col_vals}
        if len(distinct) < MIN_FINGERPRINT_TERMS:
            continue
        for block in blocks:
            rounded = {round(v[0], dec) for v in block["coefs"].values()}
            agree = sum(1 for _, v in col_vals if round(v, dec) in rounded)
            if agree == len(col_vals):
                matches.append({"col": col, "cmd": block["cmd"],
                                "agree": agree, "coefs": block["coefs"],
                                "file": block.get("file")})
    if not matches:
        return None
    # Ambiguity is not a tie to break: if two distinct logs both explain the
    # column, we cannot say which produced it.
    if len({(m["cmd"], id(m["coefs"])) for m in matches}) > 1:
        return None
    return max(matches, key=lambda m: m["agree"])


def enrich_with_logs(tables: list[dict], log_files: list[Path], root: Path,
                     max_bytes: int = MAX_LOG_BYTES) -> int:
    """Attach log provenance and full precision where a match is found."""
    blocks = []
    for lf in log_files:
        for b in parse_log(lf, max_bytes):
            b["file"] = relpath(lf, root)
            blocks.append(b)
    if not blocks:
        return 0
    matched = 0
    for t in tables:
        hit = fingerprint_match(t, blocks)
        if not hit:
            continue
        t["log"] = {"file": hit.get("file"), "cmd": hit["cmd"],
                    "matched_column": hit["col"]}
        full = [[None] * t["n_cols"] for _ in t["terms"]]
        dec = _displayed_decimals(t["est"][i][hit["col"]] for i in range(len(t["terms"])))
        for i, term in enumerate(t["terms"]):
            shown = t["est"][i][hit["col"]]
            if shown is None:
                continue
            for _, (coef, _se) in hit["coefs"].items():
                if round(coef, dec) == round(shown, dec):
                    full[i][hit["col"]] = coef
                    break
        t["est_full"] = full
        matched += 1
    return matched


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def store_file(root: Path) -> Path:
    return root / ".claude" / "state" / "table-extracts" / "store.json"


def load_store(root: Path) -> dict:
    f = store_file(root)
    if f.exists():
        try:
            data = json.loads(f.read_text())
            if data.get("schema") == SCHEMA_VERSION:
                return data
        except json.JSONDecodeError:
            print(f"warning: {f} is corrupt and was ignored; re-extract to "
                  f"rebuild it", file=sys.stderr)
        except OSError:
            pass
    return {"schema": SCHEMA_VERSION, "tables": [], "residue": [], "files": {}}


def save_store(root: Path, store: dict) -> Path:
    f = store_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    # Compact: the store is machine-read backing data. Humans read the
    # projection, and pretty-printing numeric arrays costs ~60% in size.
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, separators=(",", ":"),
                              sort_keys=True, ensure_ascii=False))
    os.replace(tmp, f)          # atomic: a crash mid-write cannot truncate
    return f


def upsert(store: dict, tables: list[dict], residue: list[dict],
           files_done: dict) -> list[str]:
    """Replace tables by id (never append) and report changed values."""
    # C3: a table deleted from its source file must not survive in the store.
    # Overwriting by id alone left stale tables projected as current results,
    # and — because anchors are positional — could duplicate a survivor.
    reparsed = set(files_done)
    prior = {t["table_id"]: t for t in store["tables"]
             if t["src"]["file"] in reparsed}
    old = {t["table_id"]: t for t in store["tables"]
           if t["src"]["file"] not in reparsed}
    diffs = []
    for t in tables:
        prev = prior.get(t["table_id"])
        if prev:
            for i, term in enumerate(t["terms"]):
                if term not in prev["terms"]:
                    continue
                j = prev["terms"].index(term)
                for col in range(min(t["n_cols"], prev["n_cols"])):
                    a, b = prev["est"][j][col], t["est"][i][col]
                    if a is not None and b is not None and abs(a - b) > 1e-12:
                        diffs.append(
                            f"{t['table_id']} col {col + 1} {term}: {a} -> {b}")
        old[t["table_id"]] = t
    store["tables"] = sorted(old.values(), key=lambda x: x["table_id"])
    touched = {t["table_id"] for t in tables} | {r["table_id"] for r in residue}
    kept = [r for r in store["residue"] if r["table_id"] not in touched]
    store["residue"] = sorted(kept + residue, key=lambda x: x["table_id"])
    store["files"].update(files_done)
    return diffs


# --------------------------------------------------------------------------
# Projection — the only thing that should enter a model's context
# --------------------------------------------------------------------------

def _fmt(v, dec=4):
    """Trim trailing zeros only after a decimal point.

    Stripping unconditionally corrupts integers: N=9450 became "945".
    """
    if v is None:
        return ""
    if not isinstance(v, (int, float)):
        return str(v)          # M3: stats may hold text ("n.a.", "---")
    text = f"{v:.{dec}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def distribution(rows: list[dict]) -> str:
    """Inventory of what the tables contain — NOT a meta-analysis.

    Deliberately limited to facts printed in the source: how many estimates
    exist, how many carry a minus sign, how many carry a star, and the extremes.

    It reports **no central tendency**. Estimates across a fixed-effect ladder,
    different samples, and different outcomes are not exchangeable, so a mean or
    median over them is not a statistic — it is a number shaped like one. Being
    deterministic makes that more dangerous, not less: an arithmetic result
    carries an authority nobody audits.

    Grouping by dependent variable is mandatory, not optional. Pooling
    incommensurable outcomes is the same class of error as pooling standard
    errors with t-statistics.
    """
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["term"], r["dep_var"]), []).append(r)
    out = ["### Coefficient inventory\n",
           "Counts of what the source tables print. **Not** a meta-analysis: "
           "no central tendency is reported, because estimates across "
           "specifications, samples and outcomes are not exchangeable. "
           "Interpretation is yours.\n",
           "| term | outcome | estimates | tables | negative | positive | zero | starred | min | max | unc |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for (term, dep), g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        vals = sorted(r["est"] for r in g)
        neg = sum(1 for v in vals if v < 0)
        pos = sum(1 for v in vals if v > 0)
        zero = sum(1 for v in vals if v == 0)
        starred = sum(1 for r in g if r["sig"])
        utypes = "/".join(sorted({r["utype"] for r in g}))
        out.append(
            f"| {term[:40]} | {(dep or '(unlabelled)')[:26]} | {len(g)} | "
            f"{len({r['table'] for r in g})} | {neg} | {pos} | {zero} | "
            f"{starred} | {_fmt(vals[0])} | {_fmt(vals[-1])} | {utypes} |")
    out.append("\n`starred` counts estimates printed with at least one "
               "significance star; the threshold each star denotes is set by "
               "the source table, not by this tool.")
    return "\n".join(out)


def project(store: dict, terms, dep_var=None, table_filter=None,
            as_csv=False, show_all=False, as_summary=False) -> str:
    tables = store["tables"]
    if table_filter:
        tables = [t for t in tables if any(f in t["table_id"] for f in table_filter)]
    rows = []
    for t in tables:
        for i, term in enumerate(t["terms"]):
            if terms and not any(q.lower() in term.lower() for q in terms):
                continue
            for col in range(t["n_cols"]):
                if t["est"][i][col] is None:
                    continue
                dv = t["dep_vars"][col] if col < len(t["dep_vars"]) else ""
                if dep_var and dep_var.lower() not in dv.lower():
                    continue
                nobs = ""
                for key, vals in t["stats"].items():
                    if key.lower().rstrip(":").strip() in {"observations", "n", "obs"}:
                        nobs = _fmt(vals[col], 0) if col < len(vals) else ""
                        break
                rows.append({
                    "term": term,
                    "table": t["table_id"],
                    "spec": t["spec_labels"][col] if col < len(t["spec_labels"]) else "",
                    "dep_var": dv,
                    "est": t["est"][i][col],
                    "unc": t["unc"][i][col],
                    "sig": "*" * t["stars"][i][col],
                    "n": nobs,
                    "utype": t["uncertainty_type"],
                    "stype": t["source_type"],
                    "conf": t["confidence"],
                    "full": (t["est_full"][i][col]
                             if t.get("est_full") and t["est_full"][i][col] is not None
                             else None),
                    "cmd": (t.get("log") or {}).get("cmd") or "",
                })
    if not rows:
        return "No coefficients matched. Try broader --terms, or run `extract` first."

    if as_summary:
        return distribution(rows)

    if as_csv:
        cols = ["term", "table", "spec", "dep_var", "est", "unc", "sig",
                "n", "utype", "stype", "conf"]
        out = [",".join(cols)]
        for r in rows:
            out.append(",".join(
                '"' + str(r[c]).replace('"', '""') + '"' if isinstance(r[c], str)
                else str("" if r[c] is None else r[c]) for c in cols))
        return "\n".join(out)

    # Never merge incompatible uncertainty types into one comparison.
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["utype"], []).append(r)

    out = []
    if len(by_type) > 1:
        out.append("> **Split by uncertainty type.** These groups report different "
                   "quantities under each coefficient and are NOT comparable "
                   "as-is.\n")
    for utype, group in sorted(by_type.items()):
        label = {"se": "standard errors", "tstat": "t-statistics",
                 "zstat": "z-statistics",
                 "pval": "p-values", "ci": "confidence intervals",
                 "unknown": "UNSTATED — treat with caution"}[utype]
        out.append(f"### Uncertainty reported: {label}\n")
        low = [r for r in group if r["conf"] < 1.0 or r["stype"] != "tex"]
        has_log = show_all and any(r["full"] is not None or r["cmd"] for r in group)
        out.append("| term | table | spec | dep var | est | unc | sig | N |"
                   + (" src | conf |" if low or show_all else "")
                   + (" full precision | command |" if has_log else ""))
        out.append("|---|---|---|---|---:|---:|---|---:|"
                   + ("---|---:|" if low or show_all else "")
                   + ("---:|---|" if has_log else ""))
        for r in sorted(group, key=lambda x: (x["term"], x["table"], x["spec"])):
            short = r["table"].rsplit("/", 1)[-1]
            line = (f"| {r['term']} | {short} | {r['spec']} | {r['dep_var']} "
                    f"| {_fmt(r['est'])} | {_fmt(r['unc'])} | {r['sig']} | {r['n']} |")
            if low or show_all:
                line += f" {r['stype']} | {r['conf']} |"
            if show_all and (r["full"] is not None or r["cmd"]):
                line += f" {r['full'] if r['full'] is not None else ''} | {r['cmd'][:60]} |"
            out.append(line)
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Verify — independent source round-trip (Tier 3)
# --------------------------------------------------------------------------

# Must cover scientific notation: a cell of "-5.0e+07" is not "-5.0".
NAIVE_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def verify_cell(root: Path, table: dict, term_i: int, col: int):
    """Re-read the source with naive splitting, independent of classification.

    Deterministic parsing cannot invent a number, but it CAN misalign one —
    the right value in the wrong column. This check bypasses row
    classification and coefficient/uncertainty pairing entirely: find the raw
    line whose first field is the term, split it on bare `&`, and confirm the
    value sits in the expected field.
    """
    path = root / table["src"]["file"]
    if not path.is_file():
        return None, "source missing"
    term = table["terms"][term_i]
    expected = table["est"][term_i][col]
    # A label can repeat within one table (stepwise / horserace layouts put
    # the same regressor on several rows). Match the SAME occurrence the
    # store recorded, otherwise this compares unrelated rows.
    occurrence = table["terms"][:term_i].count(term)
    seen = 0
    # Scope to THIS table's span. A file holding many tabulars repeats term
    # labels across them, so a whole-file scan compares unrelated tables --
    # the same defect this check exists to catch.
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(table["src"].get("line", 1) - 1, 0)
    end = len(all_lines)
    for m in re.finditer(r"\\end\{tabular\*?\}|\\end\{longtable\}", 
                         "\n".join(all_lines[start:])):
        end = start + "\n".join(all_lines[start:])[:m.end()].count("\n") + 1
        break
    for raw in all_lines[start:end]:
        if "\\multicolumn" in raw:
            continue
        if "&" not in raw:
            continue
        parts = raw.split("&")
        if clean_text(parts[0]).strip() != term:
            continue
        if seen != occurrence:
            seen += 1
            continue
        if col + 1 >= len(parts):
            continue
        m = NAIVE_NUM_RE.search(parts[col + 1].replace("\\", " "))
        if not m:
            continue
        got = float(m.group(0).replace(",", ""))
        # M1: comparing magnitudes made the only independent check blind to
        # sign errors and to symmetric column swaps (+0.5 vs -0.5).
        if abs(got - expected) < 10 ** -6:
            return True, f"{term} col{col + 1} = {got}"
        return False, f"{term} col{col + 1}: store={expected} source={got}"
    return None, f"{term} not found by naive scan"


def run_verify(root: Path, store: dict, sample: int) -> int:
    pool = []
    for t in store["tables"]:
        if t["source_type"] != "tex":
            continue
        for i in range(len(t["terms"])):
            for c in range(t["n_cols"]):
                if t["est"][i][c] is not None:
                    pool.append((t, i, c))
    if not pool:
        print("verify: store is empty — run `extract` first.")
        return 1
    random.seed(0)
    picked = random.sample(pool, min(sample, len(pool)))
    ok = bad = skip = 0
    failures = []
    for t, i, c in picked:
        res, msg = verify_cell(root, t, i, c)
        if res is True:
            ok += 1
        elif res is False:
            bad += 1
            failures.append(f"  MISALIGNED {t['table_id']}: {msg}")
        else:
            skip += 1
    print(f"verify: {ok} confirmed, {bad} misaligned, {skip} unverifiable "
          f"(of {len(picked)} sampled from {len(pool)} cells)")
    for f in failures:
        print(f)
    if bad:
        print("\nBLOCKING: a misaligned cell means the column model is wrong; "
              "every downstream comparison is suspect.")
        return 1
    # M2: a green exit that confirmed nothing is indistinguishable from a green
    # exit that confirmed everything.
    if ok == 0:
        print("\nBLOCKING: nothing could be verified — sources unreadable or "
              "unmatched. This is not a pass.")
        return 1
    if skip > len(picked) // 4:
        print(f"\nWARNING: {skip}/{len(picked)} sampled cells were "
              f"unverifiable; treat this run as weak evidence.")
    return 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def run_extract(args) -> int:
    root = Path(args.project_root).resolve()
    store = ({"schema": SCHEMA_VERSION, "tables": [], "residue": [], "files": {}}
             if args.fresh else load_store(root))
    files, dropped = discover(args.paths, root, args.exclude)
    if not files:
        print("extract: no .tex files matched.")
        return 1

    tables_all, residue_all, files_done = [], [], {}
    cached = parsed_files = blocks_seen = 0
    for f in files:
        rel = relpath(f, root)
        try:
            sha = file_sha(f)
        except OSError as exc:
            residue_all.append({"table_id": rel, "file": rel, "line": 0,
                                "reason": f"unreadable: {exc}", "raw": ""})
            continue
        if not args.force and store["files"].get(rel) == sha:
            cached += 1
            continue
        tables, residue, n_blocks = parse_file(f, rel)
        if n_blocks == 0:
            files_done[rel] = sha
            continue
        # Conservation: every block found must be accounted for.
        if n_blocks != len(tables) + len(residue):
            print(f"FATAL: conservation violated in {rel}: {n_blocks} blocks, "
                  f"{len(tables)} parsed + {len(residue)} residue", file=sys.stderr)
            return 2
        for t in tables:
            t["src"]["sha"] = sha
        blocks_seen += n_blocks
        parsed_files += 1
        tables_all.extend(tables)
        residue_all.extend(residue)
        files_done[rel] = sha

    matched_logs = 0
    if args.logs:
        log_files = []
        for d in args.logs:
            dp = Path(d)
            if dp.is_dir():
                for ext in ("*.log", "*.smcl"):
                    log_files.extend(sorted(dp.rglob(ext)))
            elif dp.is_file():
                log_files.append(dp)
        matched_logs = enrich_with_logs(tables_all, log_files, root,
                                        args.max_log_mb * 1048576)

    diffs = upsert(store, tables_all, residue_all, files_done)
    path = save_store(root, store)

    total = len(store["tables"])
    print(f"extract: {parsed_files} file(s) parsed, {cached} cached"
          + (f", {len(dropped)} excluded by --exclude" if dropped else ""))
    print(f"  tables: {len(tables_all)} this run / {total} in store")
    print(f"  residue: {len(residue_all)} this run / {len(store['residue'])} in store")
    if blocks_seen:
        rate = 100.0 * len(residue_all) / blocks_seen
        print(f"  residue rate: {rate:.1f}% of {blocks_seen} blocks seen")
    if args.logs:
        print(f"  log-matched tables: {matched_logs}")
    for note in observations(files, root):
        print(f"  note: {note}")
    low = [t for t in tables_all if t["confidence"] < 0.9]
    if low:
        print(f"  low confidence (<0.9): {len(low)} table(s)")
        for t in low[:5]:
            print(f"    {t['table_id']}: {'; '.join(t['flags'])}")
    if diffs:
        print(f"  CHANGED VALUES ({len(diffs)}) — a source table moved:")
        for d in diffs[:20]:
            print(f"    {d}")
    print(f"  store: {relpath(path, root)}")
    return 0


def run_stats(args) -> int:
    root = Path(args.project_root).resolve()
    store = load_store(root)
    tables = store["tables"]
    if not tables:
        print("stats: store is empty — run `extract` first.")
        return 1
    ncoef = sum(sum(1 for row in t["est"] for v in row if v is not None)
                for t in tables)
    utypes: dict[str, int] = {}
    for t in tables:
        utypes[t["uncertainty_type"]] = utypes.get(t["uncertainty_type"], 0) + 1
    print(f"tables:       {len(tables)}")
    print(f"coefficients: {ncoef}")
    print(f"residue:      {len(store['residue'])}")
    print(f"files:        {len(store['files'])}")
    print(f"log-matched:  {sum(1 for t in tables if t.get('log'))}")
    print("uncertainty:  " + ", ".join(f"{k}={v}" for k, v in sorted(utypes.items())))
    store_bytes = store_file(root).stat().st_size if store_file(root).exists() else 0
    print(f"store size:   {store_bytes / 1024:.0f} KB (~{store_bytes // 4} tok)")
    return 0


PROJECTION_BUDGET = 120_000  # bytes, ~30k tokens


def term_index(store: dict) -> list[tuple[str, int, int]]:
    """Distinct terms with (occurrences, tables) counts."""
    counts: dict[str, list] = {}
    for t in store["tables"]:
        for i, term in enumerate(t["terms"]):
            hit = counts.setdefault(term, [0, set()])
            hit[0] += sum(1 for v in t["est"][i] if v is not None)
            hit[1].add(t["table_id"])
    return sorted(((k, v[0], len(v[1])) for k, v in counts.items()),
                  key=lambda x: -x[1])


def run_terms(args) -> int:
    root = Path(args.project_root).resolve()
    store = load_store(root)
    idx = term_index(store)
    if not idx:
        print("terms: store is empty — run `extract` first.")
        return 1
    print(f"{len(idx)} distinct terms across {len(store['tables'])} tables\n")
    print(f"{'coefs':>6}  {'tables':>6}  term")
    for term, n, ntab in idx[:args.top]:
        print(f"{n:6d}  {ntab:6d}  {term[:90]}")
    if len(idx) > args.top:
        print(f"\n... {len(idx) - args.top} more (raise --top to see them)")
    return 0


def run_project(args) -> int:
    root = Path(args.project_root).resolve()
    store = load_store(root)
    terms = [t.strip() for t in args.terms.split(",")] if args.terms else []
    tabs = [t.strip() for t in args.tables.split(",")] if args.tables else []
    out = project(store, terms, args.dep_var, tabs, args.csv, args.all,
                  args.summarize)

    # A projection is only worth reading when it is SELECTIVE. An unfiltered
    # dump can exceed the source it was meant to compress, which defeats the
    # entire purpose of the tool, so refuse rather than flood the context.
    if len(out) > PROJECTION_BUDGET and not args.force_full:
        idx = term_index(store)
        print(f"Projection would be ~{len(out) // 4:,} tokens "
              f"(budget ~{PROJECTION_BUDGET // 4:,}). Refusing: at this size it "
              f"no longer compresses anything.\n")
        print("Narrow it with --terms / --dep-var / --tables, or use "
              "--distribution for a per-outcome count inventory.\n")
        print("Most common terms:\n")
        print(f"{'coefs':>6}  {'tables':>6}  term")
        for term, n, ntab in idx[:15]:
            print(f"{n:6d}  {ntab:6d}  {term[:90]}")
        print("\nOr pass --force-full to emit it anyway.")
        return 1
    print(out)
    return 0


# --------------------------------------------------------------------------
# Self-test (golden fixtures)
# --------------------------------------------------------------------------

def run_self_test() -> int:
    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    cases = sorted(fixtures.glob("*.tex"))
    if not cases:
        print(f"self-test: no fixtures in {fixtures}", file=sys.stderr)
        return 2
    failures = 0
    for tex in cases:
        exp_file = tex.with_suffix(".expected.json")
        if not exp_file.exists():
            print(f"  SKIP {tex.name} (no expectation file)")
            continue
        expected = json.loads(exp_file.read_text())
        tables, residue, n_blocks = parse_file(tex, tex.name)
        if n_blocks != len(tables) + len(residue):
            print(f"  FAIL {tex.name}: conservation violated")
            failures += 1
            continue
        if "residue" in expected:
            if residue and expected["residue"] in residue[0]["reason"]:
                print(f"  ok   {tex.name} (residue as expected)")
            else:
                got = residue[0]["reason"] if residue else "parsed instead"
                print(f"  FAIL {tex.name}: expected residue "
                      f"'{expected['residue']}', got '{got}'")
                failures += 1
            continue
        if not tables:
            print(f"  FAIL {tex.name}: no table parsed")
            failures += 1
            continue
        got = tables[0]
        bad = []
        for key, want in expected.items():
            have = got.get(key)
            if have != want:
                bad.append(f"      {key}:\n        want {want}\n        got  {have}")
        if bad:
            print(f"  FAIL {tex.name}")
            print("\n".join(bad))
            failures += 1
        else:
            print(f"  ok   {tex.name}")
    print(f"\nself-test: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="parse_tables",
        description="Deterministic LaTeX regression-table extractor.")
    ap.add_argument("--self-test", action="store_true",
                    help="run golden fixtures and exit")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--project-root", default=".",
                       help="project root holding .claude/state/ (default: cwd)")

    e = sub.add_parser("extract", help="parse tables into the store")
    e.add_argument("paths", nargs="+", help="directory, glob, or manuscript")
    e.add_argument("--force", action="store_true", help="re-parse despite cache hits")
    e.add_argument("--fresh", action="store_true", help="discard the store first")
    e.add_argument("--exclude", default=None,
                   help="regex of paths to skip (opt-in; nothing is skipped "
                        "by default)")
    e.add_argument("--logs", nargs="*", default=None,
                   help="log files or directories for full-precision enrichment")
    e.add_argument("--max-log-mb", type=int, default=MAX_LOG_BYTES // 1048576,
                   help="skip logs larger than this (default 50)")
    common(e)

    p = sub.add_parser("project", help="emit a small pivot for reading")
    p.add_argument("--terms", default="", help="comma-separated term filters")
    p.add_argument("--dep-var", default=None, help="dependent-variable filter")
    p.add_argument("--tables", default="", help="comma-separated table-id filters")
    p.add_argument("--csv", action="store_true", help="tidy long CSV instead")
    p.add_argument("--all", action="store_true", help="always show source/confidence")
    p.add_argument("--distribution", dest="summarize", action="store_true",
                   help="inventory of counts per term and outcome (no averages)")
    p.add_argument("--force-full", action="store_true",
                   help="emit an over-budget projection anyway")
    common(p)

    tm = sub.add_parser("terms", help="list distinct terms to filter on")
    tm.add_argument("--top", type=int, default=40)
    common(tm)

    v = sub.add_parser("verify", help="independent source round-trip check")
    v.add_argument("--sample", type=int, default=50)
    common(v)

    s = sub.add_parser("stats", help="store summary")
    common(s)

    args = ap.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if args.cmd == "extract":
        return run_extract(args)
    if args.cmd == "project":
        return run_project(args)
    if args.cmd == "stats":
        return run_stats(args)
    if args.cmd == "terms":
        return run_terms(args)
    if args.cmd == "verify":
        root = Path(args.project_root).resolve()
        return run_verify(root, load_store(root), args.sample)
    ap.print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
