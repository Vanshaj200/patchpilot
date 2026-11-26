import subprocess

from network import ping


def test_host_is_one_argument_and_no_shell(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ping("a; echo b")
    command, options = calls[0]
    assert not options.get("shell")
    assert "a; echo b" in command
