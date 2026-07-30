from __future__ import annotations

import logging
from pathlib import Path

import pytest

import immich_export.logging_config as logging_config
from immich_export.logging_config import configure_logging


def test_rotating_log_redacts_api_keys_and_url_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logfile = tmp_path / "immich-export.log"
    secret = "immich-api-secret"
    monkeypatch.setattr(logging_config, "LOG_MAX_BYTES", 350)
    monkeypatch.setattr(logging_config, "LOG_BACKUPS", 2)
    configure_logging(logfile, verbose=False, secrets=(secret,))
    logger = logging.getLogger("immich_export.test")

    for index in range(30):
        logger.warning(
            "request %s https://immich.example/api/assets?key=%s&token=visible",
            index,
            secret,
        )
    for handler in logging.getLogger().handlers:
        handler.flush()

    paths = [logfile, logfile.with_suffix(".log.1"), logfile.with_suffix(".log.2")]
    content = "".join(path.read_text() for path in paths if path.exists())
    assert secret not in content
    assert "token=visible" not in content
    assert "?[REDACTED]" in content
    assert logfile.with_suffix(".log.1").exists()
