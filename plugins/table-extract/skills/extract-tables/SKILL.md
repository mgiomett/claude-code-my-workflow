---
name: extract-tables
description: Extract regression tables from LaTeX source (and paired Stata logs) into a compact coefficient store, then read a small filtered projection instead of the raw tables. Use when synthesizing results across many tables — "compare the coefficient on X across all my tables", "pull the estimates out of these tex files", "what does treat do across specifications", "summarize my results tables" — or whenever a task would otherwise mean opening dozens of table files. Deterministic parser, no model in the extraction path.
argument-hint: "[directory, glob, or manuscript] [focal term]"
allowed-tools: ["Bash", "Read", "Glob"]
---

# Extract Tables

Reading regression tables directly is expensive and, past a few dozen tables,
crowds out the context needed to reason across them. This skill parses them
once with a deterministic script and hands you a small, filtered view.

**The discipline this skill exists to enforce: extract, then project. Do not
read the source tables, and do not read the store.**

Expect roughly **4.7x** fewer tokens on a selective query over a large corpus,
or **~10x** with the compact row format (a legend for tables and outcomes
instead of repeating them on every row). Compact omits the source and
confidence columns, so use the default view when confidence matters.
Much larger ratios are available only by answering a narrower question than the
one asked — an independent audit found every inflated figure in this project's
history came from exactly that substitution. Reading either defeats
the purpose — the store is machine-backing data, often larger than the source
it came from.

## When to use

- Comparing one coefficient across many specifications, outcomes, or samples.
- Any question that would otherwise mean opening more than a handful of tables.
- Recovering full precision or the literal estimation command from analysis logs.

## When NOT to use

- One or two tables — just read them. The fixed overhead is ~3,160 tokens per
  session (this file is ~1,826 of it), so below roughly six tables this costs
  more than it saves. For "what is in this table" questions the crossover
  is nearer fifteen tables, because the whole table has to be projected.
- Anything the projection does not carry: table notes, footnote text, exact
  formatting, column ordering. Open the file for those.
- Verifying numbers against a manuscript. That is a different job with a
  different disposition model; see the reproducibility audit skill.
- Presentation decks whose tables use overlay macros. Those are refused on
  purpose: an overlay table can show different values on different slides, so
  extracting one silently would be a quiet error.

## Workflow

### 1. Extract

Point it at a directory, a glob, or a manuscript (input chains are followed).

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" extract path/to/tables
```

**Nothing is excluded.** Point it at a directory and you get every table in
it. Where the layout suggests something — files under archive-sounding paths,
or one filename appearing in several directories — that is *reported* so you
can decide. Deciding which results are superseded is your call, not the
parser's, and a silently dropped file would bypass the conservation invariant
entirely. Skip paths explicitly with the exclude option when you want to.

Add analysis logs to recover full precision and the estimation command:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" extract path/to/tables --logs path/to/logs
```

Re-running is cheap and safe: unchanged files are skipped by content hash, and
a re-parse replaces a table's rows rather than appending. If a value changed
since the last run, it is reported as a diff — that is the signal that a source
table moved underneath you.

### 2. Discover what to filter on

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" terms
```

Lists distinct terms with occurrence counts. Do this before projecting.

### 2b. Check the dialect on a corpus you have not parsed before

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" dialects path/to/tables
```

Names the star and delimiter conventions present, and flags any not covered by
a golden fixture. Unsupported markup fails silently by nature — this is what
makes it visible first.

### 3. Project

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" project --terms treat --dep-var wage
```

Emits a compact pivot: term, table, specification, dependent variable,
estimate, uncertainty, significance, N.

**For a corpus-wide picture, ask for the inventory instead.** It counts, per
term *and per outcome*, how many estimates exist, how many are negative,
positive, or zero, how many are printed with a star, and the extremes:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" project --terms treat --distribution
```

**The inventory reports what the tables print, never what they mean.** It
deliberately gives no mean or median: estimates across a fixed-effect ladder,
different samples and different outcomes are not exchangeable, so a central
tendency over them is not a statistic. Determinism makes that trap worse rather
than better — an arithmetic result carries an authority nobody audits. Counts
are inventory; an average would be a claim.

**Choosing between inventory and rows is a correctness decision, not a cost
one.** The inventory cannot answer anything about a particular estimate, table,
or subset, and a cheap wrong answer is worse than an expensive right one.

Use the row view whenever the question names a specific table, column, or
comparison, whenever you need the values themselves, and whenever you are
unsure. Rows are the safe default; the summary is the optimisation. **A projection is only worth reading
when it is selective** — an unfiltered one can exceed the source it was meant
to compress, so the script refuses to emit an over-budget projection and shows
you the most common terms instead.

## Reading the output correctly

**Check what the uncertainty column actually is.** The value under a
coefficient may be a standard error, a t-statistic, a p-value, or a CI bound.
These are visually identical and numerically incompatible. The parser reads it
from the table notes; when the notes do not say, it reports the type as
unstated rather than assuming. Projections spanning more than one type are
split with a warning and must not be merged.

**Trust the confidence column.** Anything below 1.0 carries a flag explaining
why. Any source other than the LaTeX itself is shown explicitly.

**Watch for the stacked-panel flag.** When one table stacks Panel A over
Panel B, the summary rows repeat and cannot be attributed to a panel, so they
are dropped and the table is flagged. The coefficients remain, each tagged with
its panel.

**Read the residue.** Tables the parser could not trust are recorded with a
reason, never silently dropped. A residue entry is a table you still need to
open by hand. Common causes: a layout with no coefficient rows (a variable
definitions table — correctly refused), coefficient rows of unequal width
(usually an unescaped ampersand in a row label, which also breaks LaTeX
compilation), or values wrapped in project-specific macros.

## Guarantees

- **Deterministic.** No model participates in extraction; numbers cannot be
  invented.
- **Conservation.** Every table found is either parsed or recorded as residue.
  The counts must balance, and the script fails hard if they do not.
- **Idempotent.** Re-running produces an identical store.
- **Fail loudly.** An unparsed table always beats a mis-parsed cell, because
  the reader will not see the source.

## Verifying an extraction

Deterministic parsing cannot invent a number but it can misalign one. This
re-reads the source with independent naive splitting and confirms sampled
values sit in the expected column:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parse_tables.py" verify --sample 50
```

Run it after the first extraction on any new corpus. A single misalignment
means the column model is wrong and every downstream comparison is suspect.

## Where things live

The store is derived, temporary data under the project's local state
directory, and is safe to delete at any time — it rebuilds from source. It is
not intended to be committed.

Full flag reference: see the plugin README.
