"""Tool implementations for the sites plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

from plugins.discord_messages.impl import SendFileTool  # noqa: E402

class CreateSiteTool(Tool):
    """Publish a website — one page or a whole directory, static or backed."""
    tool_name = 'create_site'
    returns_result = True
    ends_turn = False


    MAX_CONTENT_SIZE = 3000000  # 3MB for big single-file 3D scenes, full movie recreations, complex interactive demos etc. (use base64 encoding in tool call for safety)

    async def _download_site_image(
        self, url: str, img_dir: str, filename_hint=None
    ) -> tuple[str | None, str | None]:
        """Download an image from a URL into img_dir for a site.

        Returns (dest_path, None) on success, (None, error) on failure.
        Lets create_site consume image URLs directly (Discord CDN, the
        permanent image URLs from image_generator, external hosts) instead
        of requiring a local path.
        """
        if not _is_safe_url(url):
            return None, "unsafe URL"
        try:
            session = await _get_shared_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                content_type = (
                    (resp.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if not content_type.startswith("image/"):
                    return None, f"not an image ({content_type or 'unknown'})"
                blob = await _read_response_limited(resp, 10 * 1024 * 1024)
        except Exception as e:
            return None, str(e)[:120]
        if not blob:
            return None, "empty body"
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            ext = ".png"
        filename = str(
            filename_hint or f"site-image-{int(datetime.now(timezone.utc).timestamp())}"
        )
        filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename).strip(".")
        filename = re.sub(r"^[.\\/-]+", "", filename)
        if not filename or filename in {".", ".."}:
            filename = "image"
        if not os.path.splitext(filename)[1]:
            filename += ext
        dest = os.path.join(img_dir, filename)
        if os.path.commonpath(
            [os.path.abspath(dest), os.path.abspath(img_dir)]
        ) != os.path.abspath(img_dir):
            return None, "filename escapes images dir"
        try:
            # Off-thread write; see _load_one above.
            await asyncio.to_thread(Path(dest).write_bytes, blob)
        except Exception as e:
            return None, f"write failed: {e}"
        return dest, None

    def __init__(self, bot):
        super().__init__(bot)
        self.base_dir = getattr(bot.config, "MAXWELL_SITE_DIR", "public/bot")
        self.base_url = (
            getattr(
                bot.config, "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com"
            ).rstrip("/")
            + "/bot"
        )

    def _control(self) -> dict:
        return (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )

    def get_description(self):
        return (
            f"Publish a site at {self.base_url}/<name>/. "
            "Full visual freedom: invent a new design each time; no house style unless asked. "
            "Ship it finished — real content, working controls, no placeholders. "
            "No lorem ipsum; a Loading shell has shipped nothing. "
            "Params: name, title, body (complete HTML for index.html) OR url "
            "(fetch existing HTML and host it), "
            'files (extra files {"path":"content"}), '
            "backend (optional, default false), encoding, permanent. "
            "Static HTML/CSS/JS is first-class. backend=true enables only the "
            "simple site KV API; custom backend deployment is unavailable."
        )

    async def _fetch_site_html(self, url: str) -> str:
        """Download a public HTML file to use as index.html."""
        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"
        try:
            _final, content_type, raw = await _fetch_public_url(
                url, max_bytes=self.MAX_CONTENT_SIZE
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error fetching URL: {e}"
        if not raw:
            return "Error: fetched page was empty"
        if not _blob_looks_like_html(raw, content_type, url):
            return (
                "Error: that URL is not an HTML page. "
                "Use host_file(url=...) to publish it as a file."
            )
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return "Error: fetched page was empty"
        return text

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        title: str | None = None,
        body: str | None = None,
        encoding: str = "text",
        images: str | None = None,
        files: Any = None,
        backend: Any = None,
        permanent: Any = None,
        url: str | None = None,
        **kwargs,
    ) -> str:
        # Available to everyone (non-admins too). Quota + ownership checks apply.
        extra_files, files_err = _parse_site_files(files)
        if files_err:
            return f"Error: {files_err}"
        source_url = str(url or kwargs.get("src") or "").strip()
        fetched_from_url = False
        if source_url and (body is None or not str(body).strip()):
            fetched = await self._fetch_site_html(source_url)
            if fetched.startswith("Error:"):
                return fetched
            body = fetched
            fetched_from_url = True
            if not title:
                title = _title_from_html(body) or "hosted page"
        has_index = any(f["path"] == "index.html" for f in extra_files)
        if not name or not title or (body is None and not has_index):
            missing = []
            if not name:
                missing.append("name")
            if not title:
                missing.append("title")
            if body is None and not has_index:
                missing.append("body (or url= of an HTML file, or files with an index.html)")
            return (
                f"Error: missing required params — {', '.join(missing)}. "
                "name + title + (body or url) are the minimum for a site."
            )

        mode = str(encoding or "text").strip().lower()
        if body is None:
            body = ""
        elif fetched_from_url:
            # Already decoded HTML from the URL; do not re-interpret as base64.
            pass
        elif mode in {"base64", "b64"}:
            try:
                body = base64.b64decode(str(body), validate=True).decode("utf-8")
            except Exception as e:
                return f"Error: could not decode base64 site body: {e}"
        elif mode not in {"text", "utf8", "utf-8"}:
            return "Error: encoding must be text or base64"
        elif not isinstance(body, str):
            return "Error: site body must be a string"
        else:
            normalized_body = _normalize_site_body_text_escapes(body)
            if normalized_body != body:
                logger.info(
                    "Normalized escaped whitespace in create_site body (%d chars changed)",
                    sum(a != b for a, b in zip(body, normalized_body, strict=False))
                    + abs(len(body) - len(normalized_body)),
                )
                body = normalized_body

        # Sanitize name
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())[:30].strip("-")
        if not slug or len(slug) < 2:
            return "Error: name must be at least 2 valid characters"

        user_id = str(message.author.id)
        is_admin = bool(self.bot and self.bot._is_admin(message.author.id))
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = self.bot._sites

        # Block slug takeover: only owner or admin may overwrite an existing site.
        existing = sites.get(slug) if isinstance(sites, dict) else None
        if isinstance(existing, dict):
            owner = str(existing.get("user_id") or "")
            if owner and owner != user_id and not is_admin:
                return (
                    f"Error: site slug '{slug}' is already owned by another user. "
                    "Pick a different name."
                )

        control = self._control()

        if len(body) > self.MAX_CONTENT_SIZE:
            return f"Error: content too long ({len(body)} chars, max {self.MAX_CONTENT_SIZE})"
        blocked = _elided_site_payload_error(body)
        if blocked:
            return blocked
        for entry in extra_files:
            blob = entry.get("bytes") or b""
            with contextlib.suppress(UnicodeDecodeError):
                blocked = _elided_site_payload_error(blob.decode("utf-8"))
                if blocked:
                    return blocked
        extra_bytes = sum(len(f["bytes"] or b"") for f in extra_files)
        if len(body.encode("utf-8")) + extra_bytes > SITE_MAX_TOTAL_BYTES:
            return f"Error: site too large (max {SITE_MAX_TOTAL_BYTES // 1000}KB across all files)"

        site_dir = os.path.join(self.base_dir, slug)
        created_new_dir = not os.path.isdir(site_dir)
        try:
            os.makedirs(site_dir, exist_ok=True)

            # Copy images into site's images/ directory
            image_urls = []
            missing_images = []
            if images:
                try:
                    image_list = (
                        json.loads(images) if isinstance(images, str) else images
                    )
                    if not isinstance(image_list, list):
                        image_list = [image_list]
                except json.JSONDecodeError:
                    # Might be comma-separated paths
                    image_list = [
                        {"path": p.strip()} for p in images.split(",") if p.strip()
                    ]

                img_dir = os.path.join(site_dir, "images")
                os.makedirs(img_dir, exist_ok=True)
                # Reuse the same broad-but-safe allowlist as SendFileTool so
                # images produced by image_generator (Discord CDN downloads)
                # and the shell sandbox (shelldocker) can actually be
                # embedded. The old check only allowed MAXWELL_SITE_DIR, which
                # rejected virtually every real image source (the feature was
                # silently non-functional).
                send_tool = self.bot.tools.get("send_file") if self.bot else None
                if send_tool is not None and hasattr(
                    send_tool, "_allowed_send_file_bases"
                ):
                    allowed_bases = send_tool._allowed_send_file_bases()
                else:
                    allowed_bases = [self.base_dir]
                for entry in image_list:
                    if isinstance(entry, str):
                        entry = {"path": entry}
                    src_url = str(entry.get("url") or "").strip()
                    src_path = entry.get("path", "")
                    if src_url and not src_path:
                        # URL entries: download the image into the site's
                        # images/ dir so the site is fully self-hosted and
                        # never depends on an expiring external link.
                        dest, err = await self._download_site_image(
                            src_url, img_dir, entry.get("filename")
                        )
                        if dest:
                            public_url = f"{self.base_url}/{slug}/images/{os.path.basename(dest)}"
                            image_urls.append(public_url)
                            logger.info(f"Downloaded site image {src_url} -> {dest}")
                        else:
                            missing_images.append(src_url)
                            logger.warning(
                                f"Site image URL failed: {src_url} ({err or 'unknown'})"
                            )
                        continue
                    if not src_path or not any(
                        _is_path_allowed(src_path, b) for b in allowed_bases
                    ):
                        missing_images.append(src_path or "(empty path)")
                        logger.warning(f"Site image blocked or not found: {src_path}")
                        continue
                    filename = entry.get("filename") or os.path.basename(src_path)
                    # Sanitize filename: only safe chars, and strip path
                    # separators / leading dots so ".." can't write outside
                    # the images/ dir.
                    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename).strip(".")
                    filename = re.sub(r"^[.\\/-]+", "", filename)
                    if not filename or filename in {".", ".."}:
                        filename = "image"
                    dest = os.path.join(img_dir, filename)
                    # Final guard: ensure dest stays inside img_dir.
                    if os.path.commonpath(
                        [os.path.abspath(dest), os.path.abspath(img_dir)]
                    ) != os.path.abspath(img_dir):
                        missing_images.append(src_path)
                        logger.warning(
                            f"Site image filename escapes images dir: {filename}"
                        )
                        continue
                    try:
                        shutil.copy2(src_path, dest)
                        public_url = f"{self.base_url}/{slug}/images/{filename}"
                        image_urls.append(public_url)
                        logger.info(f"Copied site image {src_path} -> {dest}")
                    except Exception as e:
                        logger.warning(f"Failed to copy image {src_path}: {e}")

            # The page is served exactly as written. CSP belongs to the host
            # (see SITE_CSP_META) — turn `site_inject_csp` on only if yours
            # doesn't set one.
            if body and parse_bool(control.get("site_inject_csp", False), False):
                body = _inject_site_csp(body)

            written: list[str] = []
            if body:
                err = await _write_site_file(
                    site_dir, "index.html", body.encode("utf-8")
                )
                if err:
                    return f"Error creating site: {err}"
                written.append("index.html")
            for entry in extra_files:
                err = await _write_site_file(
                    site_dir, entry["path"], entry["bytes"] or b""
                )
                if err:
                    return f"Error creating site: {err}"
                written.append(entry["path"])

            wants_backend = parse_bool(backend, False)
            is_permanent = parse_bool(permanent, False)

            # Commit the site metadata under a cross-process FileLock so a
            # concurrent create_site (or an API site_update/site_delete) can't
            # lose this entry or have this entry overwrite theirs. Reload fresh
            # inside the lock and re-check slug ownership. If the save fails,
            # remove the just-written HTML so we don't leave an untracked site.
            site_entry = {
                "user_id": user_id,
                "user_name": message.author.display_name,
                "created_at": datetime.now(timezone.utc).timestamp(),
                "title": title,
                "path": site_dir,
                "backend": wants_backend,
                "permanent": is_permanent,
            }
            try:
                committed = await asyncio.to_thread(
                    self._commit_site_locked, slug, user_id, is_admin, site_entry
                )
            except Exception as e:
                # Best-effort cleanup of orphaned HTML only when this call
                # created the directory. Never rmtree a slug another user
                # already committed.
                if created_new_dir:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(site_dir, ignore_errors=True)
                logger.error(f"Failed to commit site metadata for {slug}: {e}")
                return f"Error creating site: {e}"
            if not committed:
                # Overwrite disallowed by a concurrent owner change discovered
                # under the lock; clean up only a directory we created.
                if created_new_dir:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(site_dir, ignore_errors=True)
                return (
                    f"Error: site slug '{slug}' could not be committed "
                    "(ownership changed concurrently). Try again."
                )
            result = f"Site created: {self.base_url}/{slug}/"
            if len(written) > 1:
                result += f"\nFiles: {', '.join(written)}"
            if wants_backend:
                result += "\n" + site_backend.client_guide(f"/api/site/{slug}")
                result += "\nThe simple KV API is the only supported backend here."
            result += f"\nLifetime: {site_expiry_label(site_entry, control)}."
            # Placeholders shipped in the HTML are invisible in a 200 response
            # and in a screenshot of a page that has not mounted, so they are
            # named here where the model cannot miss them.
            shortfalls = _site_placeholder_warnings(body, extra_files)
            if shortfalls:
                result += (
                    "\nUNFINISHED CONTENT — fix these with edit_site before you "
                    "tell anyone the site is done:\n"
                    + "\n".join(f"  • {item}" for item in shortfalls)
                )
            api_paths = _site_api_path_warnings(slug, body, extra_files)
            if api_paths:
                result += (
                    "\nWRONG API PATH — the frontend can never reach the backend "
                    "like this. Fix with edit_site before you tell anyone the "
                    "site is done:\n"
                    + "\n".join(f"  • {item}" for item in api_paths)
                )
            if image_urls:
                result += f"\nEmbedded images ({len(image_urls)}):\n" + "\n".join(
                    f"  - {url}" for url in image_urls
                )
            if missing_images:
                result += (
                    f"\nWARNING: {len(missing_images)} image(s) NOT found on disk and skipped: "
                    + ", ".join(missing_images)
                )
            result += _site_graph_note(self.bot, slug)
            return result
        except Exception as e:
            logger.error(f"Failed to create site {slug}: {e}")
            return f"Error creating site: {e}"

    def _commit_site_locked(
        self, slug: str, user_id: str, is_admin: bool, entry: dict
    ) -> bool:
        """Reload sites.json under a cross-process lock, re-check ownership,
        add the entry, and save atomically. Returns True on commit.

        Runs in a worker thread (via asyncio.to_thread) because FileLock uses
        blocking fcntl. This is the single locked RMW for create_site metadata,
        closing the lost-update race with the API process and concurrent
        creates.
        """
        path = Path(self.bot.config.DATA_DIR) / "sites.json"
        with FileLock(path, timeout=15.0):
            sites = {}
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        sites = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.error(f"Corrupt sites.json on commit, aborting: {e}")
                return False
            # Re-check slug ownership under the lock (may have changed).
            existing = sites.get(slug)
            if isinstance(existing, dict):
                owner = str(existing.get("user_id") or "")
                if owner and owner != user_id and not is_admin:
                    return False
            sites[slug] = entry
            _atomic_json_write_sync(path, sites)
            # Keep the in-memory map + mtime in sync for this process.
            self.bot._sites = sites
            with contextlib.suppress(OSError):
                self.bot._sites_mtime = path.stat().st_mtime
            return True

    async def _save_sites(self):
        try:
            path = Path(self.bot.config.DATA_DIR) / "sites.json"

            # Cross-process lock so the API's site_update/site_delete and this
            # write can't interleave and lose an entry.
            def _locked_write():
                with FileLock(path, timeout=15.0):
                    _atomic_json_write_sync(path, self.bot._sites)
                    return path.stat().st_mtime if path.exists() else 0.0

            mtime = await asyncio.to_thread(_locked_write)
            if hasattr(self.bot, "_sites_mtime"):
                self.bot._sites_mtime = mtime
        except Exception as e:
            logger.error(f"Failed to save sites: {e}")
            raise

class EditSiteTool(_SiteOwnedTool):
    """Change a published site in place — a file, a line, or its settings."""
    tool_name = 'edit_site'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Edit a site you already published, at its existing URL. Use this "
            "to keep working on the frontend (HTML/CSS/JS) — do not recreate "
            "the site. "
            "action=list (files + sizes; notes a Python backend if one is running), "
            "read (one file; large files return a numbered window — pass "
            "start_line= to page; do not re-read a file you already have), "
            "write (replace or add a file — path defaults to index.html; or pass "
            "files={...} to write several at once), "
            "replace (swap `find` with `replace` in one file; all=true for every "
            "occurrence), delete (remove a file), rename, backend (on/off/status/"
            "clear the KV store — not the Python server), extend. "
            "Python backend code is site_server (write/replace/read), not this. "
            "Params: name, action, path, content, files, find, replace, all, "
            "title, encoding, backend, permanent, start_line. "
            "Prefer this over re-running create_site for a tweak."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        action: str = "list",
        path: str | None = None,
        content: Any = None,
        files: Any = None,
        find: str | None = None,
        replace: str | None = None,
        all: Any = None,
        replace_all: Any = None,
        title: str | None = None,
        encoding: str = "text",
        backend: Any = None,
        permanent: Any = None,
        start_line: Any = None,
        **kwargs,
    ) -> str:
        slug, entry, site_dir, err = self._resolve(message, name)
        if err:
            return err
        act = str(action or "list").strip().lower()
        if act in SITE_MUTATING_ACTIONS:
            site_read_loop_guard(message, key="", label="", action=act)
        url = f"{self.base_url}/{slug}/"

        if act in {"list", "ls", "files", "status"}:
            tree = _site_tree(site_dir)
            if not tree:
                return f"{url} has no files on disk (it may have expired)."
            lines = [f"  • {rel} ({size} bytes)" for rel, size in tree]
            out = f"{url}\n" + "\n".join(lines)
            out += f"\nLifetime: {site_expiry_label(entry, self._control())}."
            if entry.get("backend"):
                out += "\nKV store: on — " + site_backend.summarize(
                    self.bot.config.DATA_DIR, slug
                )
            if entry.get("server"):
                server_files = site_server.list_code(self.bot.config.DATA_DIR, slug)
                names = ", ".join(rel for rel, _ in server_files) or "no source"
                out += (
                    f"\nPython backend: on at {url}api/ ({names}). "
                    "Edit it with site_server (list/read/write/replace), not this tool."
                )
            out += _site_graph_note(self.bot, slug, refresh=False)
            return out

        if act in {"read", "cat", "get"}:
            rel = _safe_site_relpath(path or "index.html")
            if not rel:
                return f"Error: bad path {path!r}"
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}. Use action=list."
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return f"Error reading {rel}: {e}"
            start = _site_start_line(start_line)
            blocked = site_read_loop_guard(
                message,
                key=f"edit_site:{slug}:{rel}:{start}",
                label=f"{rel} (start_line={start})",
                action=act,
            )
            if blocked:
                return blocked
            return format_site_file_read(rel, text, start_line=start)

        if act in {"write", "put", "set", "update"}:
            extra_files, files_err = _parse_site_files(files)
            if files_err:
                return f"Error: {files_err}"
            for entry_file in extra_files:
                blob = entry_file.get("bytes") or b""
                with contextlib.suppress(UnicodeDecodeError):
                    blocked = _elided_site_payload_error(blob.decode("utf-8"))
                    if blocked:
                        return blocked
            if content is not None:
                blocked = _elided_site_payload_error(
                    content if isinstance(content, str) else json.dumps(content)
                )
                if blocked:
                    return blocked
            written: list[str] = []
            if extra_files:
                for entry_file in extra_files:
                    werr = await _write_site_file(
                        site_dir, entry_file["path"], entry_file["bytes"] or b""
                    )
                    if werr:
                        return f"Error: {werr}"
                    written.append(entry_file["path"])
            if content is not None or not extra_files:
                rel = _safe_site_relpath(path or "index.html")
                if not rel:
                    return f"Error: bad path {path!r}"
                blob, derr = _decode_site_file(content, encoding)
                if derr:
                    return f"Error: {derr}"
                if rel.endswith(".html") and str(encoding or "text").lower() in {
                    "text",
                    "utf8",
                    "utf-8",
                    "",
                }:
                    blob = _normalize_site_body_text_escapes(
                        blob.decode("utf-8", "replace")
                    ).encode("utf-8")
                werr = await _write_site_file(site_dir, rel, blob or b"")
                if werr:
                    return f"Error: {werr}"
                if rel not in written:
                    written.append(rel)
            edit_pages: list[dict] = list(extra_files or [])
            if content is not None and isinstance(content, str):
                edit_pages = edit_pages + [{"path": rel, "bytes": content.encode("utf-8")}]
            api_paths = _site_api_path_warnings(slug, None, edit_pages)
            api_note = (
                "\nWRONG API PATH — the frontend can never reach the backend "
                "like this. Fix before you tell anyone the site is done:\n"
                + "\n".join(f"  • {item}" for item in api_paths)
                if api_paths
                else ""
            )
            return (
                f"Wrote {', '.join(written)} → {url}"
                + api_note
                + _site_graph_note(self.bot, slug)
            )
        if act in {"replace", "patch", "sub"}:
            rel = _safe_site_relpath(path or "index.html")
            if not rel:
                return f"Error: bad path {path!r}"
            if not find:
                return "Error: replace needs `find` (the exact text to swap out)."
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}."
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return f"Error reading {rel}: {e}"
            if find not in text:
                return (
                    f"Error: `find` text is not in {rel} — it must match byte-for-byte. "
                    "Use action=read to see the current file."
                )
            hits = text.count(find)
            all_hits = parse_bool(
                replace_all if replace_all is not None else all, False
            )
            if all_hits:
                updated = text.replace(find, replace or "")
            else:
                updated = text.replace(find, replace or "", 1)
            werr = await _write_site_file(site_dir, rel, updated.encode("utf-8"))
            if werr:
                return f"Error: {werr}"
            if all_hits:
                extra = f" ({hits} occurrence(s))"
            else:
                extra = (
                    f" ({hits - 1} more occurrence(s) left alone)" if hits > 1 else ""
                )
            return (
                f"Patched {rel}{extra} → {url}"
                + _site_graph_note(self.bot, slug)
            )

        if act in {"delete", "rm", "remove"}:
            rel = _safe_site_relpath(path or "")
            if not rel:
                return "Error: delete needs a path. To remove the whole site use delete_site."
            if rel == "index.html":
                return "Error: refusing to delete index.html — write a new one instead."
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}."
            try:
                target.unlink()
            except OSError as e:
                return f"Error deleting {rel}: {e}"
            return f"Deleted {rel} from {slug}." + _site_graph_note(self.bot, slug)

        if act in {"rename", "title", "retitle"}:
            if not title:
                return "Error: rename needs `title`."
            entry = dict(entry)
            entry["title"] = str(title)[:200]
            await asyncio.to_thread(self._save_entry, slug, entry)
            return f"Retitled {slug} → '{entry['title']}' ({url})"

        if act in {"backend", "store", "data"}:
            mode = str(backend if backend is not None else "status").strip().lower()
            data_dir = self.bot.config.DATA_DIR
            if mode in {"clear", "wipe", "reset"}:
                await asyncio.to_thread(site_backend.wipe, data_dir, slug)
                return f"Cleared the backend store for {slug}."
            if mode in {"status", "", "none"}:
                if not entry.get("backend"):
                    return (
                        f"{slug} has no backend. Turn it on with "
                        "edit_site(action=backend, backend=true)."
                    )
                return f"{slug} backend: " + site_backend.summarize(data_dir, slug)
            enabled = parse_bool(mode, False)
            entry = dict(entry)
            entry["backend"] = enabled
            await asyncio.to_thread(self._save_entry, slug, entry)
            if not enabled:
                return f"Backend off for {slug} (data kept; /api/site/{slug} now 404s)."
            return f"Backend on for {slug}.\n" + site_backend.client_guide(
                f"/api/site/{slug}"
            )

        if act in {"extend", "renew", "keep"}:
            entry = dict(entry)
            entry["created_at"] = datetime.now(timezone.utc).timestamp()
            if permanent is not None:
                entry["permanent"] = parse_bool(permanent, False)
            await asyncio.to_thread(self._save_entry, slug, entry)
            return f"{slug}: {site_expiry_label(entry, self._control())} ({url})"

        return (
            f"Error: unknown action '{act}'. Use list, read, write, replace, "
            "delete, rename, backend, or extend."
        )

class DeleteSiteTool(_SiteOwnedTool):
    """Take a published site down."""
    tool_name = 'delete_site'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Delete a site you published: removes the files, the metadata, and "
            "its backend store. "
            "Params: name (slug). Irreversible — the URL 404s immediately."
        )

    async def execute(self, message: Message, name: str | None = None, **kwargs) -> str:
        slug, entry, site_dir, err = self._resolve(message, name)
        if err:
            return err
        owner = str(entry.get("user_id") or "")
        author = str(getattr(getattr(message, "author", None), "id", "") or "")
        if (
            owner
            and author
            and owner != author
            and not _caller_is_admin(self.bot, message)
        ):
            return f"Error: site '{slug}' belongs to someone else"
        base = Path(self.base_dir).resolve()
        try:
            target = Path(site_dir).resolve()
        except (OSError, ValueError):
            target = None
        if target is not None and (base in target.parents) and target.is_dir():
            await asyncio.to_thread(shutil.rmtree, target, True)
        save_error = None
        try:
            await asyncio.to_thread(self._save_entry, slug, None)
        except Exception as exc:
            # Still tear down the backend below. Leaving a live container and
            # its secrets behind because metadata persistence failed is worse
            # than returning an error for a partially completed delete.
            save_error = exc
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                site_backend.destroy, self.bot.config.DATA_DIR, slug
            )
        # Container, server code, database, and secrets go too.
        with contextlib.suppress(Exception):
            await site_server.destroy(self.bot.config.DATA_DIR, slug)
        with contextlib.suppress(Exception):
            from knowledge_graph import drop_site as _drop_site_graph

            _drop_site_graph(self.bot, slug)
        if save_error is not None:
            return f"Error deleting site '{slug}': {save_error}"
        return (
            f"Deleted site '{slug}' ({entry.get('title') or 'untitled'}). URL is gone."
        )

class SiteServerTool(_SiteOwnedTool):
    """Give a site a real backend: its own Python server in its own container."""
    tool_name = 'site_server'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Run and edit a real backend server for one of your sites — your own "
            "Python, routes, database, and secrets, in a sandboxed container at "
            "/bot/<name>/api/... "
            "Use this when the site needs server-side logic: accounts, WebSockets, "
            "a hidden API key, anything a static page cannot enforce. "
            "Keep working on a live backend with these actions instead of recreating it: "
            "list (source files), read (one file; large files return a numbered "
            "window — pass start_line= to page; do not re-read a file you already "
            "have), write (merge files — helpers stay; "
            "pass path+content for one file or files={...} for several), replace "
            "(exact-text patch in one file, like edit_site), deploy (full snapshot, "
            "missing files disappear), start, stop, restart, status, logs, env, "
            "rm (delete a helper file, not app.py), delete (tear the server down). "
            "app.py listens on 0.0.0.0:$PORT. flask+waitress for plain HTTP, "
            "fastapi+uvicorn for WebSockets. Only /data is writable and persists. "
            "Frontend pages: edit_site. This tool is the server."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        action: str = "status",
        files: Any = None,
        env: Any = None,
        packages: Any = None,
        path: str | None = None,
        content: Any = None,
        find: str | None = None,
        replace: str | None = None,
        all: Any = None,
        replace_all: Any = None,
        lines: int = 40,
        start_line: Any = None,
        **kwargs,
    ) -> str:
        slug, entry, _site_dir, err = self._resolve(message, name)
        if err:
            return err
        data_dir = self.bot.config.DATA_DIR
        act = str(action or "status").strip().lower()
        if act in SITE_MUTATING_ACTIONS:
            site_read_loop_guard(message, key="", label="", action=act)
        all_hits = parse_bool(replace_all if replace_all is not None else all, False)
        try:
            if act in {"write", "update", "patch_file", "code"}:
                payload = files
                if not payload and content is not None:
                    payload = {str(path or "app.py"): content}
                if not payload:
                    return (
                        'Error: write needs files={"app.py": "..."} or '
                        "path+content for one file.\n" + site_server.contract(slug)
                    )
                parsed = await asyncio.to_thread(site_server.parse_files, payload)
                for source in parsed.values():
                    blocked = _elided_site_payload_error(source)
                    if blocked:
                        return blocked
                existing_app = (
                    site_server.code_dir(data_dir, slug) / "app.py"
                ).is_file()
                if "app.py" not in parsed and not existing_app:
                    return "Error: the entry file must be called app.py."
                new_env = (
                    await asyncio.to_thread(site_server.parse_env, env) if env else None
                )
                extra = await asyncio.to_thread(site_server.parse_packages, packages)
                written = await asyncio.to_thread(
                    site_server.merge_code, data_dir, slug, parsed
                )
                await site_server.start(
                    data_dir, slug, env=new_env, packages=extra or None
                )
                await self._mark_server(slug, entry, True)
                return (
                    f"Backend server live: {self.base_url}/{slug}/api/ "
                    f"(updated {', '.join(written)}; other source files kept)\n"
                    + site_server.contract(slug)
                    + _site_graph_note(self.bot, slug)
                )

            if act in {"deploy", "create", "snapshot"}:
                if not files:
                    return (
                        "Error: deploy needs the full snapshot in files, e.g. "
                        'files={"app.py": "..."}. Missing files are deleted. '
                        "Use action=write to change one file without wiping the rest.\n"
                        + site_server.contract(slug)
                    )
                parsed = await asyncio.to_thread(site_server.parse_files, files)
                for source in parsed.values():
                    blocked = _elided_site_payload_error(source)
                    if blocked:
                        return blocked
                if "app.py" not in parsed:
                    return "Error: the entry file must be called app.py."
                new_env = (
                    await asyncio.to_thread(site_server.parse_env, env) if env else None
                )
                extra = await asyncio.to_thread(site_server.parse_packages, packages)
                written = await asyncio.to_thread(
                    site_server.write_code, data_dir, slug, parsed
                )
                await site_server.start(
                    data_dir, slug, env=new_env, packages=extra or None
                )
                await self._mark_server(slug, entry, True)
                return (
                    f"Backend server live: {self.base_url}/{slug}/api/ "
                    f"(full snapshot: {', '.join(written)})\n"
                    + site_server.contract(slug)
                    + _site_graph_note(self.bot, slug)
                )

            if act in {"replace", "patch", "sub"}:
                rel = path or "app.py"
                note = await asyncio.to_thread(
                    site_server.patch_code,
                    data_dir,
                    slug,
                    rel,
                    find or "",
                    replace,
                    all_hits=all_hits,
                )
                await site_server.start(data_dir, slug)
                await self._mark_server(slug, entry, True)
                return (
                    f"{note} and restarted → {self.base_url}/{slug}/api/"
                    + _site_graph_note(self.bot, slug)
                )

            if act in {"rm", "unlink"}:
                note = await asyncio.to_thread(
                    site_server.delete_code_file, data_dir, slug, path or ""
                )
                await site_server.start(data_dir, slug)
                await self._mark_server(slug, entry, True)
                return (
                    f"{note} and restarted → {self.base_url}/{slug}/api/"
                    + _site_graph_note(self.bot, slug)
                )

            if act in {"start", "restart", "reload"}:
                await site_server.start(data_dir, slug)
                await self._mark_server(slug, entry, True)
                return f"Backend server running at {self.base_url}/{slug}/api/"

            if act in {"stop", "pause"}:
                existed = await site_server.stop(data_dir, slug)
                await self._mark_server(slug, entry, False)
                return (
                    f"Stopped the backend for {slug} (code, data, and secrets kept)."
                    if existed
                    else f"{slug} had no backend server running."
                )

            if act in {"list", "ls", "files"}:
                tree = await asyncio.to_thread(site_server.list_code, data_dir, slug)
                if not tree:
                    return (
                        f"{slug} has no backend source. Write app.py with "
                        "site_server(action=write) first."
                    )
                lines_out = [f"  • {rel} ({size} bytes)" for rel, size in tree]
                return (
                    f"{self.base_url}/{slug}/api/\n"
                    + "\n".join(lines_out)
                    + "\nUse action=read / write / replace to edit. "
                    "write merges; deploy replaces the whole snapshot."
                    + _site_graph_note(self.bot, slug, refresh=False)
                )

            if act in {"status", "info"}:
                return await site_server.status(data_dir, slug)

            if act in {"logs", "log", "tail"}:
                return f"{slug} backend logs:\n" + await site_server.logs(
                    data_dir, slug, lines
                )

            if act in {"read", "cat"}:
                rel = path or "app.py"
                text = await asyncio.to_thread(
                    site_server.read_code, data_dir, slug, rel
                )
                start = _site_start_line(start_line)
                blocked = site_read_loop_guard(
                    message,
                    key=f"site_server:{slug}:{rel}:{start}",
                    label=f"{rel} (start_line={start})",
                    action=act,
                )
                if blocked:
                    return blocked
                return format_site_file_read(rel, text, start_line=start)

            if act in {"env", "secrets", "config"}:
                if not env:
                    current = (site_server.get_entry(data_dir, slug) or {}).get(
                        "env"
                    ) or {}
                    return (
                        f"{slug} env: "
                        + (", ".join(sorted(current)) or "none")
                        + "\nValues are never shown. Pass env={...} to replace them."
                    )
                parsed_env = await asyncio.to_thread(site_server.parse_env, env)
                await site_server.start(data_dir, slug, env=parsed_env)
                await self._mark_server(slug, entry, True)
                return (
                    f"Set {len(parsed_env)} env var(s) on {slug} and restarted it: "
                    + ", ".join(sorted(parsed_env))
                )

            if act in {"delete", "remove", "destroy"}:
                await site_server.destroy(data_dir, slug)
                await self._mark_server(slug, entry, False)
                return (
                    f"Deleted the backend server for {slug} — code, database, and secrets."
                    + _site_graph_note(self.bot, slug)
                )

            return (
                f"Error: unknown action '{act}'. Use list, read, write, replace, "
                "deploy, start, stop, restart, status, logs, env, rm, or delete."
            )
        except site_server.SiteServerError as e:
            # A failed redeploy/start writes a non-running registry row, so
            # keep the public site listing from claiming that its server is
            # still live.
            if act in {
                "write",
                "update",
                "patch_file",
                "code",
                "deploy",
                "create",
                "snapshot",
                "replace",
                "patch",
                "sub",
                "rm",
                "unlink",
                "start",
                "restart",
                "reload",
                "env",
                "secrets",
                "config",
            }:
                with contextlib.suppress(Exception):
                    await self._mark_server(slug, entry, False)
            return f"Error: {e}"

    async def _mark_server(self, slug: str, entry: dict, on: bool) -> None:
        """Record on the site itself that it has a server, for list_sites."""
        updated = dict(entry or {})
        if bool(updated.get("server")) == on:
            return
        updated["server"] = on
        await asyncio.to_thread(self._save_entry, slug, updated)

class ListSitesTool(Tool):
    """List your published sites, with slug, lifetime, and backend state."""
    tool_name = 'list_sites'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List the sites you published: slug, URL, title, time left, "
            "whether each has a KV store or a Python server. The slug is what "
            "edit_site, site_server, and delete_site take. No params."
        )

    async def execute(self, message: Message, all_users: bool = False, **kwargs) -> str:
        all_users = parse_bool(all_users, False)
        user_id = str(message.author.id)
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = getattr(self.bot, "_sites", {}) or {}

        is_admin = _caller_is_admin(self.bot, message)

        if is_admin and all_users:
            selected_sites = sites
        else:
            selected_sites = {
                k: v for k, v in sites.items() if str(v.get("user_id", "")) == user_id
            }

        if not selected_sites:
            return "No active sites found."

        control = (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )
        base_url = getattr(
            self.bot.config,
            "MAXWELL_PUBLIC_BASE_URL",
            "https://maxwell.example.com",
        ).rstrip("/")
        lines = []
        for slug, data in selected_sites.items():
            title = data.get("title", "untitled")
            marks = []
            if data.get("server"):
                marks.append("server")
            elif data.get("backend"):
                marks.append("store")
            owner_label = ""
            if (is_admin or all_users) and data.get("user_id"):
                owner_uid = str(data.get("user_id"))
                owner_label = f" [owner: {owner_uid}]"
            health = data.get("health")
            if isinstance(health, dict):
                if health.get("ok"):
                    marks.append("verified working")
                else:
                    reason = str(health.get("stub") or "broken")
                    marks.append("BROKEN: " + reason[:80])
            tail = f" [{', '.join(marks)}]" if marks else ""
            lines.append(
                f"  • {slug} — {base_url}/bot/{slug}/ — '{title}' "
                f"({site_expiry_label(data, control)}){owner_label}{tail}"
            )
        header = (
            "All active sites:\n" if (is_admin or all_users) else "Your active sites:\n"
        )
        return header + "\n".join(lines)

class HostFileTool(Tool):
    """Publish a file (HTML, image, pdf, …) at a stable public URL."""
    tool_name = 'host_file'
    returns_result = True
    ends_turn = False


    concurrency_class = "site"
    MAX_SIZE = SITE_MAX_TOTAL_BYTES

    def get_description(self):
        base = _public_files_target(self.bot)[1]
        return (
            f"Host a file at a permanent public URL under {base}/<name>/. "
            "Pass url (curl a public file — Discord attachment, raw GitHub, "
            "any http(s) link), or path (local/shell file), or filename+content. "
            "HTML is served as a page (index.html) so Discord can embed the link. "
            "Then send_message the URL without angle brackets. "
            "For a named site you will edit, create_site url= instead."
        )

    def _send_file_helper(self) -> SendFileTool:
        tools = getattr(self.bot, "tools", None) or {}
        existing = tools.get("send_file") if isinstance(tools, dict) else None
        if isinstance(existing, SendFileTool):
            return existing
        return SendFileTool(self.bot)

    async def execute(
        self,
        message: Message,
        url: str | None = None,
        path: str | None = None,
        content: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        encoding: str = "text",
        **kwargs,
    ) -> str:
        source_url = str(url or "").strip()
        source_path = str(path or "").strip()
        has_content = content is not None
        sources = sum(bool(x) for x in (source_url, source_path, has_content))
        if sources == 0:
            return (
                "Error: pass url= (fetch and host), path= (host a local file), "
                "or filename= + content=."
            )
        if sources > 1:
            return "Error: pass only one of url, path, or content"

        blob: bytes | None = None
        hint_name = str(filename or "").strip()
        content_type = ""

        if source_url:
            if not _is_safe_url(source_url):
                return "Error: Cannot fetch from private/internal URLs"
            try:
                _final, content_type, blob = await _fetch_public_url(
                    source_url, max_bytes=self.MAX_SIZE
                )
            except ValueError as e:
                return f"Error: {e}"
            except Exception as e:
                return f"Error fetching URL: {e}"
            if not hint_name:
                hint_name = _filename_from_url(source_url, content_type)
        elif source_path:
            helper = self._send_file_helper()
            resolved = helper._resolve_send_file_path(source_path)
            host_path, host_error = await helper._try_read_host_file(resolved)
            tmp_to_clean = None
            if host_path is None:
                target, cp_error = await helper._docker_cp_from_shell(source_path)
                if target is None:
                    return (
                        f"Error: could not read file at '{source_path}'. "
                        f"Host: {host_error or 'not found'}. "
                        f"Container: {cp_error or 'not found or not readable'}."
                    )
                tmp_to_clean = target
                host_path = target
            try:
                blob = await asyncio.to_thread(host_path.read_bytes)
            except Exception as e:
                return f"Error reading file from disk: {e}"
            finally:
                if tmp_to_clean is not None:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(tmp_to_clean.parent, ignore_errors=True)
            if not hint_name:
                hint_name = host_path.name
        else:
            if not hint_name:
                return "Error: filename is required when hosting inline content"
            mode = str(encoding or "text").strip().lower()
            try:
                if mode in {"base64", "b64"}:
                    blob = base64.b64decode(str(content), validate=True)
                elif mode in {"text", "utf8", "utf-8", ""}:
                    blob = str(content).encode("utf-8")
                else:
                    return "Error: encoding must be text or base64"
            except Exception as e:
                return f"Error: could not decode file content: {e}"

        if not blob:
            return "Error: file was empty"
        if len(blob) > self.MAX_SIZE:
            return f"Error: file is too large (max {self.MAX_SIZE // 1000}KB)"

        is_html = _blob_looks_like_html(blob, content_type, hint_name)
        if is_html:
            rel = "index.html"
        else:
            rel = _safe_site_relpath(hint_name)
            if not rel:
                guessed = _filename_from_url(hint_name, content_type, default="file.bin")
                rel = _safe_site_relpath(guessed)
            if not rel:
                return f"Error: unsafe or unsupported file name: {hint_name!r}"

        fallback = (
            f"f{int(datetime.now(timezone.utc).timestamp())}"
            f"{random.randint(100, 999)}"
        )
        slug_source = name or Path(rel).stem or fallback
        slug = _host_file_slug(slug_source, fallback)
        files_dir, pub_base = _public_files_target(self.bot)
        dest_dir = os.path.join(files_dir, slug)
        if os.path.isdir(dest_dir) and os.listdir(dest_dir):
            slug = _host_file_slug(f"{slug}-{random.randint(10, 99)}", fallback)
            dest_dir = os.path.join(files_dir, slug)

        err = await _write_site_file(dest_dir, rel, blob)
        if err:
            return f"Error hosting file: {err}"

        if is_html:
            public_url = f"{pub_base}/{slug}/"
            kind = "page"
        else:
            public_url = f"{pub_base}/{slug}/{rel}"
            kind = "file"
        local_path = os.path.join(dest_dir, rel.replace("/", os.sep))
        return (
            f"Hosted {kind}: {public_url}\n"
            f"{rel} ({len(blob)} bytes)\n"
            f"Local path: {local_path}\n"
            "send_message this URL without <angle brackets> so Discord unfurls "
            "it — you can see the embed/link. To turn HTML into an editable "
            f'site use create_site(name="{slug}", title="...", url="{public_url}").'
        )
