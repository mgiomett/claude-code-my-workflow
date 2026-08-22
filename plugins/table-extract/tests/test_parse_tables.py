#!/usr/bin/env python3
"""Invariant tests for parse_tables (Tier 2 of the plan's test strategy).

These check properties that must hold for ANY input, as distinct from the
golden fixtures (`--self-test`) which check specific expected values.

Run:  python3 tests/test_parse_tables.py
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import parse_tables as pt  # noqa: E402

FIXTURES = HERE / "fixtures"


def run_cli(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pt.main(list(argv))
    if code not in (0, 1):
        raise AssertionError(f"CLI exited {code}: {buf.getvalue()}")
    return buf.getvalue()


class TempProject(unittest.TestCase):
    """A throwaway project directory seeded with the fixture tables."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tblx-"))
        self.tables = self.root / "tables"
        self.tables.mkdir()
        for tex in FIXTURES.glob("*.tex"):
            shutil.copy(tex, self.tables / tex.name)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def extract(self, *extra):
        return run_cli("extract", str(self.tables),
                       "--project-root", str(self.root), *extra)

    def store(self):
        return json.loads(pt.store_file(self.root).read_text())


class TestConservation(TempProject):
    """Every tabular block found must be accounted for. Silent loss is the
    failure mode that would quietly corrupt a synthesis."""

    def test_every_block_is_parsed_or_residued(self):
        for tex in sorted(FIXTURES.glob("*.tex")):
            tables, residue, n_blocks = pt.parse_file(tex, tex.name)
            self.assertEqual(
                n_blocks, len(tables) + len(residue),
                f"{tex.name}: {n_blocks} blocks but "
                f"{len(tables)} parsed + {len(residue)} residue")

    def test_store_totals_match_sources(self):
        self.extract()
        store = self.store()
        total_blocks = sum(
            len(pt.find_tabulars(t.read_text()))
            for t in sorted(self.tables.glob("*.tex")))
        self.assertEqual(total_blocks,
                         len(store["tables"]) + len(store["residue"]))


class TestIdempotency(TempProject):
    def test_two_runs_produce_identical_store(self):
        self.extract()
        first = pt.store_file(self.root).read_text()
        self.extract("--force")
        self.assertEqual(first, pt.store_file(self.root).read_text())

    def test_reextract_replaces_never_appends(self):
        self.extract()
        n_first = len(self.store()["tables"])
        for _ in range(3):
            self.extract("--force")
        self.assertEqual(n_first, len(self.store()["tables"]),
                         "re-extraction inflated the table count")

    def test_no_duplicate_table_ids(self):
        self.extract()
        ids = [t["table_id"] for t in self.store()["tables"]]
        self.assertEqual(len(ids), len(set(ids)))


class TestCache(TempProject):
    def test_unchanged_input_is_skipped(self):
        self.extract()
        out = self.extract()
        self.assertIn("0 file(s) parsed", out)
        self.assertNotIn("0 cached", out)

    def test_touched_input_is_reparsed(self):
        self.extract()
        target = self.tables / "dialect_sym_parens.tex"
        target.write_text(target.read_text() + "\n% edited\n")
        out = self.extract()
        self.assertIn("1 file(s) parsed", out)

    def test_force_bypasses_cache(self):
        self.extract()
        out = self.extract("--force")
        self.assertIn("0 cached", out)

    def test_fresh_rebuilds_to_same_result(self):
        self.extract()
        before = self.store()["tables"]
        self.extract("--fresh")
        self.assertEqual(before, self.store()["tables"])

    def test_changed_value_is_reported_as_a_diff(self):
        self.extract()
        target = self.tables / "dialect_sym_parens.tex"
        target.write_text(target.read_text().replace("0.3420", "0.2980"))
        out = self.extract()
        self.assertIn("CHANGED VALUES", out)
        self.assertIn("treat", out)


class TestNoGuess(unittest.TestCase):
    """An unparseable layout must residue, never be forced into a row."""

    def test_descriptive_table_residues(self):
        tex = FIXTURES / "malformed_no_coefs.tex"
        tables, residue, _ = pt.parse_file(tex, tex.name)
        self.assertEqual(tables, [])
        self.assertEqual(len(residue), 1)
        self.assertIn("no coefficient rows", residue[0]["reason"])

    def test_inconsistent_coefficient_widths_residue(self):
        """Unequal coefficient rows make the column model ambiguous."""
        body = r"a & 1.0 & 2.0 \\ b & 1.0 & 2.0 & 3.0 \\"
        with self.assertRaises(pt.Residue) as ctx:
            pt.parse_tabular(body, 4)
        self.assertIn("inconsistent", str(ctx.exception))

    def test_consistent_widths_do_not_residue(self):
        body = r"a & 1.0 & 2.0 \\ b & 3.0 & 4.0 \\"
        self.assertEqual(pt.parse_tabular(body, 3)["n_cols"], 2)

    def test_unstated_uncertainty_is_unknown_not_assumed(self):
        tex = FIXTURES / "no_note_unknown.tex"
        tables, _, _ = pt.parse_file(tex, tex.name)
        self.assertEqual(tables[0]["uncertainty_type"], "unknown")
        self.assertLess(tables[0]["confidence"], 1.0)


class TestUncertaintyType(unittest.TestCase):
    def test_detection_by_keyword(self):
        cases = [
            (["Standard errors in parentheses"], "se"),
            (["t-statistics in brackets"], "tstat"),
            (["p-values in parentheses"], "pval"),
            (["95% confidence intervals in brackets"], "ci"),
            ([""], "unknown"),
            (["Some unrelated footnote"], "unknown"),
        ]
        for notes, want in cases:
            self.assertEqual(pt.detect_uncertainty_type(notes), want, notes)

    def test_proximity_breaks_ties(self):
        # Both terms present; the one next to the delimiter word wins.
        notes = ["Standard errors clustered by firm; t-statistics in parentheses"]
        self.assertEqual(pt.detect_uncertainty_type(notes), "tstat")


class TestFormatting(unittest.TestCase):
    """Regression: rstrip("0") corrupted integers — N=9450 rendered "945"."""

    def test_integers_keep_trailing_zeros(self):
        for value, want in [(9450.0, "9450"), (1000.0, "1000"),
                            (2800.0, "2800"), (0.0, "0")]:
            self.assertEqual(pt._fmt(value, 0), want)

    def test_decimals_are_trimmed(self):
        self.assertEqual(pt._fmt(0.3420), "0.342")
        self.assertEqual(pt._fmt(-1.1000), "-1.1")
        self.assertEqual(pt._fmt(None), "")


class TestStatRowsNotCoefficients(unittest.TestCase):
    """Regression: "R$^2$" and "Mean of dependent variable" were captured as
    coefficient rows because STAT_LABELS was an exact-string set."""

    def test_summary_rows_land_in_stats(self):
        tex = FIXTURES / "stat_rows_and_annotations.tex"
        tables, _, _ = pt.parse_file(tex, tex.name)
        t = tables[0]
        self.assertEqual(t["terms"], ["Specialization", "Constant"])
        for key in ("Mean of dependent variable", "R^2", "N"):
            self.assertIn(key, t["stats"])

    def test_text_valued_row_is_an_annotation(self):
        tex = FIXTURES / "stat_rows_and_annotations.tex"
        tables, _, _ = pt.parse_file(tex, tex.name)
        self.assertEqual(tables[0]["annotations"], {"Controls": ["Loan", "Loan"]})
        self.assertEqual(tables[0]["flags"], [])

    def test_stat_label_patterns(self):
        for label in ("N", "Observations", "R^2", "r squared", "Adj. R-squared",
                      "Mean of dependent variable", "Log likelihood", "Clusters"):
            self.assertTrue(pt.is_stat_label(label.lower()), label)
        for label in ("specialization", "treat", "post", "constant",
                      "industry capture"):
            self.assertFalse(pt.is_stat_label(label), label)


class TestDistribution(unittest.TestCase):
    """The inventory reports what the tables print, never what they mean."""

    def _store(self, ests):
        return {"schema": 1, "residue": [], "files": {}, "tables": [{
            "table_id": f"t{i}.tex#t0", "terms": ["treat"], "est": [[e]],
            "unc": [[0.1]], "stars": [[2 if i == 0 else 0]], "n_cols": 1,
            "spec_labels": ["(1)"], "dep_vars": ["y"], "stats": {},
            "uncertainty_type": "se", "source_type": "tex", "confidence": 1.0,
        } for i, e in enumerate(ests)]}

    def test_reports_counts(self):
        out = pt.project(self._store([0.5, 0.4, -0.2]), ["treat"], as_summary=True)
        self.assertIn("| 3 |", out)        # three estimates
        self.assertIn("Not** a meta-analysis", out)

    def test_reports_no_central_tendency(self):
        """A mean/median across non-exchangeable specifications is not a
        statistic. Its absence is the point, so it is asserted."""
        out = pt.project(self._store([0.5, 0.4, 0.3]), ["treat"], as_summary=True)
        for word in ("median", "mean", "average"):
            self.assertNotIn(word, out.lower())

    def test_no_editorial_emphasis(self):
        out = pt.project(self._store([0.5, 0.4, 0.3]), ["treat"], as_summary=True)
        self.assertNotIn("all +", out)

    def test_groups_by_outcome_never_pools(self):
        store = self._store([0.5, 0.4, -0.2])
        store["tables"][2]["dep_vars"] = ["risk"]
        out = pt.project(store, ["treat"], as_summary=True)
        self.assertIn("risk", out)
        rows = [l for l in out.splitlines() if l.startswith("| treat")]
        self.assertEqual(len(rows), 2, "outcomes were pooled into one row")

    def test_is_far_smaller_than_row_output(self):
        store = self._store([0.1 * i for i in range(1, 60)])
        rows = pt.project(store, ["treat"])
        summary = pt.project(store, ["treat"], as_summary=True)
        self.assertLess(len(summary) * 5, len(rows))


class TestProjectionBudget(TempProject):
    """An unfiltered projection can exceed the source it was meant to
    compress; the tool must refuse rather than flood the context."""

    def test_over_budget_projection_is_refused(self):
        self.extract()
        original = pt.PROJECTION_BUDGET
        try:
            pt.PROJECTION_BUDGET = 200
            out = run_cli("project", "--project-root", str(self.root))
        finally:
            pt.PROJECTION_BUDGET = original
        self.assertIn("Refusing", out)
        self.assertIn("--terms", out)

    def test_force_full_overrides(self):
        self.extract()
        original = pt.PROJECTION_BUDGET
        try:
            pt.PROJECTION_BUDGET = 200
            out = run_cli("project", "--force-full", "--project-root", str(self.root))
        finally:
            pt.PROJECTION_BUDGET = original
        self.assertNotIn("Refusing", out)


class TestProjectionRefusesToMix(unittest.TestCase):
    """SEs and t-statistics occupy the same visual slot but are not
    comparable; a projection must never silently merge them."""

    def _store(self):
        def table(tid, utype, est):
            return {
                "table_id": tid, "terms": ["treat"], "est": [[est]],
                "unc": [[0.1]], "stars": [[2]], "n_cols": 1,
                "spec_labels": ["(1)"], "dep_vars": ["y"], "stats": {},
                "uncertainty_type": utype, "source_type": "tex",
                "confidence": 1.0,
            }
        return {"schema": 1,
                "tables": [table("a.tex#t0", "se", 0.5),
                           table("b.tex#t0", "tstat", 0.6)],
                "residue": [], "files": {}}

    def test_mixed_types_are_split_and_flagged(self):
        out = pt.project(self._store(), ["treat"])
        self.assertIn("NOT comparable", out)
        self.assertIn("standard errors", out)
        self.assertIn("t-statistics", out)

    def test_single_type_has_no_warning(self):
        store = self._store()
        store["tables"] = store["tables"][:1]
        out = pt.project(store, ["treat"])
        self.assertNotIn("NOT comparable", out)


class TestNothingIsExcludedByDefault(TempProject):
    """Deciding a directory is superseded is a relevance judgement, and this
    tool does not make those. Pointing it at a directory must return every
    table in it — and an excluded file would bypass the conservation
    invariant, since it never enters the parsed/residue accounting."""

    def test_archive_named_paths_are_still_parsed(self):
        legacy = self.tables / "legacy"
        legacy.mkdir()
        shutil.copy(FIXTURES / "dialect_sym_parens.tex", legacy / "old_results.tex")
        self.extract()
        self.assertIn("legacy/old_results.tex", json.dumps(self.store()["files"]))

    def test_archive_named_paths_are_reported_not_dropped(self):
        legacy = self.tables / "deprecated"
        legacy.mkdir()
        shutil.copy(FIXTURES / "dialect_sym_parens.tex", legacy / "v1.tex")
        out = self.extract()
        self.assertIn("reads as archived", out)
        self.assertIn("WERE parsed", out)

    def test_exclude_is_opt_in(self):
        legacy = self.tables / "legacy"
        legacy.mkdir()
        shutil.copy(FIXTURES / "dialect_sym_parens.tex", legacy / "old_results.tex")
        self.extract("--exclude", "legacy")
        self.assertNotIn("legacy/old_results.tex", json.dumps(self.store()["files"]))

    def test_duplicate_basenames_are_reported(self):
        sub = self.tables / "v2"
        sub.mkdir()
        shutil.copy(FIXTURES / "dialect_sym_parens.tex",
                    sub / "dialect_sym_parens.tex")
        out = self.extract()
        self.assertIn("more than one directory", out)


class TestVerifyRoundTrip(TempProject):
    def test_verify_confirms_clean_extraction(self):
        self.extract()
        store = self.store()
        code = pt.run_verify(self.root, store, 50)
        self.assertEqual(code, 0)

    def test_verify_detects_injected_misalignment(self):
        self.extract()
        store = self.store()
        for t in store["tables"]:
            if "sym_parens" in t["table_id"]:
                t["est"][0][0], t["est"][0][1] = t["est"][0][1], t["est"][0][0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pt.run_verify(self.root, store, 200)
        self.assertEqual(code, 1, "swapped columns were not detected")
        self.assertIn("MISALIGNED", buf.getvalue())


class TestVerifierEdgeCases(unittest.TestCase):
    """Both cases produced false MISALIGNED reports on a real corpus."""

    def test_scientific_notation_is_read_whole(self):
        m = pt.NAIVE_NUM_RE.search("    -5.0e+07*  ")
        self.assertEqual(float(m.group(0)), -5.0e07)

    def test_repeated_label_occurrence_is_tracked(self):
        table = {"terms": ["a", "b", "a", "a"]}
        counts = [table["terms"][:i].count(t)
                  for i, t in enumerate(table["terms"])]
        self.assertEqual(counts, [0, 0, 1, 2])


class TestLogEnrichment(unittest.TestCase):
    """Logs carry full precision and the literal estimation command; binding
    them to a table column is by coefficient fingerprint, not by filename."""

    LOGS = HERE / "log_fixtures"

    def test_blocks_parse_without_command_echo(self):
        blocks = pt.parse_log(self.LOGS / "estimates.log")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["dep_var"], "outcome")
        self.assertGreaterEqual(len(blocks[0]["coefs"]), 3)
        self.assertIn("reghdfe", blocks[0]["cmd"])
        self.assertAlmostEqual(blocks[0]["coefs"]["treat"][0], 0.3417123)

    def test_fingerprint_binds_the_right_column(self):
        tex = self.LOGS / "matching_table.tex"
        tables, _, _ = pt.parse_file(tex, tex.name)
        n = pt.enrich_with_logs(tables, [self.LOGS / "estimates.log"], self.LOGS)
        self.assertEqual(n, 1)
        t = tables[0]
        self.assertEqual(t["log"]["matched_column"], 0)
        self.assertIn("absorb(id year)", t["log"]["cmd"])
        # Full precision recovered beyond the table's 4 displayed decimals.
        self.assertAlmostEqual(t["est_full"][0][0], 0.3417123)
        self.assertEqual(t["est"][0][0], 0.3417)

    def test_non_matching_log_is_ignored(self):
        tex = self.LOGS / "matching_table.tex"
        tables, _, _ = pt.parse_file(tex, tex.name)
        other = [{"cmd": "reg y x", "dep_var": "y", "file": "x.log",
                  "coefs": {"x": (99.9, 1.0), "z": (88.8, 1.0)}}]
        self.assertIsNone(pt.fingerprint_match(tables[0], other))

    def test_refuses_coincidental_match_on_near_zero_column(self):
        """C7: a column printing 0.000/0.000/0.000 must not bind to any log
        that happens to contain small coefficients."""
        table = {"terms": ["a", "b", "c"], "n_cols": 1,
                 "est": [[0.0], [0.0], [0.0]]}
        blocks = [{"cmd": "reg q z", "dep_var": "q", "file": "z.log",
                   "coefs": {"z": (0.0001, 1.0), "w": (-0.0002, 1.0),
                             "v": (5.0, 1.0)}}]
        self.assertIsNone(pt.fingerprint_match(table, blocks))

    def test_refuses_when_two_logs_both_explain_a_column(self):
        """C7: ambiguity is not a tie to break."""
        table = {"terms": ["a", "b", "c"], "n_cols": 1,
                 "est": [[0.5], [0.25], [-0.75]]}
        coefs = {"x": (0.5, 1.0), "y": (0.25, 1.0), "z": (-0.75, 1.0)}
        blocks = [{"cmd": "reg one", "dep_var": "o", "file": "a.log",
                   "coefs": dict(coefs)},
                  {"cmd": "reg two", "dep_var": "o", "file": "b.log",
                   "coefs": dict(coefs)}]
        self.assertIsNone(pt.fingerprint_match(table, blocks))


class TestGroupBy(unittest.TestCase):
    """Comparing one term across files/dirs/vintages was the one question the
    tool could not express, so it kept being answered with throwaway code --
    which is where every analysis error came from."""

    def _store(self):
        def t(tid, est):
            return {"table_id": tid, "terms": ["treat"], "est": [[est]],
                    "unc": [[0.1]], "stars": [[1]], "n_cols": 1,
                    "spec_labels": ["(1)"], "dep_vars": ["y"], "stats": {},
                    "uncertainty_type": "se", "source_type": "tex",
                    "confidence": 1.0, "notes": "SEs in parentheses",
                    "flags": []}
        return {"schema": 1, "residue": [], "files": {}, "tables": [
            t("v1/a.tex#t0", 0.5), t("v1/b.tex#t0", 0.6), t("v2/a.tex#t0", 0.7)]}

    def test_group_by_dir_splits_vintages(self):
        out = pt.project(self._store(), ["treat"], group_by="dir")
        self.assertIn("Grouped by dir: 2 group(s)", out)
        self.assertIn("## v1", out)
        self.assertIn("## v2", out)

    def test_group_by_file_splits_schemes(self):
        out = pt.project(self._store(), ["treat"], group_by="file")
        self.assertIn("Grouped by file: 2 group(s)", out)

    def test_grouping_never_loses_or_duplicates_a_coefficient(self):
        store = self._store()
        for mode in pt.GROUP_KEYS:
            out = pt.project(store, ["treat"], group_by=mode)
            self.assertIn("3 coefficient(s).", out, mode)

    def test_unknown_key_is_refused_not_ignored(self):
        out = pt.project(self._store(), ["treat"], group_by="colour")
        self.assertIn("Unknown --group-by", out)

    def test_shared_path_prefix_is_printed_once(self):
        store = self._store()
        for t in store["tables"]:
            t["table_id"] = "/very/long/shared/root/" + t["table_id"]
        out = pt.project(store, ["treat"], group_by="dir")
        self.assertIn("paths relative to", out)
        self.assertNotIn("## /very/long/shared/root/v1", out)


class TestNotes(unittest.TestCase):
    """The store always carried notes; nothing surfaced them, so "what do the
    table notes say" needed the source file."""

    def _store(self, note, flags=()):
        return {"schema": 1, "residue": [], "files": {}, "tables": [{
            "table_id": "a.tex#t0", "terms": ["treat"], "est": [[0.5]],
            "unc": [[0.1]], "stars": [[1]], "n_cols": 1, "spec_labels": ["(1)"],
            "dep_vars": ["y"], "stats": {}, "uncertainty_type": "se",
            "source_type": "tex", "confidence": 1.0, "notes": note,
            "flags": list(flags)}]}

    def test_notes_absent_by_default(self):
        out = pt.project(self._store("Robust SEs clustered by firm."), ["treat"])
        self.assertNotIn("Table notes", out)

    def test_notes_shown_on_request(self):
        out = pt.project(self._store("Robust SEs clustered by firm."), ["treat"],
                         show_notes=True)
        self.assertIn("Table notes", out)
        self.assertIn("clustered by firm", out)

    def test_flags_surface_with_notes(self):
        out = pt.project(self._store("", ["stacked panels: stats dropped"]),
                         ["treat"], show_notes=True)
        self.assertIn("stacked panels", out)


class TestDialectProbe(unittest.TestCase):
    """The worst shipped bug was silent because nothing reported which markup
    a corpus used; an unsupported convention could only be found by its damage."""

    def _hits(self, text):
        return [n for n, pat in pt.DIALECT_PROBES if pat.search(text)]

    def test_detects_each_star_convention(self):
        self.assertIn("stars: \\sym{***}", self._hits(r"0.34\sym{***}"))
        self.assertIn("stars: $^{***}$ / ^{***}", self._hits(r"0.34$^{***}$"))
        self.assertIn("stars: bare ***", self._hits("0.34***"))
        self.assertIn("stars: \\textsuperscript{***}",
                      self._hits(r"0.34\textsuperscript{***}"))

    def test_detects_delimiters_and_decimal_comma(self):
        self.assertIn("uncertainty in ( )", self._hits("& (0.091)"))
        self.assertIn("uncertainty in [ ]", self._hits("& [0.091]"))
        self.assertIn("decimal comma", self._hits("& -0,342"))

    def test_plain_prose_triggers_nothing(self):
        self.assertEqual(self._hits("This table reports baseline results."), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
