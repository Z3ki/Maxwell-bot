"""Root-only ExecStartPre: consume a boot-local activation exactly once."""

from __future__ import annotations

import json
import os
from pathlib import Path

from standby.policy import PolicyDenied, activation_record


def consume(directory: Path) -> None:
    if os.geteuid() != 0:
        raise PolicyDenied(
            "Only the service's privileged pre-start may consume activation"
        )
    source = directory / "activation.json"
    record = activation_record(source)
    # Exclusive owner marker = single use: a second start fails here until the
    # ordered stepdown/failback removes it. The source receipt must stay in place
    # while the unit runs: systemd 257 re-reads LoadCredential sources for every
    # Exec* command, so unlinking it here made the main process fail at the
    # CREDENTIALS step. Stepdown/failback remove both files.
    fd = os.open(directory / "owner.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    consume(Path("/run/maxwell-standby"))
