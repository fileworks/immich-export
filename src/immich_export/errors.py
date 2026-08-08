"""User-facing errors with stable exit codes (never a raw traceback by default)."""

from __future__ import annotations

from .exit_codes import ExitCode


class ImmichExportError(Exception):
    """Base for all errors that should surface as a one-line human message."""

    exit_code: int = ExitCode.FATAL


class ConfigError(ImmichExportError):
    """Invalid flags, malformed URL, missing required options."""

    exit_code = ExitCode.USAGE


class AuthError(ImmichExportError):
    """API key rejected by the server (401/403)."""

    exit_code = ExitCode.USAGE


class ServerUnreachableError(ImmichExportError):
    """Connection refused, DNS failure, timeout."""

    exit_code = ExitCode.CONFLICT


class OutputError(ImmichExportError):
    """Output directory unwritable or out of space."""

    exit_code = ExitCode.FATAL


class AssetIntegrityError(ImmichExportError):
    """One asset cannot be verified; the completed run may continue as partial."""


class ChecksumError(AssetIntegrityError):
    """Immich's checksum is invalid or local/downloaded bytes do not match it."""
