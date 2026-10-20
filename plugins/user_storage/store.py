"""Per-user persistent storage that outlives the shell sandbox.

The shell guest's /workspace is a tmpfs that dies with the container (idle
recycle, timeout, cancel, restart). This store is the boring durable copy on
the host: one directory tree per Discord user, a quota, a public/ folder that
can be served at /u/<handle>/, and a merge step for whatever the sandbox wrote
into its /storage mount.

Both the bot process and the API process open this. Anything that mutates a
user's tree takes that user's flock, so the dashboard and a shell sync cannot
interleave half a merge. Nothing here ever follows a symlink, extracts a tar
member by name, or trusts a path it didn't validate itself.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import io
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

MB = 1024 * 1024

# Handles are URL path segments under /u/. Keep anything that already means
# something on the public host (or looks like staff) out of the pool.
_HANDLE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?")
_RESERVED_HANDLES = frozenset(
    {
        "admin", "administrator", "api", "app", "assets", "bot", "bots", "dashboard",
        "data", "dev", "discord", "docs", "files", "guide", "help", "install",
        "login", "logout", "maxwell", "mod", "moderator", "official", "operator",
        "owner", "privacy", "root", "security", "staff", "static", "status",
        "support", "system", "terms", "u", "user", "users", "www",
    }
)
_MAX_PATH = 512
_MAX_PART = 128
PUBLIC_DIR = "public"


def _env_mb(name: str, default: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, high))


def premium_quota_bytes() -> int:
    return _env_mb("MAXWELL_STORAGE_PREMIUM_MB", 100, 1024) * MB


def free_quota_bytes() -> int:
    return _env_mb("MAXWELL_STORAGE_FREE_MB", 0, 1024) * MB


def max_files() -> int:
    try:
        value = int(os.environ.get("MAXWELL_STORAGE_MAX_FILES", "") or 2000)
    except (TypeError, ValueError):
        value = 2000
    return max(10, min(value, 20000))


def load_or_create_key(path: str | os.PathLike, size: int = 32) -> bytes:
    """A random key file shared by the bot and API processes.

    Both processes boot at once and both want the key. The naive
    create-then-write lets the loser read a zero-byte file, "repair" it, and
    walk off with a different key than the winner. Write a temp file, then
    link() it into place: the name only ever appears with the full key in it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        if len(data) >= size:
            return data[:size]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(size))
                handle.flush()
                os.fsync(handle.fileno())
            if data:
                # A short file is crash debris from the old writer; anything
                # signed with it was never verifiable. Replace it.
                os.replace(tmp, path)
            else:
                with contextlib.suppress(FileExistsError):
                    os.link(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    data = path.read_bytes()
    if len(data) < size:
        raise OSError(f"could not create key file {path}")
    return data[:size]


class StorageError(Exception):
    """A user-facing refusal. The message is safe to show the requester."""


def clean_path(raw: str, *, allow_root: bool = False) -> str:
    """Normalise a user path to 'a/b/c'. Raise on anything clever."""
    text = str(raw or "").strip().replace("\\", "/")
    for prefix in ("/storage/", "storage/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    else:
        if text in {"/storage", "storage"}:
            text = ""
    text = text.strip("/")
    if not text:
        if allow_root:
            return ""
        raise StorageError("a file path inside /storage is required")
    if len(text.encode("utf-8", "surrogatepass")) > _MAX_PATH:
        raise StorageError("path is too long")
    parts = text.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise StorageError("path may not contain empty, '.' or '..' parts")
        if len(part.encode("utf-8", "surrogatepass")) > _MAX_PART:
            raise StorageError("a path component is too long")
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise StorageError("path contains control characters")
    return "/".join(parts)


def valid_handle(handle: str) -> str:
    value = str(handle or "").strip().lower()
    if not _HANDLE_RE.fullmatch(value) or "--" in value:
        raise StorageError(
            "handles are 1-30 lowercase letters, digits or single dashes, "
            "and cannot start or end with a dash"
        )
    if value in _RESERVED_HANDLES:
        raise StorageError("that handle is reserved")
    return value


def suggest_handle(name: str, uid: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:24].strip("-")
    if not base or base in _RESERVED_HANDLES or not _HANDLE_RE.fullmatch(base):
        base = "u" + str(uid)[-6:]
    return base


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    mtime: float
    is_dir: bool = False

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": int(self.mtime),
            "type": "dir" if self.is_dir else "file",
        }


@dataclass
class MergeResult:
    written: int = 0
    deleted: int = 0
    skipped: list[str] | None = None
    clean: bool = True
    generation: int = 0
    error: str = ""
    # Post-merge host manifest; the next merge base when ``clean``.
    manifest: dict[str, str] | None = None


class UserStorage:
    """Filesystem tree + sqlite metadata. Thread- and process-safe per user."""

    def __init__(self, root: str | os.PathLike, *, plan_lookup: Callable[[str], str] | None = None):
        self.root = Path(root)
        self.users = self.root / "users"
        self.staging = self.root / "staging"
        self.locks = self.root / "locks"
        for path in (self.root, self.users, self.staging, self.locks):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "storage.sqlite3"
        self.plan_lookup = plan_lookup
        self._hash_cache: dict[tuple, str] = {}
        self._hash_lock = threading.Lock()
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "uid TEXT PRIMARY KEY, handle TEXT UNIQUE, gen INTEGER NOT NULL DEFAULT 0, "
                "updated REAL NOT NULL DEFAULT 0)"
            )
        self._key = self._load_key()

    # ---- plumbing -------------------------------------------------------

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    def _load_key(self) -> bytes:
        return load_or_create_key(self.root / "link.key")

    @staticmethod
    def _uid(uid: str) -> str:
        value = str(uid or "").strip()
        if not value.isdigit() or len(value) > 25:
            raise StorageError("storage needs a Discord user id")
        return value

    def user_dir(self, uid: str) -> Path:
        return self.users / self._uid(uid) / "files"

    @contextlib.contextmanager
    def locked(self, uid: str) -> Iterator[None]:
        uid = self._uid(uid)
        with open(self.locks / f"{uid}.lock", "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _bump(self, uid: str) -> int:
        now = time.time()
        with self._db() as db:
            db.execute(
                "INSERT INTO users(uid, gen, updated) VALUES(?, 1, ?) "
                "ON CONFLICT(uid) DO UPDATE SET gen=gen+1, updated=excluded.updated",
                (uid, now),
            )
            row = db.execute("SELECT gen FROM users WHERE uid=?", (uid,)).fetchone()
        return int(row[0])

    def generation(self, uid: str) -> int:
        uid = self._uid(uid)
        with self._db() as db:
            row = db.execute("SELECT gen FROM users WHERE uid=?", (uid,)).fetchone()
        return int(row[0]) if row else 0

    # ---- plan / quota ---------------------------------------------------

    def plan(self, uid: str) -> str:
        if self.plan_lookup is None:
            return "free"
        try:
            return "premium" if self.plan_lookup(str(uid)) == "premium" else "free"
        except Exception:
            return "free"

    def quota(self, uid: str) -> int:
        return premium_quota_bytes() if self.plan(uid) == "premium" else free_quota_bytes()

    # ---- walking --------------------------------------------------------

    def _walk(self, base: Path, rel: str = "") -> Iterator[tuple[str, os.stat_result, bool]]:
        """Yield (relpath, lstat, is_dir) without following symlinks."""
        try:
            with os.scandir(base) as it:
                items = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            return
        for item in items:
            path = f"{rel}/{item.name}" if rel else item.name
            try:
                info = item.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                yield path, info, True
                yield from self._walk(Path(item.path), path)
            elif stat.S_ISREG(info.st_mode):
                yield path, info, False
            # Symlinks, sockets, devices: never created by us, never served.

    def usage(self, uid: str) -> tuple[int, int]:
        total = files = 0
        for _path, info, is_dir in self._walk(self.user_dir(uid)):
            if not is_dir:
                total += info.st_size
                files += 1
        return total, files

    def status(self, uid: str) -> dict:
        used, files = self.usage(uid)
        quota = self.quota(uid)
        return {
            "plan": self.plan(uid),
            "quota_bytes": quota,
            "used_bytes": used,
            "files": files,
            "max_files": max_files(),
            "handle": self.handle_for(uid),
            "writable": quota > 0 and used <= quota,
        }

    def list(self, uid: str, prefix: str = "", *, limit: int = 1000) -> list[Entry]:
        prefix = clean_path(prefix, allow_root=True)
        base = self.user_dir(uid)
        start = self._resolve(base, prefix) if prefix else base
        if prefix and (start is None or not start.is_dir()):
            raise StorageError("that folder does not exist")
        out = []
        for path, info, is_dir in self._walk(start, prefix):
            out.append(Entry(path, 0 if is_dir else info.st_size, info.st_mtime, is_dir))
            if len(out) >= limit:
                break
        return out

    def _resolve(self, base: Path, rel: str) -> Path | None:
        """Walk component by component with lstat; any symlink means no."""
        current = base
        for part in rel.split("/"):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode):
                return None
        return current

    def file_path(self, uid: str, path: str) -> Path | None:
        """A regular file inside the user's tree, or None. For serving."""
        rel = clean_path(path)
        target = self._resolve(self.user_dir(uid), rel)
        if target is None:
            return None
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return None
        return target if stat.S_ISREG(info.st_mode) else None

    def read(self, uid: str, path: str, *, max_size: int = 25 * MB) -> bytes:
        target = self.file_path(uid, path)
        if target is None:
            raise StorageError("file not found in /storage")
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if info.st_size > max_size:
                raise StorageError(f"file is larger than {max_size // MB} MB")
            return handle.read(max_size + 1)[:max_size]

    # ---- writes ---------------------------------------------------------

    def _check_capacity(self, uid: str, extra_bytes: int, extra_files: int) -> None:
        quota = self.quota(uid)
        if quota <= 0:
            raise StorageError("persistent storage is a Premium feature on this bot")
        used, files = self.usage(uid)
        if used + extra_bytes > quota:
            raise StorageError(
                f"storage is full ({used / MB:.1f} of {quota // MB} MB used); delete something first"
            )
        if files + extra_files > max_files():
            raise StorageError(f"storage allows at most {max_files()} files")

    def _make_parents(self, base: Path, rel: str) -> Path:
        current = base
        current.mkdir(parents=True, exist_ok=True)
        for part in rel.split("/")[:-1]:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                os.mkdir(current, 0o755)
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise StorageError(f"'{part}' is a file, not a folder")
        return current

    def write(self, uid: str, path: str, data: bytes) -> int:
        uid = self._uid(uid)
        rel = clean_path(path)
        if rel == PUBLIC_DIR:
            raise StorageError("public is a folder")
        with self.locked(uid):
            base = self.user_dir(uid)
            existing = self._resolve(base, rel) if base.exists() else None
            old = 0
            new_file = 1
            if existing is not None:
                info = os.lstat(existing)
                if stat.S_ISDIR(info.st_mode):
                    raise StorageError("a folder already uses that name")
                old, new_file = info.st_size, 0
            self._check_capacity(uid, len(data) - old, new_file)
            parent = self._make_parents(base, rel)
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".mw-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(tmp, 0o644)
                os.replace(tmp, parent / rel.rsplit("/", 1)[-1])
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
            return self._bump(uid)

    def mkdir(self, uid: str, path: str) -> int:
        uid = self._uid(uid)
        rel = clean_path(path)
        with self.locked(uid):
            if self.quota(uid) <= 0:
                raise StorageError("persistent storage is a Premium feature on this bot")
            # make_parents builds every component of rel; the "/x" is the
            # leaf it deliberately doesn't create.
            self._make_parents(self.user_dir(uid), rel + "/x")
            return self._bump(uid)

    def delete(self, uid: str, path: str) -> int:
        uid = self._uid(uid)
        rel = clean_path(path)
        with self.locked(uid):
            target = self._resolve(self.user_dir(uid), rel)
            if target is None:
                raise StorageError("nothing at that path")
            info = os.lstat(target)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(target)
            else:
                os.unlink(target)
            return self._bump(uid)

    def move(self, uid: str, src: str, dst: str) -> int:
        uid = self._uid(uid)
        a, b = clean_path(src), clean_path(dst)
        if b == a or b.startswith(a + "/"):
            raise StorageError("cannot move a folder into itself")
        with self.locked(uid):
            base = self.user_dir(uid)
            source = self._resolve(base, a)
            if source is None:
                raise StorageError("nothing at the source path")
            if self._resolve(base, b) is not None:
                raise StorageError("the destination already exists")
            parent = self._make_parents(base, b)
            os.rename(source, parent / b.rsplit("/", 1)[-1])
            return self._bump(uid)

    def wipe(self, uid: str) -> bool:
        """Privacy deletion: the tree, the handle, the generation row."""
        uid = self._uid(uid)
        with self.locked(uid):
            shutil.rmtree(self.users / uid, ignore_errors=True)
            with self._db() as db:
                db.execute("DELETE FROM users WHERE uid=?", (uid,))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.locks / f"{uid}.lock")
        return True

    # ---- handles --------------------------------------------------------

    def handle_for(self, uid: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT handle FROM users WHERE uid=?", (self._uid(uid),)).fetchone()
        return row[0] if row and row[0] else None

    def uid_for_handle(self, handle: str) -> str | None:
        try:
            value = valid_handle(handle)
        except StorageError:
            return None
        with self._db() as db:
            row = db.execute("SELECT uid FROM users WHERE handle=?", (value,)).fetchone()
        return row[0] if row else None

    def set_handle(self, uid: str, handle: str) -> str:
        uid = self._uid(uid)
        value = valid_handle(handle)
        with self._db() as db:
            owner = db.execute("SELECT uid FROM users WHERE handle=?", (value,)).fetchone()
            if owner and owner[0] != uid:
                raise StorageError("that handle is taken")
            db.execute(
                "INSERT INTO users(uid, handle, gen, updated) VALUES(?, ?, 0, ?) "
                "ON CONFLICT(uid) DO UPDATE SET handle=excluded.handle, updated=excluded.updated",
                (uid, value, time.time()),
            )
        return value

    def ensure_handle(self, uid: str, name: str = "") -> str:
        existing = self.handle_for(uid)
        if existing:
            return existing
        base = suggest_handle(name, uid)
        for attempt in range(50):
            candidate = base if attempt == 0 else f"{base[:26]}-{attempt + 1}"
            try:
                return self.set_handle(uid, candidate)
            except StorageError:
                continue
        return self.set_handle(uid, "u" + str(uid)[-10:])

    # ---- signed links ---------------------------------------------------

    def sign(self, uid: str, path: str, *, ttl: int = 7 * 86400) -> tuple[str, int]:
        uid, rel = self._uid(uid), clean_path(path)
        expires = int(time.time()) + max(60, min(int(ttl), 30 * 86400))
        mac = hmac.new(self._key, f"{uid}\0{rel}\0{expires}".encode(), hashlib.sha256)
        return mac.hexdigest()[:32], expires

    def verify(self, uid: str, path: str, expires: str, signature: str) -> bool:
        try:
            uid, rel, when = self._uid(uid), clean_path(path), int(expires)
        except (StorageError, TypeError, ValueError):
            return False
        if when < time.time():
            return False
        mac = hmac.new(self._key, f"{uid}\0{rel}\0{when}".encode(), hashlib.sha256)
        return hmac.compare_digest(mac.hexdigest()[:32], str(signature or ""))

    # ---- sandbox sync ---------------------------------------------------

    def _sha(self, path: Path, info: os.stat_result) -> str:
        key = (str(path), info.st_size, info.st_mtime_ns, info.st_ino)
        with self._hash_lock:
            cached = self._hash_cache.get(key)
        if cached:
            return cached
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        with self._hash_lock:
            if len(self._hash_cache) > 50000:
                self._hash_cache.clear()
            self._hash_cache[key] = value
        return value

    def manifest(self, uid: str) -> dict[str, str]:
        """path -> sha256 for every regular file; directories map to ''."""
        base = self.user_dir(uid)
        out: dict[str, str] = {}
        for path, info, is_dir in self._walk(base):
            out[path] = "" if is_dir else self._sha(base / path, info)
        return out

    def export_tar(self, uid: str) -> tuple[tempfile.SpooledTemporaryFile, dict[str, str], int]:
        """Snapshot the tree as a tar for restoring into a sandbox.

        Returns (tarfile handle rewound to 0, manifest, generation). The
        manifest is the merge base for the next import from that sandbox.
        """
        uid = self._uid(uid)
        # Handed to the caller, who closes it once the sandbox has eaten it.
        spool = tempfile.SpooledTemporaryFile(max_size=8 * MB)  # noqa: SIM115
        try:
            with self.locked(uid):
                generation = self.generation(uid)
                base = self.user_dir(uid)
                manifest: dict[str, str] = {}
                with tarfile.open(fileobj=spool, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for path, info, is_dir in self._walk(base):
                        member = tarfile.TarInfo(path)
                        member.mtime = int(info.st_mtime)
                        member.uid = member.gid = 0
                        member.uname = member.gname = "root"
                        if is_dir:
                            member.type = tarfile.DIRTYPE
                            member.mode = 0o755
                            manifest[path] = ""
                            tar.addfile(member)
                            continue
                        member.mode = 0o644
                        member.size = info.st_size
                        fd = os.open(base / path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(fd, "rb") as handle:
                            manifest[path] = self._sha(base / path, info)
                            tar.addfile(member, handle)
        except BaseException:
            spool.close()
            raise
        spool.seek(0)
        return spool, manifest, generation

    def _stage(self, tar: tarfile.TarFile, stage: Path, quota: int, result: MergeResult) -> dict[str, str] | None:
        """Unpack a streamed tar into ``stage`` by hand. None means refuse it all.

        tarfile.extract* is not used on purpose: member names, link targets and
        modes all come from the sandbox, which is exactly who we don't trust.
        """
        incoming: dict[str, str] = {}
        total = file_count = 0
        limit_files = max_files()
        for member in tar:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            name = name.rstrip("/")
            if name in {"", "."}:
                continue
            try:
                rel = clean_path(name)
            except StorageError:
                result.skipped.append(name[:80])
                continue
            if member.isdir():
                try:
                    self._make_parents(stage, rel + "/x")
                except StorageError:
                    result.skipped.append(rel)
                    continue
                incoming[rel] = ""
                continue
            if not member.isreg():
                result.skipped.append(rel)
                continue
            total += member.size
            if total > quota:
                result.error = f"/storage is over the {quota // MB} MB quota; nothing was saved"
                return None
            file_count += 1
            if file_count > limit_files:
                result.error = f"/storage has more than {limit_files} files; nothing was saved"
                return None
            source = tar.extractfile(member)
            if source is None:
                result.skipped.append(rel)
                continue
            # Parent may be a file in a hostile archive; make_parents refuses
            # rather than clobbering.
            try:
                parent = self._make_parents(stage, rel)
            except StorageError:
                result.skipped.append(rel)
                continue
            target = parent / rel.rsplit("/", 1)[-1]
            if target.is_dir():
                result.skipped.append(rel)
                continue
            digest = hashlib.sha256()
            with open(target, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
            os.chmod(target, 0o644)
            with contextlib.suppress(OSError):
                os.utime(target, (member.mtime, member.mtime))
            incoming[rel] = digest.hexdigest()
            for ancestor in _ancestors(rel):
                incoming.setdefault(ancestor, "")
        return incoming

    def import_tar(
        self,
        uid: str,
        fileobj,
        base: dict[str, str],
        *,
        base_generation: int,
    ) -> MergeResult:
        """Merge a sandbox's /storage tar back into the host tree.

        Three-way-ish: whatever the sandbox changed relative to ``base`` is
        applied; host-side edits made meanwhile (dashboard, tools) survive
        unless the sandbox touched the same path, in which case the sandbox
        wins because it's the newer write. Symlinks, hardlinks and device
        nodes are dropped and reported, never recreated.
        """
        uid = self._uid(uid)
        quota = self.quota(uid)
        result = MergeResult(skipped=[])
        if quota <= 0:
            result.error = "persistent storage is not enabled for this user"
            return result
        stage = Path(tempfile.mkdtemp(prefix=f"{uid}-", dir=self.staging))
        try:
            try:
                with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
                    incoming = self._stage(tar, stage, quota, result)
            except (tarfile.TarError, EOFError):
                result.error = "the sandbox returned an unreadable archive"
                return result
            if incoming is None:
                return result

            with self.locked(uid):
                live_base = self.user_dir(uid)
                live = self.manifest(uid)
                result.clean = self.generation(uid) == base_generation and live == base
                changed = {p: h for p, h in incoming.items() if base.get(p) != h}
                removed = [p for p in base if p not in incoming]
                # Project the post-merge size before touching anything.
                sizes = {p: os.lstat(live_base / p).st_size for p, h in live.items() if h}
                for path, digest in changed.items():
                    if digest:
                        sizes[path] = os.lstat(stage / path).st_size
                for path in removed:
                    if live.get(path) == base.get(path):
                        sizes.pop(path, None)
                if sum(sizes.values()) > quota:
                    result.error = (
                        f"saving would exceed the {quota // MB} MB quota; /storage changes were not saved"
                    )
                    return result
                # Deletions deepest-first so folders empty out before removal.
                for path in sorted(removed, key=lambda p: p.count("/"), reverse=True):
                    if path not in live or live.get(path) != base.get(path):
                        continue  # host changed it meanwhile; keep the host copy
                    target = self._resolve(live_base, path)
                    if target is None:
                        continue
                    try:
                        if live[path] == "":
                            os.rmdir(target)
                        else:
                            os.unlink(target)
                        result.deleted += 1
                    except OSError:
                        # Non-empty dir: the host added something inside it.
                        result.clean = False
                for path in sorted(changed, key=lambda p: p.count("/")):
                    digest = changed[path]
                    if digest == "":
                        try:
                            self._make_parents(live_base, path + "/x")
                        except StorageError:
                            result.skipped.append(path)
                        continue
                    try:
                        parent = self._make_parents(live_base, path)
                    except StorageError:
                        result.skipped.append(path)
                        result.clean = False
                        continue
                    destination = parent / path.rsplit("/", 1)[-1]
                    if destination.is_dir() and not destination.is_symlink():
                        result.skipped.append(path)
                        result.clean = False
                        continue
                    os.replace(stage / path, destination)
                    result.written += 1
                result.generation = self._bump(uid) if (result.written or result.deleted) else self.generation(uid)
                result.manifest = self.manifest(uid)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return result


def _ancestors(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def tar_bytes(files: dict[str, bytes]) -> bytes:
    """Tiny helper for tests and for seeding: {path: data} -> tar bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, data in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(data)
            member.mtime = int(time.time())
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()
