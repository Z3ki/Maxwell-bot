"""Trusted, standalone inspection of an untrusted repository checkout.

Run ``INSPECTION_SCRIPT`` using ``python3 -I -c`` in a fresh, immutable Docker
image with only the target checkout mounted read-only and networking disabled.
Arguments are ``operation worktree [ref]``. It imports only the standard library;
never import or execute Python modules from the mounted project.
"""

from textwrap import dedent


INSPECTION_SCRIPT = dedent(
    r"""
    import os
    import re
    import selectors
    import stat
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    MAX_METADATA_BYTES = 64 * 1024 * 1024
    MAX_METADATA_FILES = 10000
    MAX_OUTPUT_BYTES = 120000
    COMMAND_TIMEOUT = 90
    REF_PATTERN = re.compile(r"^[A-Za-z0-9._/@+-]{1,180}$")
    OPERATIONS = {"status", "diff", "verify", "checkout_head"}

    def validate_ref(value):
        if value and (
            not REF_PATTERN.fullmatch(value)
            or value.startswith(("-", "/", "."))
            or ".." in value or "@{" in value or "//" in value
            or value.endswith(("/", ".", ".lock"))
        ):
            raise ValueError("invalid git ref")
        return value

    def command(argv, cwd, env):
        # Drain both pipes even after the capture limit, so a large diff cannot
        # deadlock or grow the inspector's memory without bound.
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        deadline = time.monotonic() + COMMAND_TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("repository inspection timed out")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        kind = key.data
                        room = MAX_OUTPUT_BYTES - len(buffers[kind])
                        buffers[kind].extend(chunk[:room])
                        if len(chunk) > room:
                            truncated[kind] = True
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        text = {}
        for kind in buffers:
            text[kind] = buffers[kind].decode("utf-8", errors="replace")
            if truncated[kind]:
                text[kind] += "\n[inspection output truncated]\n"
        return code, text["stdout"], text["stderr"]

    def copy_file(source, destination, budget):
        # O_NOFOLLOW and O_NONBLOCK reject symlinks and avoid blocking on FIFOs.
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("unsupported non-regular Git metadata")
            if info.st_size > MAX_METADATA_BYTES - budget[0]:
                raise ValueError("Git inspection metadata exceeds 64 MiB")
            budget[1] += 1
            if budget[1] > MAX_METADATA_FILES:
                raise ValueError("Git inspection metadata exceeds 10000 files")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    budget[0] += len(chunk)
                    if budget[0] > MAX_METADATA_BYTES:
                        raise ValueError("Git inspection metadata exceeds 64 MiB")
                    target.write(chunk)

    def copy_refs(source, destination, budget):
        if not source.exists() and not source.is_symlink():
            return
        if source.is_symlink() or not source.is_dir():
            raise ValueError("unsupported symbolic Git refs directory")
        for base, directories, files, descriptor in os.fwalk(source, follow_symlinks=False):
            relative = Path(base).relative_to(source)
            if len(relative.parts) > 64:
                raise ValueError("Git refs exceed inspection depth limit")
            for name in directories:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("unsupported symbolic Git refs directory")
                budget[1] += 1
                if budget[1] > MAX_METADATA_FILES:
                    raise ValueError("Git inspection metadata exceeds 10000 entries")
            for name in files:
                # fwalk supplies a stable open parent. Access the leaf via that
                # descriptor so a concurrent rename cannot redirect the copy.
                proc_path = Path("/proc/self/fd") / str(descriptor) / name
                copy_file(proc_path, destination / relative / name, budget)

    def prepare_gitdir(worktree, scratch, env):
        source = worktree / ".git"
        if source.is_symlink() or not source.is_dir():
            raise ValueError("inspection requires a normal .git directory; linked worktrees are unsupported")
        if (source / "commondir").exists():
            raise ValueError("linked Git directories are unsupported for isolated inspection")
        objects = source / "objects"
        if objects.is_symlink() or not objects.is_dir():
            raise ValueError("unsupported Git objects directory")
        target = scratch / "git"
        target.mkdir()
        budget = [0, 0]
        source_config = source / "config"
        object_format = "sha1"
        if source_config.exists() or source_config.is_symlink():
            # Probe only repository format, never includes or executable
            # settings. The source config is never placed in the active Gitdir.
            probe = scratch / "format-config"
            copy_file(source_config, probe, budget)
            code, output, _error = command(
                ["/usr/bin/git", "config", "--no-includes", "--file", str(probe),
                 "--get-regexp", r"^extensions\.(objectformat|refstorage)$"],
                scratch, env,
            )
            if code not in (0, 1):
                raise ValueError("cannot read Git repository format")
            for line in output.splitlines():
                key, _separator, value = line.partition(" ")
                value = value.strip().lower()
                if key == "extensions.objectformat":
                    if value not in {"sha1", "sha256"}:
                        raise ValueError("unsupported Git object format")
                    object_format = value
                if key == "extensions.refstorage" and value != "files":
                    raise ValueError("unsupported Git reference storage for inspection")
        config = (
            "[core]\n"
            "repositoryformatversion = " + ("1" if object_format == "sha256" else "0") + "\n"
            "bare = false\nfsmonitor = false\nhooksPath = /dev/null\n"
            "attributesFile = /dev/null\n"
            "[submodule]\nrecurse = false\n"
            "[protocol]\nallow = never\n"
        )
        if object_format == "sha256":
            config += "[extensions]\nobjectformat = sha256\n"
        (target / "config").write_text(config, encoding="utf-8")
        for name in ("HEAD", "index", "packed-refs", "shallow"):
            path = source / name
            if path.exists() or path.is_symlink():
                copy_file(path, target / name, budget)
        for path in source.glob("sharedindex.*"):
            if re.fullmatch(r"sharedindex\.[0-9a-f]{40,64}", path.name):
                copy_file(path, target / path.name, budget)
        if not (target / "HEAD").exists():
            raise ValueError("Git checkout has no HEAD metadata")
        copy_refs(source / "refs", target / "refs", budget)
        (target / "refs").mkdir(exist_ok=True)
        # Object data is only read, never copied. Source config, hooks, info,
        # reflogs, modules, and worktree configuration are not imported.
        (target / "objects").symlink_to(objects, target_is_directory=True)
        return target

    def render(result, label=None):
        code, output, error = result
        if label:
            print("[" + label + "]")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        if error:
            print("[stderr]")
            print(error, end="" if error.endswith("\n") else "\n")
        return code

    def inspect(operation, worktree, ref, scratch):
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(scratch), "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_PAGER": "cat",
        }
        gitdir = prepare_gitdir(worktree, scratch, env)
        argv = ["/usr/bin/git", "--no-pager", "--git-dir=" + str(gitdir),
                "--work-tree=" + str(worktree)]
        def git(*arguments):
            return command(argv + list(arguments), worktree, env)
        status_arguments = ["status", "--short", "--branch", "--ignore-submodules=all"]
        diff_arguments = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        if operation == "status":
            return render(git(*status_arguments))
        if operation == "diff":
            return render(git(*(diff_arguments + ["--minimal"] + ([ref] if ref else []) + ["--"])))
        if operation == "checkout_head":
            branch = git("branch", "--show-current")
            head = git("rev-parse", "--verify", "HEAD")
            print("branch=" + branch[1].strip())
            print("head=" + head[1].strip())
            return render(git(*status_arguments)) or branch[0] or head[0]
        status_code = render(git(*status_arguments), "status")
        check_code = render(git(*(diff_arguments + ["--check", "--"])), "diff-check")
        scan = command(
            ["/usr/bin/rg", "--no-config", "--no-follow", "-n", "--hidden",
             "-g", "!.git", "-g", "!node_modules", "-g", "!vendor", "-e",
             r"(TODO|FIXME|NotImplementedError|raise NotImplemented|pass\s*(#.*)?$|placeholder)",
             "--", "."], worktree, env,
        )
        scan_code = render(scan, "placeholder scan")
        return status_code or check_code or (scan_code if scan_code != 1 else 0)

    def main():
        if len(sys.argv) not in (3, 4) or sys.argv[1] not in OPERATIONS:
            raise ValueError("usage: inspection operation worktree [ref]")
        operation = sys.argv[1]
        ref = validate_ref(sys.argv[3] if len(sys.argv) == 4 else "")
        if operation != "diff" and ref:
            raise ValueError("only diff accepts a ref")
        worktree = Path(sys.argv[2]).resolve(strict=True)
        if not worktree.is_dir():
            raise ValueError("checkout directory does not exist")
        with tempfile.TemporaryDirectory(prefix="maxwell-inspect-") as temporary:
            return inspect(operation, worktree, ref, Path(temporary))

    try:
        sys.exit(main())
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        sys.exit(2)
    """
).strip()
