"""Atomic replacement with bounded recovery from Windows sharing locks."""

from __future__ import annotations

import os
import time
from pathlib import Path


def replace_atomic(temporary: Path, destination: Path) -> None:
    """Never delete the destination or fall back to a non-atomic overwrite.

    OneDrive/Windows bind mounts can briefly deny replacing an open file. Retry
    only permission/sharing failures for 3.1 seconds, then propagate the error.
    Keep a failed temporary file for diagnosis; the last good file stays intact.
    """
    waits = (0.1, 0.2, 0.4, 0.8, 1.6)
    for attempt in range(len(waits) + 1):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == len(waits):
                raise
            time.sleep(waits[attempt])
