#!/usr/bin/env python3
"""`make smoke-gpu PLUGIN=<adapter>`: the smoke run of one adapter.

Options: `ce_worker.validation_cli`; the flow: docs/GPU_VALIDATION.md.
"""

import sys

from ce_worker.validation_cli import main

if __name__ == "__main__":
    sys.exit(main("smoke"))
