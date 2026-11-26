import subprocess


def ping(host: str) -> int:
    """Ping a host once and return the exit code."""
    return subprocess.run(f"ping -c 1 {host}", shell=True, check=False).returncode
