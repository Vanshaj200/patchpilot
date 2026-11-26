from importlib.metadata import version

import patchpilot


def test_version_matches_installed_metadata() -> None:
    assert patchpilot.__version__ == version("patchpilot")
