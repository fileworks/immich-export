"""Album/people symlink views over the primary tree.

Views are derived artifacts: they are rebuilt from scratch each run. Cleanup
only ever removes *symlinks* (and then-empty directories) — a regular file
that somehow ended up inside a view directory is left alone and reported.
"""

from __future__ import annotations

import os
from pathlib import Path

from .layout import sanitize_component


def _clear_view(view_root: Path, warnings: list[str]) -> None:
    if not view_root.exists():
        return
    for entry in sorted(view_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            try:
                entry.rmdir()
            except OSError:
                warnings.append(f"view cleanup: {entry} not empty, left in place")
        else:
            warnings.append(f"view cleanup: {entry} is a regular file, left in place")


def build_view(
    view_root: Path,
    groups: dict[str, list[Path]],
    *,
    warnings: list[str],
) -> int:
    """Create `<view_root>/<group>/<name> -> relative target` links.

    `groups` maps a group name (album title, person name) to media file paths;
    targets must live under the same export root so links stay relative and the
    tree stays portable. Returns the number of links created.
    """
    _clear_view(view_root, warnings)
    links = 0
    for group_name, targets in sorted(groups.items()):
        group_dir = view_root / sanitize_component(group_name)
        group_dir.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for target in sorted(targets):
            name = target.name
            if name in used:
                name = f"{target.stem}-{links}{target.suffix}"
            used.add(name)
            link = group_dir / name
            relative_target = os.path.relpath(target, group_dir)
            try:
                link.symlink_to(relative_target)
                links += 1
            except OSError as exc:
                warnings.append(f"could not link {link}: {exc}")
    return links
