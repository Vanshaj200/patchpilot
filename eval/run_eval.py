"""Run the seeded-bug evaluation. Same as `patchpilot eval`; accepts the same options.

Run from the repository root: python eval/run_eval.py --readme README.md
"""

import sys

from patchpilot.cli import app

if __name__ == "__main__":
    app(["eval", *sys.argv[1:]])
