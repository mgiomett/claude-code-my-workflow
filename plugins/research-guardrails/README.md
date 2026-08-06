# research-guardrails

Cross-project `PreToolUse` guardrails for research repositories. One canonical
copy, loaded in **every** project, so a fix lands everywhere instead of being
copy-pasted into the next repo and then drifting.

## What it does

| Hook | Blocks | Notes |
|---|---|---|
| `git-guardrails.py` | `git reset --hard`, `git clean -f`, `git push --force`, blanket `git add -A/.`, mass `checkout/restore .`, `filter-branch`/`filter-repo` | Ordinary `add <path>`, `commit`, `tag`, `stash`, `fetch` are untouched |
| `git-guardrails.py` | *Prompts* on recursive delete (`rm -r`, `-rf`, `-f -r`, `--recursive`) | Backstop for the `Bash(rm -r*)` ask rules, which miss split flags and the long form. Plain `rm file.txt` runs freely |
| `git-guardrails.py` | `curl`/`wget` piped into a shell or interpreter | Cannot be a permission rule — Claude Code splits compound commands on `\|` and matches each subcommand independently, so `Bash(curl * \| bash)` never matches. The hook sees the whole string |
| `git-guardrails.py` | *Warns* on hardcoded `/Users/...` / `/home/...` paths written into `.R`, `.qmd`, `.do`, `.py`, `.Rmd` | Hardcoded paths are how a raw-data reference leaks into a replication package. `CLAUDE_STRICT_PATHS=1` turns the warning into a denial |
| `resource-guard.py` | Starting work when disk or memory is already below threshold | Two tiers: a floor for everything, a higher bar for recognizably expensive work |

Both hooks **fail open**: any internal error exits 0 with no decision. A bug in
a guardrail must never block your work.

> **Do not add a deny here for anything you want to *prompt* on.** A hook's deny
> beats an `ask` rule, so re-adding (say) `git merge` to this hook would silently
> kill the prompt configured in `~/.claude/settings.json`. Merge, rebase,
> cherry-pick, revert, and `branch -D` are gated there, by design — this hook
> deliberately passes them through.

## Configuration

All optional; defaults are sensible for a laptop.

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_STRICT_PATHS` | unset | `=1` denies (rather than warns on) hardcoded machine paths |
| `CLAUDE_RESOURCE_GUARD` | `1` | `=0` disables the resource guard entirely |
| `CLAUDE_MIN_DISK_MB` | `10240` | Floor tier — applies to all work |
| `CLAUDE_MIN_MEM_MB` | `4096` | Floor tier |
| `CLAUDE_HEAVY_DISK_MB` | `25600` | Heavy tier |
| `CLAUDE_HEAVY_MEM_MB` | `8192` | Heavy tier |
| `CLAUDE_HEAVY_PATTERN` | `simulations/`, `sim_*`, `benchmark_*` | Regex selecting what counts as heavy. Set per project when your expensive work lives elsewhere (e.g. `analysis/`). A malformed regex falls back to the default rather than disabling the guard |

## The permission floor that pairs with this

This plugin supplies the *hooks*. The other half — the deny / ask / allow rules — cannot
ship in a plugin (a plugin's `settings.json` accepts only `agent` and `subagentStatusLine`),
so it lives in `~/.claude/settings.json`. A working copy is committed here as
[`global-settings.example.json`](global-settings.example.json): 67 deny rules (credentials,
OS paths, `sudo`, disk and system tools), 14 ask rules (git history operations, recursive
delete, `gh repo`/`release`, remote repointing), and a deliberately wide allow layer.

Install or restore it with:

```bash
cp plugins/research-guardrails/global-settings.example.json ~/.claude/settings.json
```

Review it first — it is a starting point, not a mandate, and the cosmetic keys (`theme`,
notification toggles) are personal preference rather than part of the floor.

**Keeping the copy current:** this file is a snapshot, not a symlink. After changing
`~/.claude/settings.json`, refresh it with the reverse copy and commit:

```bash
cp ~/.claude/settings.json plugins/research-guardrails/global-settings.example.json
```

## Install

From the repository root:

```bash
./scripts/link-plugin.sh
```

This symlinks the plugin into `~/.claude/skills/`, where Claude Code
auto-discovers it as `research-guardrails@skills-dir` in every project — no
marketplace, no install step, no per-repo copy. Confirm with `/plugin`, and
after editing a hook run `/reload-plugins` (edits to hooks, unlike edits to a
`SKILL.md`, are not picked up live).

## What this is not

These are guardrails against **accident, not a security boundary.** They match
command strings, so an indirection like `g=add; git $g -A` still gets through,
and they cannot constrain a Python or R subprocess that opens a file itself.
The companion permission floor in `~/.claude/settings.json` has the same limit:
`Read`/`Edit` deny rules cover Claude's own file tools and recognized Bash file
commands, but not arbitrary subprocesses.

They raise the cost of a mistake. They do not make a session adversarially
safe. For OS-level enforcement, see
[sandboxing](https://code.claude.com/docs/en/sandboxing).

## Why a plugin rather than per-project hooks

Permission rules and hooks are separate mechanisms with different reach:

- **Permissions** cannot ship in a plugin (a plugin's `settings.json` accepts
  only `agent` and `subagentStatusLine`), so the deny/ask floor lives in
  `~/.claude/settings.json`.
- **Hooks** cannot usefully live in user settings if they reference
  `$CLAUDE_PROJECT_DIR` — that resolves to whichever project is open, so every
  repo without a local copy of the script gets a broken hook. Inside a plugin
  they use `${CLAUDE_PLUGIN_ROOT}` and resolve correctly everywhere.

That split is why this repository carries both a `plugins/` directory and a
documented user-settings floor.
