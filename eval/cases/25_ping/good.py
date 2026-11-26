import subprocess


def ping(host: str) -> int:
    """Ping a host once and return the exit code."""
    return subprocess.run(["ping", "-c", "1", host], check=False).returncode
