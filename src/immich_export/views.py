"""Atomically rebuilt album/people symlink views over current verified state."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .errors import OutputError
from .layout import sanitize_component


def _validate_owned_view(view_root: Path) -> None:
    if not view_root.exists() and not view_root.is_symlink():
        return
    if view_root.is_symlink() or not view_root.is_dir():
        raise OutputError(f"View path {view_root} is not a managed directory.")
    try:
        for entry in view_root.rglob("*"):
            if entry.is_symlink() or entry.is_dir():
                continue
            raise OutputError(
                f"View {view_root} contains unexpected regular file {entry}; "
                "move it out before rebuilding the managed view."
            )
    except OSError as exc:
        raise OutputError(f"Cannot inspect managed view {view_root}: {exc}") from exc


def _remove_managed_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise OutputError(f"Cannot remove staged managed view {path}: {exc}") from exc


def build_view(
    view_root: Path,
    groups: dict[str, list[Path]],
    *,
    warnings: list[str] | None = None,
) -> int:
    """Build a complete view off to the side and swap it into place.

    A regular file under an existing managed view is never deleted or ignored:
    it is an output ownership conflict and aborts publication.
    """
    del warnings  # retained for source compatibility; view failures are no longer warnings
    _validate_owned_view(view_root)
    stage: Path | None = None
    backup: Path | None = None
    links = 0
    try:
        view_root.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{view_root.name}.stage-", dir=view_root.parent))
        for group_name, targets in sorted(groups.items()):
            group_dir = stage / sanitize_component(group_name)
            group_dir.mkdir(parents=True, exist_ok=True)
            used: set[str] = set()
            for target in sorted(set(targets)):
                if not target.is_file():
                    raise OutputError(
                        f"Cannot publish {view_root.name} view: target {target} is not verified."
                    )
                name = target.name
                suffix = 1
                while name in used:
                    name = f"{target.stem}-{suffix}{target.suffix}"
                    suffix += 1
                used.add(name)
                link = group_dir / name
                # stage and final roots are siblings at the same depth, so this
                # relative target remains valid after the directory rename.
                relative_target = os.path.relpath(target, group_dir)
                link.symlink_to(relative_target)
                links += 1

        if view_root.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{view_root.name}.previous-", dir=view_root.parent)
            )
            backup.rmdir()
            view_root.replace(backup)
        try:
            stage.replace(view_root)
            stage = None
        except OSError:
            if backup is not None and backup.exists() and not view_root.exists():
                backup.replace(view_root)
                backup = None
            raise
        if backup is not None:
            _remove_managed_tree(backup)
            backup = None
        return links
    except OutputError:
        raise
    except OSError as exc:
        raise OutputError(f"Cannot publish managed view {view_root}: {exc}") from exc
    finally:
        if stage is not None:
            _remove_managed_tree(stage)
        if backup is not None and backup.exists() and not view_root.exists():
            try:
                backup.replace(view_root)
                backup = None
            except OSError as exc:
                raise OutputError(
                    f"Cannot restore prior managed view {view_root} from {backup}: {exc}"
                ) from exc
