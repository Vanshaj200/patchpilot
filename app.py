"""Entry point for Vercel, which looks for an `app` object here: the hosted web page."""

import sys
from pathlib import Path

# Import the package from the source folder, whether or not the host installs the project.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from patchpilot.web import create_app  # noqa: E402

app = create_app(hosted=True)
