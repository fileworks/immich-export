from __future__ import annotations

import io

from immich_export.progress import LOG_EVERY, Progress


class _FakeTty(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class TestTerminal:
    def test_repaints_a_single_live_line(self) -> None:
        stream = _FakeTty()
        with Progress(stream) as progress:
            for _ in range(3):
                progress.exported()

        output = stream.getvalue()
        assert "\r" in output  # repainted in place rather than scrolled
        assert "3 exported" in output
        assert output.endswith("\n")  # cursor released on close

    def test_counts_each_outcome_separately(self) -> None:
        stream = _FakeTty()
        with Progress(stream) as progress:
            progress.exported()
            progress.skipped()
            progress.failed()

        final = stream.getvalue()
        assert "1 exported" in final
        assert "1 up to date" in final
        assert "1 failed" in final

    def test_note_does_not_leave_the_counter_mid_line(self) -> None:
        stream = _FakeTty()
        progress = Progress(stream)
        progress.exported()
        progress.note("Indexed 2 album(s).")

        assert "Indexed 2 album(s).\n" in stream.getvalue()


class TestNonTerminal:
    """Under cron the stream is a file: periodic lines, never carriage returns."""

    def test_emits_periodic_lines_without_carriage_returns(self) -> None:
        stream = io.StringIO()
        with Progress(stream) as progress:
            for _ in range(LOG_EVERY):
                progress.exported()

        output = stream.getvalue()
        assert "\r" not in output
        assert f"{LOG_EVERY:,} exported" in output

    def test_stays_quiet_below_the_logging_interval(self) -> None:
        stream = io.StringIO()
        progress = Progress(stream)
        for _ in range(LOG_EVERY - 1):
            progress.exported()

        assert stream.getvalue() == ""  # no spam for a small run, until close()


class TestDisabled:
    def test_writes_nothing_at_all(self) -> None:
        stream = _FakeTty()
        with Progress(stream, enabled=False) as progress:
            progress.exported()
            progress.note("hello")

        assert stream.getvalue() == ""


class TestCloseIsIdempotent:
    """run_export closes the tracker, then the context manager closes it again."""

    def test_double_close_ends_the_line_exactly_once(self) -> None:
        stream = _FakeTty()
        with Progress(stream) as progress:
            progress.exported()
            progress.close()
            progress.close()

        # Repainting the same text is fine on a TTY; a second line-break is not.
        assert stream.getvalue().count("\n") == 1
