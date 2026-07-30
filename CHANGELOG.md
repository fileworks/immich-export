# CHANGELOG


## v0.1.1 (2026-07-30)

### Bug Fixes

- Refresh uv.lock during release so its version cannot lag
  ([#10](https://github.com/fileworks/immich-export/pull/10),
  [`bfa4a98`](https://github.com/fileworks/immich-export/commit/bfa4a98966a2203c4d40ca7f7de91d67f8bbc9d8))

semantic-release rewrites pyproject.toml and __init__.py, but not uv.lock. So 0.1.0 shipped to PyPI
  with the lock still saying 0.0.4, and the Homebrew bump refused it — correctly — with "uv.lock
  project version does not match the requested release". The tap is still on 0.0.4 as a result.

Nothing caught it because `source_versions` only compared pyproject.toml and __init__.py. uv.lock is
  now one of the locations it inspects, so a lagging lock is a disagreement rather than a silence.

`prepare` ports what paperless-export already does and is the reason its 1.1.0 bump succeeded today
  while this one did not. Only the project's own entry may move: a release is the wrong moment to
  silently re-resolve dependencies, so if anything else in the lock changed, the refresh is reverted
  and the release fails.

Also corrects the current lock, which is one line and version-only — verified with
  `_lock_without_project_version`, which reports the rest of the resolution unchanged.

Uses PROJECT throughout rather than the repeated literal name paperless-export hardcodes.

Co-authored-by: gykonik <gykonik@gmail.com>

- Treat both lock identities as sources, and check the tagged lock
  ([#11](https://github.com/fileworks/immich-export/pull/11),
  [`a732c74`](https://github.com/fileworks/immich-export/commit/a732c742446b3ab3dd0c5b0d94a088b48478fc56))

My previous change added uv.lock to `source_versions` but not to `source_names`, the allow-list of
  keys that are *not* distribution filenames. `verify` therefore read "uv.lock" as an artifact and
  the 0.1.1 release failed with:

Distribution filenames disagree with 0.1.1: ['immich_export-0.1.1-py3-none-any.whl',
  'immich_export-0.1.1.tar.gz', 'uv.lock']

The wheel and sdist were correct; the check was wrong.

`tagged_source_versions` now reads the tag's uv.lock as well. The working tree agreeing is not the
  guarantee that matters — what shipped is the tag, and that is what the Homebrew bump resolves.
  paperless-export already checked both; this finishes the parity rather than half of it.

Co-authored-by: gykonik <gykonik@gmail.com>


## v0.1.0 (2026-07-30)

### Features

- Add durable manifests, run history, and scale budgets
  ([#9](https://github.com/fileworks/immich-export/pull/9),
  [`30fe418`](https://github.com/fileworks/immich-export/commit/30fe4180ee3f68d9c6eb6b13e6834f2961cffe7a))

* fix: align release integrity gates

* feat: add durable manifests, run history, and scale budgets

An interrupted export left a truncated manifest line that made every later run crash, which broke
  the resume the manifest exists to provide. The durable manifest writes atomically and reads past
  damaged lines, reporting them instead of dying on them.

Adds a run history so a repeated export can say what changed since the last one, structured logging
  with a configured level rather than bare prints, and recorded performance budgets with a test that
  fails when a run exceeds them.

Owned by the `scale-immich-export-observability` OpenSpec change.

* test: check documented flags against declarations, not rendered help

The test asserted each option appeared in `--help` output, which is Rich's render: it wraps, colours
  and boxes to the terminal it thinks it has. That made the assertion a function of the runner's
  width. It passed at every width locally and failed on CI, where the option names were not in the
  rendered text at all.

The contract is "the README documents the flags that exist", so it now reads the command tree. Same
  coverage, no dependency on how help happens to be drawn.

* test: include a flag's negative form when reading declarations

A boolean flag declares `--symlinks` in `opts` and `--no-symlinks` in `secondary_opts`, so reading
  only the former would miss any negative form the README documents.

---------

Co-authored-by: gykonik <gykonik@gmail.com>


## v0.0.4 (2026-07-26)

### Bug Fixes

- Harden verified export state ([#7](https://github.com/fileworks/immich-export/pull/7),
  [`ef1941f`](https://github.com/fileworks/immich-export/commit/ef1941f3034cf34bb3a24cd2a9ed2fe0dd062319))

- Verify current media, metadata, sidecars, and views - Gate the next release and include 3f2a592
  resume/progress

- Make release cleanup checkout-safe ([#8](https://github.com/fileworks/immich-export/pull/8),
  [`38930be`](https://github.com/fileworks/immich-export/commit/38930be8d82433038f21b356c1c8e0d883ae483f))

- preserve the exact-SHA release gate


## v0.0.3 (2026-07-13)

### Bug Fixes

- **release**: Attach the sdist and wheel to the GitHub Release
  ([#4](https://github.com/fileworks/immich-export/pull/4),
  [`34bb701`](https://github.com/fileworks/immich-export/commit/34bb701f727c42ac626f974a9d1665be58f79b59))

The semantic-release action only runs 'version' — it bumps, tags, writes the changelog and creates
  the GitHub Release, but it never runs 'publish'. So every Release was created with no files
  attached: a bare tag pointing at PyPI.

Upload the sdist and wheel that semantic-release already built, so the Release stands on its own.
  That matters for a tool whose whole purpose is to be an escape hatch — you should be able to grab
  it from GitHub without going through a package index.

Uses the preinstalled gh CLI rather than another action, so the org's Actions allow-list does not
  need a new entry.


## v0.0.2 (2026-07-13)

### Bug Fixes

- **release**: Drop the duplicate build that broke publishing
  ([#3](https://github.com/fileworks/immich-export/pull/3),
  [`2f8683b`](https://github.com/fileworks/immich-export/commit/2f8683b21f8b0b75ffa0e6805547e450af394896))

The release job died at 'Build sdist + wheel' with

PermissionError: [Errno 13] Permission denied: dist/<pkg>-0.0.1.tar.gz

python-semantic-release already builds the sdist and wheel into dist/ via the build_command in
  pyproject.toml. It is a Docker action running as root, so those artefacts are root-owned. The
  workflow then ran a second, redundant 'uv build' as the unprivileged runner user, which cannot
  overwrite them.

Publish to PyPI and the Homebrew bump are both gated on that step, so a permission error on a build
  that never needed to happen silently took out the entire release.

Remove the duplicate build and the setup-uv step that only served it, and let the publish action
  consume the dist/ that semantic-release produced.

- **release**: Give checkout the release token so the version push is allowed
  ([#2](https://github.com/fileworks/immich-export/pull/2),
  [`6ae635e`](https://github.com/fileworks/immich-export/commit/6ae635e0f7d3a0a1e65a40e76d470d5bbd5122a3))

The release job fails with GH013 — the chore(release) version commit cannot be pushed to protected
  main.

Handing SEMANTIC_RELEASE_TOKEN to the python-semantic-release action is not enough: that input only
  authenticates GitHub API calls. The actual git push uses the credentials actions/checkout persists
  into the remote's extraheader, which were still the default GITHUB_TOKEN — an identity with no
  ruleset bypass.

Set the token on checkout as well, mirroring media-sorter's semantic-release workflow, so the push
  authenticates as an org owner and the always-bypass applies.

### Continuous Integration

- Allow CI to be triggered manually ([#1](https://github.com/fileworks/immich-export/pull/1),
  [`0e64d69`](https://github.com/fileworks/immich-export/commit/0e64d694c92fcd85992101ce249aaab9dd45c63e))

* ci: allow CI to be triggered manually

workflow_dispatch makes it possible to re-run checks without pushing a commit — needed while
  verifying the org's Actions allow-list, and useful any time a run fails for reasons outside the
  code.

* fix(release): push the version commit with a token that can bypass the ruleset

semantic-release commits the version bump to main. The branch ruleset requires a PR, and the default
  GITHUB_TOKEN has no bypass — GitHub rejects the Actions app as a bypass actor ("must be part of
  the ruleset source or owner organization"), so the release job could never land its commit.

SEMANTIC_RELEASE_TOKEN is a fine-grained PAT that acts as an org owner, who already holds an
  always-bypass on the ruleset. Falls back to GITHUB_TOKEN so nothing breaks where the secret is
  absent.
