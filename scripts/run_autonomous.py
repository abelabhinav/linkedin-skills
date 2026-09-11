#!/usr/bin/env python3
"""Run the autonomous LinkedIn background daemon and WhatsApp webhook server.

Usage:
    python scripts/run_autonomous.py [--port 8080] [--interval-minutes 180]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.manage import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Default to serve
        sys.argv.append("serve")
    sys.exit(main())
