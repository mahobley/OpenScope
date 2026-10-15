from __future__ import annotations

import os
from pathlib import Path


def drop_invoking_user_privileges() -> None:
    """Keep the already-open capture socket, but run saves as the desktop user."""
    ids = invoking_user_ids()
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or ids is None:
        return
    uid, gid = ids
    if uid == 0:
        return
    import pwd
    user = pwd.getpwuid(uid)
    os.initgroups(user.pw_name, gid)
    os.setgid(gid)
    os.setuid(uid)
    # Do not keep sudo's private temp directory in the user process.
    import tempfile
    temporary = os.confstr("CS_DARWIN_USER_TEMP_DIR") if "CS_DARWIN_USER_TEMP_DIR" in os.confstr_names else "/tmp"
    os.environ["TMPDIR"] = temporary or "/tmp"
    tempfile.tempdir = None
    for name in ("SUDO_UID", "SUDO_GID", "SUDO_USER"):
        os.environ.pop(name, None)


def invoking_user_ids() -> tuple[int, int] | None:
    """Return the desktop user behind sudo, when this process was elevated."""
    try:
        uid = int(os.environ["SUDO_UID"])
        gid = int(os.environ["SUDO_GID"])
    except (KeyError, TypeError, ValueError):
        return None
    return (uid, gid) if uid >= 0 and gid >= 0 else None


def make_user_editable(path: Path) -> None:
    """Give a sudo-invoking user ownership of an output created for them."""
    ids = invoking_user_ids()
    if ids is None:
        return
    try:
        os.chown(path, *ids, follow_symlinks=False)
        mode = os.stat(path, follow_symlinks=False).st_mode
        path.chmod(mode | (0o700 if path.is_dir() else 0o600))
    except OSError:
        # Output creation must not fail solely because ownership normalization
        # is unavailable on a platform or filesystem.
        pass


def make_output_tree_user_editable(root: Path) -> None:
    """Normalize an existing recording tree without following symlinks."""
    if not root.exists():
        return
    make_user_editable(root)
    if not root.is_dir() or root.is_symlink():
        return
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        make_user_editable(base)
        for name in (*names, *files):
            candidate = base / name
            if not candidate.is_symlink():
                make_user_editable(candidate)
