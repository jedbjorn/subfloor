"""Acquire the controller-owned terminal before exec of the captured native CLI."""
from __future__ import annotations

import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(2)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
