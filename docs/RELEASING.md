# Releasing the fork

This document describes how `kskylespence/TradingAgents` (the
"HedgeFund" fork) cuts a new version, syncs from upstream
`TauricResearch/TradingAgents`, and handles the occasional out-of-band
hotfix. The fork tags a new version on **every Coolify deploy** — the
deployed image is the meaningful release artifact, so each
production push gets a real version string and changelog entry.

The fork uses PEP 440 local segments to layer its own counter on top of
upstream's version: `<upstream-base>+hf.<N>`. The `+hf.` segment makes
the version installable by pip but unpublishable to PyPI by design
(see [Why this scheme?](#why-this-scheme) at the bottom).

## Version ↔ tag ↔ upstream reference table

| pyproject.toml version | Git tag       | Upstream base                  |
|------------------------|---------------|--------------------------------|
| 0.2.5+hf.1             | v0.2.5-hf.1   | TauricResearch v0.2.5          |
| 0.2.5+hf.2             | v0.2.5-hf.2   | TauricResearch v0.2.5          |
| 0.3.0+hf.1             | v0.3.0-hf.1   | TauricResearch v0.3.0 (future) |

Note the asymmetry: the **version string** uses `+` (PEP 440 local
segment delimiter), the **git tag** uses `-` (`+` is not a legal git
ref character). The two are intentionally one-to-one — `v` prefix and
`+` → `-` is the only transform.

Two files carry the version string and **MUST** stay in lockstep:

- `pyproject.toml` — `[project] version = "..."`
- `web/backend/app/__init__.py` — `__version__ = "..."`

The web backend's `/api/health` endpoint reports `__version__` so that
the running Coolify build can be verified against the tag.

## 1. Cutting a fork release (the deploy workflow)

This is the workflow you run before every Coolify deploy. Assumes a
clean working tree on `main` with `origin` pointing at the fork.

### 1.1 Determine the next version

Look at the most recent `v*-hf.*` tag:

```powershell
git describe --tags --abbrev=0 --match "v*-hf.*"
```

```bash
git describe --tags --abbrev=0 --match 'v*-hf.*'
```

- If the upstream base hasn't moved since that tag, increment `+hf.N`
  by one (e.g. `0.2.5+hf.1` → `0.2.5+hf.2`).
- If the upstream-merge workflow (section 2) has run since the last
  cut and bumped the upstream base, reset the counter: `0.2.5+hf.4`
  → `0.3.0+hf.1`.

For the rest of this section, assume the new version is
`0.2.5+hf.2`, which corresponds to tag `v0.2.5-hf.2`.

### 1.2 Draft the changelog section with git-cliff

`cliff.toml` at the repo root is configured to group conventional
commits into the same headings `CHANGELOG.fork.md` uses. Draft into a
scratch file rather than the changelog directly — the bullets always
need hand-editing before they read like prose.

```powershell
git cliff --unreleased --tag v0.2.5-hf.2 > $env:TEMP\fork-draft.md
```

```bash
git cliff --unreleased --tag v0.2.5-hf.2 > /tmp/fork-draft.md
```

Open the draft and rewrite each bullet to match the voice of existing
`CHANGELOG.fork.md` entries: bold the feature name with a period,
then a sentence or two on the *why*, not just the *what*. Prefer
"Coolify build originally failed when X" over "fix Coolify build."

### 1.3 Update `CHANGELOG.fork.md`

Three edits, in order:

1. Rename the current `## [Unreleased]` heading to
   `## [0.2.5+hf.2] — YYYY-MM-DD` (today's date), and replace its
   contents with the hand-edited bullets from the draft.
2. Re-create an empty `## [Unreleased]` block at the top with all
   five subheadings present (`### Added`, `### Changed`, `### Fixed`,
   `### Removed`, `### Security`). The five empty subheadings are
   load-bearing — keeping them present means the next contributor
   never has to guess which heading to add a line under.
3. Add the compare link at the bottom of the file:

   ```
   [0.2.5+hf.2]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5-hf.1...v0.2.5-hf.2
   ```

   And update the existing `[Unreleased]` link to compare against the
   new tag:

   ```
   [Unreleased]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5-hf.2...HEAD
   ```

### 1.4 Bump the version strings

Both files must change together; mismatched versions are caught by the
`/api/health` step below but are easier to fix before the tag exists.

- `pyproject.toml`:

  ```toml
  [project]
  version = "0.2.5+hf.2"
  ```

- `web/backend/app/__init__.py`:

  ```python
  __version__ = "0.2.5+hf.2"
  ```

### 1.5 Commit, tag, push

```powershell
git add CHANGELOG.fork.md pyproject.toml web/backend/app/__init__.py
git commit -m "chore(release): 0.2.5+hf.2"
git tag v0.2.5-hf.2
git push origin main --tags
```

```bash
git add CHANGELOG.fork.md pyproject.toml web/backend/app/__init__.py
git commit -m "chore(release): 0.2.5+hf.2"
git tag v0.2.5-hf.2
git push origin main --tags
```

The `chore(release):` prefix is what `cliff.toml` uses to exclude the
release commit itself from the *next* draft. Don't change it.

### 1.6 Trigger Coolify redeploy

If Coolify's auto-deploy webhook on `main` is enabled, the `git push`
above already kicked off a build. Otherwise hit the redeploy button
in the Coolify dashboard (or use the `coolify` MCP tools).

### 1.7 Verify the deployed build matches the tag

Once Coolify reports the deploy as healthy, hit `/api/health` and
confirm the `version` field matches what you just tagged:

```powershell
curl https://<coolify-host>/api/health | ConvertFrom-Json | Select-Object -ExpandProperty version
# expect: 0.2.5+hf.2
```

```bash
curl -s https://<coolify-host>/api/health | jq -r .version
# expect: 0.2.5+hf.2
```

If the returned version doesn't match the tag, Coolify is either
serving a stale image or built from the wrong ref. Check the Coolify
deployment logs before assuming the version files are wrong locally.

## 2. Syncing from upstream

Two sub-workflows: catching up to `upstream/main` between upstream
releases, and absorbing a tagged upstream release. Both end with new
commits on the fork's `main` that are folded into the *next* fork cut
(section 1) — syncing does not by itself produce a release.

### 2.1 Catching up to `upstream/main`

When upstream lands work but hasn't tagged a release:

1. Fetch and inspect what's incoming:

   ```powershell
   git fetch upstream
   git log --oneline HEAD..upstream/main
   ```

   ```bash
   git fetch upstream
   git log --oneline HEAD..upstream/main
   ```

2. Squash-merge so the entire upstream pull becomes a single commit
   on the fork's history. This keeps `git log --oneline` on the fork
   readable, and the squash-commit shows up in `cliff.toml`'s
   `Upstream` group as one line rather than dozens.

   ```bash
   git merge --squash upstream/main
   git commit -m "chore(upstream): sync to upstream/main @ <short-sha>"
   ```

   Replace `<short-sha>` with the 7-character SHA of
   `upstream/main`'s tip. The `chore(upstream)` prefix routes the
   commit to the `Upstream` section of the next draft.

3. Resolve any conflicts. Two ground rules:

   - **`CHANGELOG.md` should never conflict.** It's an unchanged
     mirror of upstream's changelog and only upstream edits it. If a
     conflict appears, something has gone wrong locally — take
     upstream's version verbatim:

     ```bash
     git checkout --theirs CHANGELOG.md
     git add CHANGELOG.md
     ```

   - **`CHANGELOG.fork.md` would only conflict if upstream added a
     file by that name** (extremely unlikely). If it does, keep ours:

     ```bash
     git checkout --ours CHANGELOG.fork.md
     git add CHANGELOG.fork.md
     ```

4. Run the test suite and fix anything that broke from the sync.
   Common breakage surfaces: capability-table rows for newly-added
   models, default-config keys that upstream renamed, and analyst
   wiring in `graph/setup.py`.

   ```powershell
   pytest
   ```

5. Append a line to `CHANGELOG.fork.md` under `[Unreleased]`,
   creating an `### Upstream` subheading if one doesn't exist yet:

   ```markdown
   ### Upstream

   - **Synced with upstream/main @ <short-sha>** — <one-line summary
     of what came in, e.g. "new DeepSeek-V4 model rows + a fix for
     Anthropic tool-choice on Opus 4.7."
   ```

### 2.2 Absorbing a tagged upstream release

When upstream tags a new release (e.g. `v0.2.6`):

1. Same procedure as above, but merge the tag instead of `main`:

   ```bash
   git fetch upstream --tags
   git merge --squash v0.2.6
   git commit -m "chore(upstream): sync to upstream v0.2.6"
   ```

2. The next fork cut will use `0.2.6+hf.1` — the counter resets the
   moment the upstream base moves forward. Don't pre-bump the
   version files; the bump happens during section 1.

3. *Optional but recommended:* also merge the tag itself (no-fast-
   forward) so `git describe` cleanly identifies the upstream
   baseline in our history graph:

   ```bash
   git merge --no-ff v0.2.6 -m "chore(upstream): merge tag v0.2.6"
   ```

   This adds a second commit but makes `git describe --match 'v*'`
   on the fork resolve back to the upstream tag, which is useful
   when diagnosing "what upstream version is this fork actually
   based on?"

## 3. Hotfix workflow

Occasionally a fix needs to land on `main` without an immediate
Coolify deploy — for example, a docs-only correction, a CI fix, or a
backport queued for the next deploy window. The workflow is identical
to section 1 with two differences:

1. **Skip step 1.6.** Don't redeploy. The cut exists so the fix lands
   on `main` with a real version, but Coolify keeps serving the
   previous tag until the next planned deploy.

2. **Document the deferral in the commit message body**, so the
   "release cut without deploy" intent is recorded:

   ```powershell
   git commit -m "chore(release): 0.2.5+hf.3

   Hotfix: <short description>. Not deployed — next scheduled deploy
   will pick this up alongside <expected next bundle>."
   ```

The verification step (1.7) becomes "verify on next deploy" — note
the hotfix version in your deploy plan so 1.7 runs against the
*latest* tag when the actual deploy happens.

Hotfix cuts still update both version files and still tag. The whole
point of the scheme is that every push to `main` is reproducible from
a tag; skipping the tag would break that.

## Why this scheme?

PEP 440 [local version identifiers](https://peps.python.org/pep-0440/#local-version-identifiers)
(the `+segment` syntax) are pip-installable but rejected by PyPI by
design — exactly what a downstream fork wants. A fork that publishes
`0.2.5+hf.2` to PyPI would be both a license-and-trademark mess and a
namespace pollution risk for upstream users; making the version
literally unpublishable removes the temptation and the foot-gun. The
same pattern is used by PyTorch (`+cpu`, `+cu118` build tags) and by
Forgejo's downstream Gitea fork. The `hf` suffix is short for
"HedgeFund," the working-tree folder name this repo lives under,
chosen for being unambiguous against `+local` or `+fork` (both of
which collide with conventions used by other tooling).

The per-deploy cadence pairs naturally with the `+hf.N` counter: the
deploy *is* the release, the tag is the audit trail, and `/api/health`
on the running container is the verification step. Upstream's
semver-on-tags discipline still governs the `<upstream-base>` portion
of the version, so `pip install` resolvers continue to see this fork
as "a build of upstream 0.2.5" — which it is.
