# table-extract

Deterministic extraction of regression tables from LaTeX source (and paired
Stata logs) into a compact coefficient store, so a high-tier model reads a
small filtered projection instead of hundreds of kilobytes of table source.

One canonical copy, loaded in **every** project, so a fix lands everywhere
instead of being copy-pasted into the next repo and then drifting.

## Why

Past a few dozen tables, reading the source stops being merely expensive and
starts crowding out the context needed to reason across them. Parsing is
deterministic, so it does not belong in a model's context at all.

The savings come from three places, and the design serves all three:

1. **Compact representation** — columnar arrays rather than per-cell objects.
2. **Selective projection** — the store holds everything; only a filtered
   slice is ever read. This is the large multiplier, and the reason the script
   refuses to emit an over-budget projection.
3. **Content-hash caching** — the mechanical pass happens once, not once per
   session.

## Core principle: fail loudly, never quietly

This feeds numbers to a reader that will not see the source. A silently
mis-parsed cell is far worse than an unparsed table, so every tabular block
either yields a verified table or lands in `residue[]` with a reason and its
raw span. The counts must balance, and extraction aborts if they do not.

## Commands

| Command | Does |
|---|---|
| `extract PATH...` | Parse a directory, glob, or manuscript into the store |
| `terms` | List distinct terms with counts — run before projecting |
| `project` | Emit a compact, filtered pivot for reading |
| `verify` | Independent source round-trip check for misalignment |
| `stats` | Store summary |
| `--self-test` | Golden fixtures |

### Flags

| Flag | Applies to | Effect |
|---|---|---|
| `--project-root` | all | Project holding `.claude/state/` (default: cwd) |
| `--force` | extract | Re-parse despite content-hash cache hits |
| `--fresh` | extract | Discard the store and rebuild |
| `--logs` | extract | Log files or directories for full-precision enrichment |
| `--max-log-mb` | extract | Skip logs larger than this (default 50) |
| `--exclude` | extract | Regex of paths to skip (opt-in; nothing is skipped by default) |
| `--terms` | project | Comma-separated term filters |
| `--dep-var` | project | Dependent-variable filter |
| `--tables` | project | Comma-separated table-id filters |
| `--distribution` | project | Per-term, per-outcome count inventory (no averages) |
| `--csv` | project | Tidy long CSV instead of markdown |
| `--all` | project | Always show source and confidence columns |
| `--force-full` | project | Emit an over-budget projection anyway |
| `--sample` | verify | Number of cells to round-trip (default 50) |
| `--top` | terms | How many terms to list (default 40) |

## What it parses

`estout`/`esttab`-family LaTeX output is the well-tested primary path. Star
markup and uncertainty delimiters are **sniffed per file**, not enumerated —
an unrecognized combination produces residue rather than a guess.

Log enrichment anchors on the coefficient-table header, so logs written
without command echo still parse. A log regression is bound to a table column
by **coefficient fingerprint** — the log's coefficients rounded to the table's
displayed precision must agree on at least two terms and disagree on none.
Deterministic; no model involved. This recovers full precision, the literal
estimation command, and the true N.

## It reports structure; it does not act on it

Point it at a directory and every table in it is parsed. Where the layout is
suggestive — files under `legacy`/`deprecated`/`old` paths, or one filename
appearing in several directories — that is surfaced as a note.

Excluding them by default was the original design and it was wrong twice over.
Deciding a directory is superseded is a relevance judgement, which this tool
does not make; and an excluded file never entered the parsed/residue
accounting, so the filter was a hole in the conservation invariant that is
supposed to prevent exactly that kind of silent loss.

Reporting also generalises where the heuristic could not: duplicate-filename
detection catches date-versioned directories (`20260811/`, `20260814/`, ...),
which no `legacy|old` pattern would ever match.

## Uncertainty type is load-bearing

The value under a coefficient may be a standard error, a t-statistic, a
p-value, or a CI bound. They occupy the same visual slot and are numerically
incompatible. It is parsed from the table notes; when the notes do not say
(common in `\input{}`-ed fragments, where the note lives in the manuscript
wrapper), the type is recorded as unknown rather than assumed, and projections
spanning more than one type are split with a warning.

## Install

From the repository root:

```bash
./scripts/link-plugin.sh
```

Confirm with `/plugin`. Uninstall:

```bash
rm ~/.claude/skills/table-extract
```

## Testing

```bash
python3 plugins/table-extract/scripts/parse_tables.py --self-test
python3 plugins/table-extract/tests/test_parse_tables.py
```

Golden fixtures cover both star/delimiter dialects, SE vs t-statistic notes,
`\multicolumn` spans, sparse columns, summary-statistic rows, text annotation
rows, and a layout that must be refused. The invariant suite covers
conservation, idempotency, cache correctness, refusal to guess, refusal to
merge incompatible uncertainty types, stale filtering, projection budgeting,
and round-trip misalignment detection (including an injected-fault test that
must fail).

## What it saves, honestly

These figures were re-measured by an independent audit after the first set was
found to be systematically flattering. The originals are corrected below rather
than deleted, because *how* they were wrong is the more useful lesson.

Baseline throughout = the fair one: glob, grep to find matching files, then
read only those, with the Read tool's line numbering counted (a real ~9.5%
cost that measuring bytes on disk misses).

| Question | Skill | Read files | Ratio |
|---|---:|---:|---:|
| Every estimate of one term, 91 of 149 tables | 31,582 | 148,422 | **4.7x** |
| Corpus-wide count inventory of one term | 1,615 | 148,422 | **92x** |
| Per-scheme rates across 43 schemes | 153,567 | 1,294,384 | **7.8x** |
| Judgment question needing the estimates | 30,131 | 55,400 | **1.8x** |
| One specific table | 2,091 | 1,234 | **0.4x — a loss** |

**There is no single ratio, and the honest number is around 4.7x** for a
selective query over a large corpus. Treat anything larger as a claim about the
*question*, not about the tool.

### What the audit corrected

| Was claimed | Actually | Why |
|---|---|---|
| 138x | 92x | baselines inflated ~5-10% by a padding artifact in the Read emulation |
| 5.1x | 4.7x | same |
| 212x | 7.8x | the 212x measured a *pooled* inventory against a question that asked for per-scheme rates, which this tool cannot group |
| 15.2x | 1.8x | a judgment question was answered from counts, contradicting this skill's own "rows are the safe default" |
| 84x | n/a | that artifact was produced by hand-written code, not by this tool |
| overhead ~1,030 | ~2,600-3,000 | invoking the skill loads SKILL.md, 1,617 tokens, never counted |
| line numbering 26% | 9.5% | the emulation padded line numbers; the real Read tool does not |

The pattern worth remembering: **every inflated ratio came from substituting a
cheaper question for the one asked.** The two figures that survived scrutiny
are the two where the output actually answered the stated question.

### Where it is a loss

- Below roughly **3 tables** for a selective single-term query.
- Below roughly **15 tables**, or never, for "what is in this table" — the
  whole table has to be projected, and that is bigger than the source.
- For an all-terms question at any size: an unfiltered projection exceeds the
  source it was meant to compress, and the tool refuses rather than emit it.

A baseline larger than a context window (the 43-scheme case, 1.29M tokens) is
not a saving so much as a difference in feasibility — nothing could have paid
that cost, so the comparison is not to a real alternative.

## Known limits

- **Stacked panels** (two panels in one `tabular`) keep their coefficients and
  record a `panel` per row, but their summary rows (N, R2) are ambiguous and
  are dropped with a flag rather than guessed. A wrong N beside a coefficient
  is worse than none.
- **Beamer overlay tables** are refused: values are slide-dependent.
- **Rows that match no branch** are recorded in `unparsed_rows` and counted, so
  row-level loss is visible rather than only reflected in a confidence penalty.
- **Decimal commas** are distinguished from thousands separators by the leading
  group; `0,342` is 0.342, not 342.
- **No grouping by file, scheme, or version.** `project` filters by term,
  dependent variable and table id, but cannot break results out per source
  file. A per-scheme or per-vintage comparison needs one call per group, or
  code written against the store — which is where several past errors came
  from.

## What this is not

- **Not a verifier.** It extracts; it does not adjudicate whether a number is
  right. Checking a manuscript against outputs is a separate job with its own
  disposition model.
- **Not a PDF reader.** LaTeX source and logs only.
- **Not a slide parser.** Presentation tables using overlay macros are
  refused deliberately: an overlay table can show different values on
  different slides, so extracting one would be a quiet error.
- **Not a meta-analysis.** The inventory counts what the source prints and
  reports no central tendency, because estimates across specifications,
  samples and outcomes are not exchangeable. Grouping by outcome is mandatory
  for the same reason SEs and t-statistics are never merged.
- **Not a harmoniser.** It will not decide that two differently-labelled
  variables are the same thing. That judgment stays with the reader, who has
  every table in view at once.
- **Not persistent.** The store is derived, gitignored, temporary data. Delete
  it freely; it rebuilds from source.
