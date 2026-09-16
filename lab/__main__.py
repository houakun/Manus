#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""让 `python -m lab ...` 可用。"""

import sys

from lab.cli import main

if __name__ == "__main__":
    sys.exit(main())
