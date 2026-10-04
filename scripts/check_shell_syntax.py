"""Check every tracked shell script, including deployment helpers."""

from pathlib import Path
import subprocess


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    files = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.sh"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    paths = [path.decode("utf-8") for path in files if path]
    for path in paths:
        subprocess.run(["bash", "-n", path], cwd=root, check=True)
    print(f"Shell syntax passed for {len(paths)} tracked scripts.")


if __name__ == "__main__":
    main()
