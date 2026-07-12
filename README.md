# immich-export

Pull **everything** out of [Immich](https://immich.app) — the files **and** the
metadata that only lives in its database (albums, people, tags, descriptions,
favorites, geo) — into a redundant, human-readable local folder tree.

This is the media escape hatch that makes "Immich = source of truth"
reversible: if Immich vanished tomorrow, the export is a plain tree you can
browse, grep, or import anywhere else.

```
immich-export/
  library/2024/03/IMG_1234.jpg          # primary tree (self-contained mode)
  library/2024/03/IMG_1234.jpg.xmp      # sidecar: tags, people, albums, description, geo, favorite
  albums/Japan-2019/IMG_1234.jpg        # → symlink into library/
  people/Anna/IMG_1234.jpg              # → symlink into library/
  manifest.jsonl                        # one line per asset: id, checksum, path, all metadata
  manifest.csv                          # the same, human-readable
  export-report.txt                     # counts, warnings, errors, timing
```

## Install

```sh
pipx install immich-export
# or
brew install fileworks/tap/immich-export
```

*(Not yet published — first release pending; until then: `uv run immich-export` from a checkout.)*

## Usage

```sh
export IMMICH_SERVER=https://immich.local:2283
export IMMICH_API_KEY=...   # Immich → Account Settings → API Keys

# full portable export (copies originals)
immich-export --out ./immich-export

# incremental re-run: only new/changed assets are downloaded
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
| `--resume` | on | skip assets already exported with an unchanged checksum (from `manifest.jsonl`) |
| `--include-hidden` | off | also export hidden + locked-folder assets |

## Guarantees

- **Read-only against Immich.** Never writes back.
- **Best-effort.** One bad asset logs an error and the run continues; the report lists every failure.
- **Idempotent / resumable.** The manifest records each asset's checksum; re-runs download only new or changed files, and refresh sidecars when only metadata (albums/tags/people) changed.
- **Verifiable.** Every download is checked against Immich's SHA-1; `manifest.jsonl` lets you diff export-vs-server (or vs. a restore) any time.
- **Streams.** Assets are paged and downloaded with bounded concurrency — a 100k-asset library never sits in memory.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including an empty library) |
| 2 | bad configuration or authentication failure |
| 3 | server unreachable |
| 4 | output directory unwritable / out of space |
| 1 | unexpected error (re-run with `--verbose` for the traceback) |

## Sidecar format

Standard XMP wherever a standard slot exists — `dc:subject` (tags),
`Iptc4xmpExt:PersonInImage` (people), `dc:description`, `photoshop:DateCreated`,
`exif:GPSLatitude/Longitude`, `xmp:Rating` (favorite → 5) — so digiKam,
Lightroom and exiftool can read them. Album membership and Immich ids live in a
custom `immich:` namespace in the same file.

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

## License

MIT
