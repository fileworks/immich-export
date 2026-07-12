# CHANGELOG


## v0.0.1 (2026-07-12)

### Bug Fixes

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
