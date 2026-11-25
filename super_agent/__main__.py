"""Module entrypoint for `python -m super_agent`."""

from __future__ import annotations

import sys

from super_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
