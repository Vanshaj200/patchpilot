import builtins

from loader import read_names


def test_file_is_closed(tmp_path, monkeypatch):
    (tmp_path / "names.txt").write_text("ann\n")
    opened = []
    real_open = builtins.open

    def tracking_open(*args, **kwargs):
        opened.append(real_open(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(builtins, "open", tracking_open)
    read_names(str(tmp_path / "names.txt"))
    assert all(handle.closed for handle in opened)
