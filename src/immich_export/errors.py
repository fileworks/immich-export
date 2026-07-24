"""User-facing errors with stable exit codes (never a raw traceback by default)."""

from __future__ import annotations

EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_UNREACHABLE = 3
EXIT_OUTPUT = 4
EXIT_PARTIAL = 5


class ImmichExportError(Exception):
    """Base for all errors that should surface as a one-line human message."""

    exit_code: int = EXIT_UNEXPECTED


class ConfigError(ImmichExportError):
    """Invalid flags, malformed URL, missing required options."""

    exit_code = EXIT_CONFIG


class AuthError(ImmichExportError):
    """API key rejected by the server (401/403)."""

    exit_code = EXIT_CONFIG


class ServerUnreachableError(ImmichExportError):
    """Connection refused, DNS failure, timeout."""

    exit_code = EXIT_UNREACHABLE


class OutputError(ImmichExportError):
    """Output directory unwritable or out of space."""

    exit_code = EXIT_OUTPUT


class AssetIntegrityError(ImmichExportError):
    """One asset cannot be verified; the completed run may continue as partial."""


class ChecksumError(AssetIntegrityError):
    """Immich's checksum is invalid or local/downloaded bytes do not match it."""
