import pytest
from storage import resolve_upload


def test_parent_folder_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_upload(tmp_path, "../secret.txt")
