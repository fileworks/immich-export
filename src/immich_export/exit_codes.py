"""Process exit codes, shared in meaning across the fileworks CLIs.

The three tools are driven from the same scripts, so a code has to mean the
same thing in each of them. It did not: `1` was "unexpected error" here and
"partial success" in `unpacksort`, and `3` and `4` disagreed the same way.
This is that vocabulary, matching `unpacksort.models.ExitOutcome`.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable process outcomes."""

    #: Everything asked for was done.
    SUCCESS = 0
    #: Some of the work completed and some did not; the output is usable but
    #: incomplete, and the report says which parts are missing.
    PARTIAL = 1
    #: The invocation itself is wrong — bad flags, missing paths, malformed
    #: URL, or credentials the server rejected. Nothing was attempted.
    USAGE = 2
    #: Something this tool depends on is unreachable or in a state it cannot
    #: proceed from.
    CONFLICT = 3
    #: An unexpected failure, or output that could not be written.
    FATAL = 4
    #: Cancelled by the operator, following the shell convention.
    INTERRUPTED = 130
