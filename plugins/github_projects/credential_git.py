"""Standalone trusted Git executor; the workspace never receives credentials.

Run with python3 -I in a fresh container. Only ordinary object/ref/index data
crosses into its private Git directory; workspace Git configuration is data, not
configuration for the commands. A JSON request (including the credential) arrives
on stdin, never Docker arguments or environment. No workspace program is run.
"""
from textwrap import dedent


def validate_git_commands(commands: list[list[str]]) -> None:
    """Accept only the service's structured workflows, never shell/Git options."""
    import re

    ref_pattern = re.compile(r"^[A-Za-z0-9._/@+-]{1,180}$")

    def ref(value: str) -> bool:
        return bool(
            ref_pattern.fullmatch(value)
            and not value.startswith(("-", "/", "."))
            and not value.endswith(("/", ".", ".lock"))
            and not any(part in value for part in ("..", "@{", "//"))
        )

    if not isinstance(commands, list) or not commands or len(commands) > 8:
        raise ValueError("Git commands must be a nonempty structured command list")
    for command in commands:
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise ValueError("Git commands must contain string argument lists")
        if any("\0" in arg for arg in command):
            raise ValueError("invalid Git argument")
        accepted = command in (
            ["add", "-A"], ["diff", "--cached", "--check"],
            ["status", "--short", "--branch"], ["fetch", "--prune", "origin"],
        )
        if len(command) == 3 and command[:2] == ["commit", "-m"]:
            accepted = bool(command[2].strip())
        if len(command) == 3 and command[:2] == ["push", "origin"]:
            accepted = ref(command[2])
        if len(command) == 2 and command[0] == "checkout":
            accepted = ref(command[1])
        if len(command) == 4 and command[:2] == ["checkout", "-B"]:
            accepted = ref(command[2]) and ref(command[3])
        if len(command) == 3 and command[:2] == ["fetch", "origin"]:
            match = re.fullmatch(
                r"\+?pull/([1-9][0-9]*)/head:refs/remotes/origin/pr/([1-9][0-9]*)", command[2]
            )
            # Force is confined to the matching PR tracking ref, never branches.
            accepted = bool(match and match[1] == match[2])
        if not accepted:
            raise ValueError("unsupported Git command; use the structured repository workflows")


# Include the same validator in the isolated interpreter without importing any
# project module (the only mount is the untrusted repository).
import inspect

GIT_SCRIPT = inspect.getsource(validate_git_commands) + "\n" + dedent(
    r'''
    import base64
    import errno
    import fcntl
    import json
    import os
    import re
    import selectors
    import shutil
    import stat
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    MAX_BYTES = 4 * 1024 * 1024 * 1024
    MAX_ENTRIES = 250000
    DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    budget = [0, 0]
    original_objects = {}
    object_cache = None
    cache_signatures = {}
    used_cache = {}

    def signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def account(size=0):
        budget[0] += size
        budget[1] += 1
        if budget[0] > MAX_BYTES or budget[1] > MAX_ENTRIES:
            raise ValueError("Git snapshot exceeds 4 GiB or 250000 entries")

    def command(argv, cwd, env):
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        captured = [bytearray(), bytearray()]
        deadline = time.monotonic() + 1500
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 0)
                selector.register(process.stderr, selectors.EVENT_READ, 1)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Git command timed out")
                    for key, _ in selector.select(0.25):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output = captured[key.data]
                        room = 120000 - len(output)
                        output.extend(chunk[:max(0, room)])
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        return code, *(bytes(value).decode("utf-8", errors="replace") for value in captured)

    def copy_file(source_fd, name, destination):
        descriptor = os.open(name, FILE_FLAGS, dir_fd=source_fd)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("unsupported non-regular Git metadata")
            account(info.st_size)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                remaining = info.st_size
                while remaining:
                    chunk = source.read(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("Git metadata changed while copying")
                    target.write(chunk)
                    remaining -= len(chunk)

    def snapshot_object(source_fd, name, destination):
        """Share immutable objects or reflink them into private disk storage.

        Only sanitized object filenames enter this tree. Open and verify the
        source without following symlinks, including after the hardlink step.
        Credential files and executable Git configuration are never shared.
        """
        descriptor = os.open(name, FILE_FLAGS, dir_fd=source_fd)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("unsupported non-regular Git object")
            account(info.st_size)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(name, destination, src_dir_fd=source_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}:
                    raise
                # Separate Docker mounts cannot hardlink directly. Keep a
                # private disk cache so unchanged packs are copied at most
                # once even on filesystems without reflink support.
                parts = destination.parts
                key = "/".join(parts[parts.index("objects") + 1:])
                cached = object_cache / key if object_cache is not None else None
                record = cache_signatures.get(key)
                if cached is not None:
                    try:
                        cached_info = cached.lstat()
                    except FileNotFoundError:
                        cached_info = None
                    if (cached_info is not None and stat.S_ISREG(cached_info.st_mode)
                            and record == [list(signature(info)), list(signature(cached_info))]):
                        os.link(cached, destination, follow_symlinks=False)
                        used_cache[key] = record
                        original_objects[str(destination)] = (signature(info), signature(destination.stat()))
                        return
                # Cross-filesystem storage can still avoid data copies via
                # FICLONE. A streaming disk copy is the portable last resort.
                with destination.open("xb") as target:
                    try:
                        fcntl.ioctl(target.fileno(), 0x40049409, source.fileno())
                    except OSError as clone_error:
                        if clone_error.errno not in {errno.EXDEV, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTTY, errno.ENOSYS}:
                            raise
                        remaining = info.st_size
                        while remaining:
                            chunk = source.read(min(remaining, 65536))
                            if not chunk:
                                raise ValueError("Git object changed while copying")
                            target.write(chunk)
                            remaining -= len(chunk)
                if cached is not None:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cached.with_name(cached.name + ".new")
                    temporary.unlink(missing_ok=True)
                    os.link(destination, temporary, follow_symlinks=False)
                    os.replace(temporary, cached)
                    used_cache[key] = [list(signature(info)), list(signature(cached.stat()))]
            else:
                linked = destination.lstat()
                if not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                    destination.unlink()
                    raise ValueError("Git object changed while linking")
            original_objects[str(destination)] = (signature(info), signature(destination.stat()))

    def check_object_budget(gitdir):
        size = entries = 0
        for parent, directories, files in os.walk(gitdir / "objects", followlinks=False):
            entries += len(directories) + len(files)
            for name in files:
                info = (Path(parent) / name).lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("unsupported symbolic or special Git object")
                size += info.st_size
            if size > MAX_BYTES or entries > MAX_ENTRIES:
                raise ValueError("Git object store exceeds 4 GiB or 250000 entries")

    def copy_tree(source_fd, destination, *, objects=False, depth=0):
        if depth > 64:
            raise ValueError("Git metadata exceeds depth limit")
        destination.mkdir(parents=True, exist_ok=True)
        for name in os.listdir(source_fd):
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if objects:
                # No alternates, info/grafts, commit graphs or other paths that
                # can redirect object reads back into the writable checkout.
                if depth == 0 and name not in {"pack"} and not re.fullmatch(r"[0-9a-f]{2}", name):
                    continue
                if depth == 1:
                    parent = destination.name
                    if parent == "pack":
                        if not re.fullmatch(r"pack-[0-9a-f]{40,64}\.(pack|idx|rev|promisor)", name):
                            continue
                    elif not re.fullmatch(r"[0-9a-f]{38}|[0-9a-f]{62}", name):
                        continue
            if stat.S_ISDIR(info.st_mode):
                if objects and depth != 0:
                    raise ValueError("unsupported Git object directory")
                account()
                descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=source_fd)
                try:
                    copy_tree(descriptor, destination / name, objects=objects, depth=depth + 1)
                finally:
                    os.close(descriptor)
            elif stat.S_ISREG(info.st_mode):
                if objects:
                    snapshot_object(source_fd, name, destination / name)
                else:
                    copy_file(source_fd, name, destination / name)
            else:
                raise ValueError("unsupported symbolic or special Git metadata")

    def has_file(directory, name):
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def safe_config(gitdir, scratch, env, remote, source_fd=None):
        object_format = "sha1"
        identity = {"user.name": "Maxwell", "user.email": "maxwell@users.noreply.github.com"}
        if source_fd is not None and has_file(source_fd, "config"):
            probe = scratch / "source-config"
            copy_file(source_fd, "config", probe)
            for key in ("extensions.objectformat", "extensions.refstorage", "user.name", "user.email"):
                code, output, error = command(
                    ["/usr/bin/git", "config", "--no-includes", "--file", str(probe), "--get", key],
                    scratch, env,
                )
                if code not in (0, 1):
                    raise ValueError("cannot read Git configuration as data")
                value = output.rstrip("\n")
                if key == "extensions.objectformat" and code == 0:
                    if value not in {"sha1", "sha256"}:
                        raise ValueError("unsupported Git object format")
                    object_format = value
                elif key == "extensions.refstorage" and code == 0 and value != "files":
                    raise ValueError("unsupported Git reference storage")
                elif key in identity and code == 0 and value.strip():
                    identity[key] = value
        config = gitdir / "config"
        config.write_text(
            "[core]\nrepositoryformatversion = " + ("1" if object_format == "sha256" else "0") +
            "\nbare = false\nfsmonitor = false\nhooksPath = /dev/null\nattributesFile = /dev/null\n" +
            "[submodule]\nrecurse = false\n[protocol]\nallow = never\n" +
            "[protocol \"https\"]\nallow = always\n" +
            "[credential]\nhelper =\n[commit]\ngpgSign = false\n" +
            "[gc]\nauto = 0\n[maintenance]\nauto = false\n" +
            "[http]\nfollowRedirects = false\n" +
            ("[extensions]\nobjectformat = sha256\n" if object_format == "sha256" else ""),
            encoding="utf-8",
        )
        values = dict(identity, **{
            "remote.origin.url": remote,
            "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
        })
        for key, value in values.items():
            code, _output, _error = command(
                ["/usr/bin/git", "config", "--file", str(config), "--", key, value], scratch, env
            )
            if code:
                raise ValueError("cannot create trusted Git configuration")

    def snapshot(worktree_fd, scratch, env, remote):
        source_fd = os.open(".git", DIRECTORY_FLAGS, dir_fd=worktree_fd)
        try:
            if has_file(source_fd, "commondir"):
                raise ValueError("linked Git directories are unsupported")
            gitdir = scratch / "git"
            gitdir.mkdir()
            safe_config(gitdir, scratch, env, remote, source_fd)
            for name in os.listdir(source_fd):
                if name in {"HEAD", "index", "packed-refs", "shallow"} or re.fullmatch(r"sharedindex\.[0-9a-f]{40,64}", name):
                    copy_file(source_fd, name, gitdir / name)
            if not (gitdir / "HEAD").is_file():
                raise ValueError("Git checkout has no HEAD")
            for name in ("refs", "objects"):
                descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=source_fd)
                try:
                    copy_tree(descriptor, gitdir / name, objects=name == "objects")
                finally:
                    os.close(descriptor)
            return gitdir
        finally:
            os.close(source_fd)

    def publish_file(source, destination_fd, name):
        # Atomic leaf replacement, relative to a stable no-follow parent. A
        # concurrent workspace symlink cannot redirect writes or read scratch.
        descriptor, temporary = tempfile.mkstemp(prefix=".maxwell-", dir=f"/proc/self/fd/{destination_fd}")
        temporary = Path(temporary).name
        try:
            with os.fdopen(descriptor, "wb") as target, source.open("rb") as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    target.write(chunk)
            os.replace(temporary, name, src_dir_fd=destination_fd, dst_dir_fd=destination_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=destination_fd)
            except FileNotFoundError:
                pass

    def open_destination(parent_fd, name):
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)

    def publish_tree(source, destination_fd, *, exact=False):
        names = set(os.listdir(source))
        if exact:
            for name in os.listdir(destination_fd):
                info = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
                if name not in names and not stat.S_ISDIR(info.st_mode):
                    os.unlink(name, dir_fd=destination_fd)
        for name in names:
            path = source / name
            if path.is_dir():
                descriptor = open_destination(destination_fd, name)
                try:
                    publish_tree(path, descriptor, exact=exact)
                finally:
                    os.close(descriptor)
            else:
                original = original_objects.get(str(path))
                if original and signature(path.stat()) == original[1]:
                    try:
                        unchanged = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        if stat.S_ISREG(unchanged.st_mode) and signature(unchanged) == original[0]:
                            continue
                publish_file(path, destination_fd, name)
        # Pruned ref directories can remain empty, but must contain no stale
        # refs. Objects are append-only and are never pruned by this executor.
        if exact:
            for name in os.listdir(destination_fd):
                if name not in names:
                    info = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=destination_fd)
                        try:
                            clear_refs(descriptor)
                        finally:
                            os.close(descriptor)

    def clear_refs(directory_fd):
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    clear_refs(descriptor)
                finally:
                    os.close(descriptor)
            else:
                os.unlink(name, dir_fd=directory_fd)

    def publish(gitdir, worktree_fd, fresh):
        destination_fd = open_destination(worktree_fd, ".git")
        try:
            for name in ("objects", "refs"):
                descriptor = open_destination(destination_fd, name)
                try:
                    publish_tree(gitdir / name, descriptor, exact=name == "refs")
                finally:
                    os.close(descriptor)
            for name in ("HEAD", "index", "packed-refs", "shallow"):
                if (gitdir / name).is_file():
                    publish_file(gitdir / name, destination_fd, name)
                elif has_file(destination_fd, name):
                    os.unlink(name, dir_fd=destination_fd)
            for path in gitdir.glob("sharedindex.*"):
                publish_file(path, destination_fd, path.name)
            if fresh:
                # This file has no authorization header; network credentials
                # live exclusively in a separate private configuration file.
                publish_file(gitdir / "config", destination_fd, "config")
        finally:
            os.close(destination_fd)

    def execute(request, worktree, scratch, credentials):
        commands = request["commands"]
        validate_git_commands(commands)
        repo = request["repo"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo):
            raise ValueError("invalid repository")
        remote = "https://github.com/" + repo + ".git"
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(scratch), "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
        }
        worktree_fd = os.open(worktree, DIRECTORY_FLAGS)
        try:
            fresh = not has_file(worktree_fd, ".git")
            if fresh:
                if not request.get("checkout"):
                    raise ValueError("repo is not checked out yet")
                if os.listdir(worktree_fd):
                    raise ValueError("cannot clone into a nonempty workspace")
            else:
                gitdir = snapshot(worktree_fd, scratch, env, remote)
            auth = credentials / "network-config"
            token = request["token"]
            if not token or any(char in token for char in "\r\n\0"):
                raise ValueError("invalid GitHub credential")
            # Git quotes special config characters; the token is never a shell
            # argument, environment value, remote URL, or published metadata.
            encoded = base64.b64encode(("x-access-token:" + token).encode()).decode("ascii")
            header = json.dumps("AUTHORIZATION: Basic " + encoded)
            auth.write_text(
                '[http "https://github.com/"]\nextraHeader = ' + header + '\nfollowRedirects = false\n',
                encoding="utf-8",
            )
            auth.chmod(0o600)
            network_env = dict(env, GIT_CONFIG_GLOBAL=str(auth))
            if fresh:
                clone = scratch / "clone"
                result = command([
                    "/usr/bin/git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                    "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
                    "clone", "--no-checkout", "--template=", "--", remote, str(clone),
                ], scratch, network_env)
                if result[0]:
                    return result
                gitdir = clone / ".git"
                safe_config(gitdir, scratch, env, remote)
            argv = ["/usr/bin/git", "--no-pager", "--git-dir=" + str(gitdir), "--work-tree=" + str(worktree)]
            # Restore ordinary origin tracking without importing branch config.
            code, branch, _error = command(argv + ["symbolic-ref", "--quiet", "--short", "HEAD"], scratch, env)
            if code == 0:
                branch = branch.strip()
                for key, value in (("remote", "origin"), ("merge", "refs/heads/" + branch)):
                    command(["/usr/bin/git", "config", "--file", str(gitdir / "config"), "--",
                             "branch." + branch + "." + key, value], scratch, env)
            if fresh:
                commands = [["checkout", "-f", "HEAD"]] + commands
            output, error, code = "", "", 0
            for arguments in commands:
                actual = list(arguments)
                if actual[0] in {"checkout", "status", "diff", "push", "fetch"}:
                    actual.insert(1, "--recurse-submodules=no" if actual[0] in {"push", "fetch"} else "--ignore-submodules=all" if actual[0] in {"status", "diff"} else "--no-recurse-submodules")
                if actual[0] == "diff":
                    actual[1:1] = ["--no-ext-diff", "--no-textconv"]
                if actual[0] == "commit":
                    actual.insert(1, "--no-gpg-sign")
                code, out, err = command(argv + actual, worktree, network_env if actual[0] in {"fetch", "push"} else env)
                output = (output + out)[:120000]
                error = (error + err)[:120000]
                if code:
                    break
            check_object_budget(gitdir)
            publish(gitdir, worktree_fd, fresh)
            return code, output, error
        finally:
            os.close(worktree_fd)

    def cached_execute(request, worktree, storage, credentials):
        global object_cache, cache_signatures
        if storage is None:
            with tempfile.TemporaryDirectory(prefix="maxwell-git-") as directory:
                return execute(request, worktree, Path(directory), credentials)
        cache_root = Path(storage)
        with (cache_root / "cache.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Only the lock holder creates disk snapshots. Remove remnants of
            # killed containers before starting another operation.
            for stale in cache_root.glob("maxwell-git-*"):
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
            object_cache = cache_root / "object-cache"
            object_cache.mkdir(exist_ok=True)
            manifest = cache_root / "cache.json"
            try:
                with manifest.open() as stream:
                    raw = stream.read(64 * 1024 * 1024 + 1)
                decoded = json.loads(raw) if len(raw) <= 64 * 1024 * 1024 else {}
                cache_signatures = decoded if isinstance(decoded, dict) else {}
            except (FileNotFoundError, ValueError):
                cache_signatures = {}
            with tempfile.TemporaryDirectory(prefix="maxwell-git-", dir=storage) as directory:
                result = execute(request, worktree, Path(directory), credentials)
            # Retain only this snapshot's objects: stale packs cannot grow
            # the persistent cache without bound.
            for parent, _dirs, files in os.walk(object_cache):
                for name in files:
                    path = Path(parent) / name
                    if str(path.relative_to(object_cache)) not in used_cache:
                        path.unlink()
            temporary = manifest.with_suffix(".new")
            temporary.write_text(json.dumps(used_cache), encoding="utf-8")
            os.replace(temporary, manifest)
            return result

    def main():
        request = json.load(sys.stdin)
        token = str(request.get("token", ""))
        try:
            # Private disk caches contain sanitized objects only. Authentication
            # stays on ephemeral /tmp, including when a container is killed.
            storage = sys.argv[2] if len(sys.argv) > 2 else None
            with tempfile.TemporaryDirectory(prefix="maxwell-git-auth-") as credentials:
                code, output, error = cached_execute(request, Path(sys.argv[1]), storage, Path(credentials))
        except (OSError, ValueError, TimeoutError) as exc:
            code, output, error = 2, "", str(exc) + "\n"
        if token:
            encoded = base64.b64encode(("x-access-token:" + token).encode()).decode("ascii")
            for secret in (token, encoded):
                output = output.replace(secret, "[redacted]")
                error = error.replace(secret, "[redacted]")
        sys.stdout.write(output)
        sys.stderr.write(error)
        return code

    raise SystemExit(main())
    '''
).strip()
