# Security policy

Report vulnerabilities privately through GitHub Security Advisories for
`fileworks/immich-export`. Do not include exported documents, media, or API tokens in
a report.

Security fixes target the latest release on PyPI.

## Threat model

`immich-export` is a command-line client that talks to **your own** Immich server and
writes to your own filesystem. It holds one secret — the API token — which is
read from the environment and is never written to disk, to a log line, or to an
export manifest.

Worth knowing when assessing a report:

- **Server responses are untrusted input.** Album names, people names, and tags become directory names. They are sanitised before they touch the filesystem; a path-traversal escape from a hostile or compromised server is a real vulnerability and we want the report.
- **Export directories are trusted.** The tool writes where you tell it to, and
  a path you passed is a path you meant.

## Out of scope

Anything that requires already holding your API token, and anything that depends
on the upstream server being trusted to behave — that is the server's own
security boundary, not this client's.
