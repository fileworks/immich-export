# Repository instructions

- Keep the client read-only; never call an Immich endpoint that mutates.
- No httpx exception escapes `client.py` — translate to `errors.py` types.
- Publish output only after the SHA-1 is verified, via a same-directory
  temporary and an atomic replace inside the export boundary.
- Keep exit codes on the shared `ExitCode` vocabulary (see README).
- Keep parser boundaries typed and keep strict mypy enabled globally.
- Keep credentials out of files, logs, manifests, reports, and command output.
- Use Conventional Commits; do not add automated co-author trailers.
