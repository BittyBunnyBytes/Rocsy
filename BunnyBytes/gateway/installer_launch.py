#!/usr/bin/env python3
"""Start the Rocsy installer from this extracted package: in the browser, or in the terminal (--terminal / --defaults)."""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
runtime = root / 'installer' / 'runtime.py'
default_dir = (root / 'installer' / 'default_dir.txt').read_text().strip()
# Options (--terminal, --defaults, --dir ...) go on to the runtime; see `--help`.
sys.argv = [str(runtime), str(root), default_dir, *sys.argv[1:]]
runpy.run_path(str(runtime), run_name='__main__')
