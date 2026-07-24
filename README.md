# immich-export

Export supported originals and metadata from [Immich](https://immich.app) —
albums, people, tags, descriptions, favorites, and coordinates — into a
human-readable local folder tree.

The result is useful for inspection, migration, and an additional local copy.
It is not a replacement for independent, tested backups of Immich and its
database.

```
immich-export/
  library/2024/03/IMG_1234.jpg          # primary tree (self-contained mode)
  library/2024/03/IMG_1234.jpg.xmp      # sidecar: tags, people, albums, description, geo, favorite
  albums/Japan-2019/IMG_1234.jpg        # → symlink into library/
  people/Anna/IMG_1234.jpg              # → symlink into library/
  manifest.jsonl                        # append-only verified-state history
  manifest-current.jsonl                # authoritative current verified set
  manifest.csv                          # human-readable current projection
  export-report.txt                     # counts, warnings, errors, timing
```

## Install

```sh
pipx install immich-export
# or
brew install fileworks/tap/immich-export
```

Version `0.0.3` is published on
[PyPI](https://pypi.org/project/immich-export/0.0.3/), as a
[GitHub Release](https://github.com/fileworks/immich-export/releases/tag/v0.0.3),
and through `fileworks/tap`. Development after that tag remains unreleased
until the normal release workflow runs.

## Usage

```sh
export IMMICH_SERVER=https://immich.local:2283
export IMMICH_API_KEY=...   # Immich → Account Settings → API Keys

# full portable export (copies originals)
immich-export --out ./immich-export

# verified re-run: local originals are rehashed; only missing/changed bytes download
immich-export --out ./immich-export

# sidecar mode: you already have the Storage-Template tree mounted —
# only write .xmp sidecars + album/people views next to it
immich-export --mode sidecar --library-root /volume1/photos --out /volume1/photos

# custom primary tree
immich-export --layout "{year}/{album}" --out ./export
```

Key flags (see `immich-export --help` for all):

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `self-contained` | `self-contained` copies originals; `sidecar` only writes XMP + views next to an existing tree |
| `--layout` | `{year}/{month}` | primary tree; tokens `{year} {month} {day} {album} {type}`; `{album}` falls back to `Unsorted` |
| `--album-view` / `--people-view` | on | build `albums/` and `people/` symlink views |
| `--sidecars` | `xmp` | `xmp` or `none` |
| `--since` | — | only assets taken on/after this date |
| `--resume` | on | use prior state as a resume/migration hint; local bytes are still rehashed |
| `--include-hidden` | off | also export hidden + locked-folder assets |
| `--stale-assets` | `keep` | keep/report absent outputs or explicitly move owned outputs to `quarantine` |
| `--concurrency` | `4` | bound concurrent API work, downloads, and local verification |

## Verified behavior

- **Read-only against Immich.** Never writes back.
- **Verified originals.** Downloads are SHA-1 checked before atomic promotion,
  and existing local originals are rehashed on every run in both modes.
- **Canonical metadata and XMP.** All persisted/path/XMP fields share one typed
  state. Missing, malformed, or stale required XMP is atomically refreshed.
- **History versus current.** `manifest.jsonl` is append-only audit history.
  `manifest-current.jsonl`, its CSV projection, and generated views contain only
  assets verified by the latest completed compatible scan.
- **Partial runs are explicit.** Asset-specific integrity failures are reported,
  excluded from current state, and return exit code `5`; run-level failures do
  not replace the prior current snapshot.
- **Conservative reconciliation.** A compatible full scan removes absent assets
  from current state and views. Their files remain reported orphans by default.
  Explicit quarantine moves only manifest-owned outputs; sidecar mode never
  moves Immich-managed originals.
- **Bounded work and visible progress.** Assets are paged, concurrent work is
  bounded, and terminal/log progress remains available.

These checks cover the API fields and files the exporter supports. They cannot
detect metadata Immich does not expose, storage failures that occur after a
successful verification, or prove that an export can restore an entire Immich
installation. Keep separate backups and test restoration procedures.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including an empty library) |
| 2 | bad configuration or authentication failure |
| 3 | server unreachable |
| 4 | output directory unwritable / out of space |
| 5 | completed partial run with one or more asset failures |
| 1 | unexpected error (re-run with `--verbose` for the traceback) |

## Sidecar format

Standard XMP wherever a standard slot exists — `dc:subject` (tags),
`Iptc4xmpExt:PersonInImage` (people), `dc:description`, `photoshop:DateCreated`,
`exif:GPSLatitude/Longitude`, `xmp:Rating` (favorite → 5) — so digiKam,
Lightroom and exiftool can read them. Album membership and Immich ids live in a
custom `immich:` namespace in the same file.

When XMP is enabled, an asset is current only after its canonical sidecar
matches the manifest state. Exact generated-state validation removes metadata
that was deleted in Immich rather than retaining stale XMP nodes.

## Immich API compatibility

Built against the **Immich v3 API** (spec version 3.0.1). Instead of a
generated client, the exact API slice used is declared in
`src/immich_export/api_contract.py` and checked in CI against a vendored,
pruned copy of the official OpenAPI spec. To check a new Immich release:

```sh
uv run python scripts/refresh_api_spec.py --ref v3.1.0
uv run pytest tests/test_contract.py
```

A removed endpoint or field fails the tests *before* it breaks at runtime.

## Development

```sh
uv sync --all-extras --dev
uv run ruff check . && uv run ruff format --check .   # lint
uv run mypy                                           # strict types
uv run pytest                                         # tests (mock Immich API)
uv build                                              # sdist + wheel
```

Conventional Commits drive releases (`python-semantic-release`): merge to
`main` → version bump + changelog + GitHub Release + PyPI publish (OIDC) +
Homebrew formula bump.

For per-clone paths, commands, or preferences, create an ignored
`CLAUDE.local.md` at the repository root. Do not put credentials or other
secrets in it.

## License

MIT
