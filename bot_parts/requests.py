"""Inbound request admission, outcome recording and reply dispatch.

Split out of bot.py; methods keep their original bodies.
"""

import bot_common as _common

# Mechanical split: the methods below were written against bot.py's
# module globals. Bind every shared name so the bodies run unchanged.
for _name in dir(_common):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_common, _name))
del _name

class RequestPipelineMixin:

    def _get_channel_lock(self, channel_id: str) -> asyncio.Lock:
        return self._channel_locks.get(channel_id)

    def _channel_lock_timeout(self) -> float:
        """How long to wait for the short memory-write critical section.

        This lock no longer spans reply generation, so the wait is bounded by
        a SQLite write rather than by a provider call plus a tool loop. It can
        be short, and a timeout here is now a genuine anomaly instead of the
        routine outcome it used to be.
        """
        raw = (getattr(self, "_control", None) or {}).get(
            "channel_lock_timeout_seconds", 15
        )
        try:
            return max(3.0, min(float(raw), 60.0))
        except (TypeError, ValueError):
            return 15.0

    def _on_reply_queue_drop(self, channel_id: str, entry: Any, why: str) -> None:
        """Record a queue eviction so a drop is never fully silent.

        The queue only evicts stale entries and soft chatter, but a drop is
        still the one outcome worth counting: if this number climbs the room
        is generating turns faster than the bot can answer them, which is a
        capacity problem and not something to discover from user complaints.
        """
        stats = getattr(self, "_reply_drop_counts", None)
        if not isinstance(stats, dict):
            stats = {}
            self._reply_drop_counts = stats
        stats[why] = int(stats.get(why, 0) or 0) + 1
        status = {
            "deferred": "deferred",
            "channel cleared": "superseded",
            "same_user_interrupt": "superseded",
        }.get(why, "suppressed")
        MaxwellBot._record_request_outcome(self, entry.message, status, f"queue_{why}")

    async def _request_state(self, message) -> dict:
        journal = getattr(self, "_request_journal", None)
        mid = str(getattr(message, "id", "") or "")
        return ((await journal.submit(journal.get, mid)) or {}) if journal is not None and mid else {}

    def _record_request_outcome(self, message, status: str, reason: str = "") -> asyncio.Future:
        """Queue callbacks enqueue immediately; async callers await durability."""
        journal = getattr(self, "_request_journal", None)
        mid = str(getattr(message, "id", "") or "")
        if journal is None or not mid:
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        def record():
            row = journal.get(mid)
            if not row or (row["status"] in _REQUEST_TERMINAL and not (
                row["status"] == status == "delivered" and reason == "partial_delivery"
            )):
                return None
            journal.update(mid, status, reason=reason)
            return row

        def completed(future):
            if future.cancelled():
                return
            try:
                row = future.result()
            except Exception as exc:
                logger.error("Journal outcome failed mid=%s status=%s error=%s",
                             mid, status, type(exc).__name__)
                return
            if row is None:
                return
            if status in _REQUEST_TERMINAL:
                (getattr(self, "_interaction_requests", None) or {}).pop(mid, None)
            logger.info(
                "inbound mid=%s cid=%s stage=%s reason=%s attempts=%s effects=%s",
                mid, row["channel_id"], status, reason,
                row["attempts"], row["effects_started"],
            )

        future = journal.submit(record)
        future.add_done_callback(completed)
        return future

    async def _mark_request_effect(self, message) -> None:
        """Commit the checkpoint before HTTP without blocking the Discord loop."""
        journal = getattr(self, "_request_journal", None)
        mid = str(getattr(message, "id", "") or "")
        if journal is None or not mid:
            return

        def checkpoint():
            row = journal.get(mid)
            if row and row["status"] == "superseded":
                raise asyncio.CancelledError
            if row and not row["effects_started"]:
                journal.update(mid, row["status"], effects_started=True)
                return row
            return None

        row = await journal.submit(checkpoint)
        if row:
            logger.info("inbound mid=%s cid=%s stage=effect_started",
                        mid, row["channel_id"])

    async def _record_delivery(self, message, sent) -> None:
        journal = getattr(self, "_request_journal", None)
        mid = str(getattr(message, "id", "") or "")
        if journal is None or not mid or sent is None or not getattr(sent, "id", None):
            return

        def delivered():
            row = journal.get(mid)
            if row and row["status"] not in _REQUEST_TERMINAL:
                journal.update(mid, "delivered", effects_started=True, response_id=str(sent.id))
                return row
            return None

        row = await journal.submit(delivered)
        if row:
            (getattr(self, "_interaction_requests", None) or {}).pop(mid, None)
            logger.info("inbound mid=%s cid=%s stage=delivered response_id=%s",
                        mid, row["channel_id"], sent.id)

    def _inbound_setting(
        self, key: str, default: float, low: float, high: float
    ) -> float:
        try:
            value = float((getattr(self, "_control", None) or {}).get(key, default))
            return max(low, min(value, high)) if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    async def _invoke_request_tool(
        self, inbound_message, tool_identifier: str, handler, /, **tool_args
    ):
        """Track live completion separately from the durable no-replay checkpoint.

        Dispatcher parameters are positional-only and must not share names with
        tool arguments. ``create_site(name=...)`` / ``host_file(name=...)`` used
        to raise ``TypeError: got multiple values for argument 'name'`` because
        the identifier was also called ``name``.
        """
        if tool_identifier not in {"no_response", "more_tools", "reasoning"}:
            await MaxwellBot._mark_request_effect(self, inbound_message)
        state = _current_inbound_effects.get()
        if state and state["message_id"] != str(
            getattr(inbound_message, "id", "") or ""
        ):
            state = None
        if state is not None:
            state["tools_running"] += 1
            if tool_identifier not in RESULT_TOOL_NAMES and tool_identifier not in {
                "no_response", "more_tools", "reasoning"
            }:
                state["uncertain"] = True
            # Legacy tools can send directly, without the slowmode wrapper.
            if tool_identifier in {
                "create_poll",
                "create_thread", "thread_control", "send_message", "send_file", "shell", "send_meme",
                "send_media", "send_gif", "forward_message",
            }:
                state["send_attempted"] = True
        try:
            # Model-supplied kwargs must never shadow the Discord message object.
            tool_args.pop("message", None)
            tool_args.pop("self", None)
            denied = MaxwellBot._authorize_tool_execution(
                self, inbound_message, tool_identifier, handler, tool_args
            )
            if denied:
                raise PermissionError(denied)
            if _current_request_provider.get() is not None:
                budget = _current_byok_tool_budget.get()
                # All parallel calls in a native tool batch share this mutable
                # request budget. Increment synchronously before execute() so
                # sibling tasks cannot race past the provider's eight-call cap.
                if not isinstance(budget, dict) or budget.get("calls", 0) >= 8:
                    raise PermissionError("refused: personal provider tool-call limit reached")
                budget["calls"] += 1
            result = await handler.execute(inbound_message, **tool_args)
            if state is not None:
                if str(result or "").lstrip().lower().startswith(
                    ("error", "could not", "failed", "refused")
                ):
                    state["uncertain"] = True
                else:
                    state["tools_completed"] += 1
            return result
        except BaseException:
            if state is not None:
                state["uncertain"] = True
            raise
        finally:
            if state is not None:
                state["tools_running"] -= 1

    def _authorize_tool_execution(
        self, message, identifier: str, handler, arguments: dict | None = None
    ) -> str | None:
        """Independent authorization check immediately before tool execution.

        Tool catalogs and hooks are presentation/extensibility layers. This
        check runs for native, recovered-text, alias, and internal dispatches.
        """
        canonical = str(
            getattr(handler, "tool_name", None)
            or getattr(handler, "name", None)
            or identifier
        ).strip()
        if not canonical:
            return "refused: tool identity is missing"
        preferences = getattr(self, "_user_preferences", None)
        created = getattr(message, "created_at", None)
        author = getattr(message, "author", None)
        if preferences is not None and created is not None and author is not None:
            if not preferences.request_allowed(author.id, created.timestamp()):
                return "refused: this request predates your privacy deletion or settings are unavailable; send a new request"
        control = getattr(self, "_control", None) or {}
        if not control.get("tools_enabled", True):
            return "refused: tools are disabled"
        from premium_access import premium_for_current_request
        from plugins.maxwell_extras.task_workers import GITHUB_ACCOUNT_ACTIONS, WORKER_TOOLS, _inside_worker
        if (
            canonical in WORKER_TOOLS and not _inside_worker.get()
            and not (canonical == "github_repo" and (arguments or {}).get("action") in GITHUB_ACCOUNT_ACTIONS)
            and premium_for_current_request(getattr(author, "id", ""))
            and control.get("premium_task_workers_enabled", True)
            and "delegate_task" in (getattr(self, "tools", {}) or {})
            and "delegate_task" not in set(control.get("disabled_tools", []) or [])
        ):
            return "refused: delegate this task through delegate_task; task tools run inside workers"
        if _inside_worker.get() and canonical == "github_repo" and ((arguments or {}).get("action") in GITHUB_ACCOUNT_ACTIONS or "token" in (arguments or {})):
            return "refused: workers cannot change accounts or repository permissions"
        if _current_request_provider.get() is not None:
            budget = _current_byok_tool_budget.get()
            if not isinstance(budget, dict) or budget.get("calls", 0) >= 8:
                return "refused: personal provider tool-call limit reached"
        disabled = set(control.get("disabled_tools", []) or [])
        if canonical in disabled or identifier in disabled:
            return "refused: this tool is disabled"
        if (getattr(getattr(self, "config", None), "MAXWELL_RESTRICT_PUBLIC_RUNTIME", False)
                and canonical in PUBLIC_RUNTIME_BLOCKED_TOOLS):
            return "refused: this tool is retired from the public runtime"

        author = getattr(message, "author", None)
        user_id = str(getattr(author, "id", "") or "").strip()
        if canonical in OPERATOR_MAILBOX_TOOLS and not getattr(self, "_is_admin", lambda _uid: False)(user_id):
            return "refused: the operator mailbox is restricted to Maxwell operators"
        needs_identity = bool(
            getattr(handler, "requires_admin", False)
            or getattr(handler, "required_discord_permissions", ())
            or canonical == "shell"
        )
        if needs_identity and not user_id.isdigit():
            return "refused: authenticated requester identity is unavailable"

        manager = getattr(self, "plugin_manager", None)
        plugin_entry = None
        guild = getattr(message, "guild", None)
        guild_id = str(
            getattr(guild, "id", None)
            or getattr(message, "guild_id", None)
            or ""
        )
        if manager is not None:
            plugin_entry = getattr(manager, "all_plugin_tools", {}).get(canonical)
            if plugin_entry:
                try:
                    allowed = manager.get_available_tools(
                        user_id=user_id,
                        platform=MaxwellBot._message_tool_platform(self, message),
                        guild_id=guild_id,
                    )
                except Exception:
                    logger.exception("Tool availability check failed for %s", canonical)
                    return "refused: tool availability could not be verified"
                if canonical not in allowed:
                    return "refused: this tool is unavailable to this user"


        capability = guild_capability_for_tool(
            canonical, plugin_owned=plugin_entry is not None
        )
        server_disabled = (
            (getattr(self, "_control", None) or {}).get(
                "guild_disabled_capabilities", {}
            ) or {}
        ).get(guild_id, [])
        if guild_id and capability in server_disabled:
            return f"refused: this server has disabled the {capability} capability"

        if bool(getattr(handler, "requires_admin", False)):
            checker = getattr(self, "_is_admin", None)
            if not callable(checker) or not checker(user_id):
                return "refused: this tool is restricted to Maxwell operators"

        required_perms = tuple(
            str(value).strip()
            for value in (getattr(handler, "required_discord_permissions", ()) or ())
            if str(value).strip()
        )
        if required_perms:
            guild = getattr(message, "guild", None)
            if guild is None:
                return "refused: this tool requires a server permission context"
            from tooling.helpers import _missing_cap

            for permission in required_perms:
                denial = _missing_cap(
                    guild,
                    permission,
                    message,
                    channel=getattr(message, "channel", None),
                )
                if denial:
                    return denial

        visibility = str(getattr(message, "response_visibility", "public") or "public")
        private_side_effects = {
            "forward_message", "create_poll", "create_thread",
            "react", "create_invite", "leave_server",
            "thread_control", "delete_message", "edit_message", "purge_messages",
            "pin_message", "kick_member", "ban_member", "unban_member",
            "timeout_member", "softban_member", "voice_mod", "set_member_nickname",
            "set_nickname", "create_channel", "edit_channel", "delete_channel",
            "create_category", "edit_category", "delete_category", "set_channel_permissions",
            "move_channel", "clone_channel", "sync_channel", "manage_role",
            "manage_emoji", "manage_invites", "edit_server", "change_avatar",
            "send_media", "send_meme", "send_gif",
            "create_site", "edit_site", "delete_site", "host_file", "site_server",
            "reminder",
        }
        args = arguments or {}
        action = str(args.get("action") or "list").strip().lower()
        site_read = canonical in {"edit_site", "site_server"} and (
            action in {"list", "ls", "files", "read", "cat", "get", "status", "info", "logs", "log", "tail"}
            or (canonical == "site_server" and action == "request"
                and str(args.get("method") or "GET").upper() in {"GET", "HEAD", "OPTIONS"})
        )
        if visibility == "private" and canonical in private_side_effects and not site_read:
            return "refused: choose Public visibility before requesting a server-wide side effect"

        args = arguments or {}
        try:
            encoded = json.dumps(args, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            return "refused: tool arguments are not valid JSON data"
        if len(encoded) > 256 * 1024 or len(args) > 128:
            return "refused: tool arguments exceed the request size limit"
        try:
            import jsonschema

            schema_getter = getattr(handler, "get_parameters", None)
            schema = schema_getter() if callable(schema_getter) else None
            if isinstance(schema, dict) and schema.get("type") == "object":
                jsonschema.Draft202012Validator(schema).validate(args)
        except ImportError:
            return "refused: argument schema validation is unavailable"
        except Exception as exc:
            return f"refused: invalid arguments ({type(exc).__name__})"
        return None

    async def _request_failure(
        self, message, reason: str, *, retryable=False, normal_completion=False
    ) -> None:
        row = (await self._request_state(message))
        if not row or row["status"] in _REQUEST_TERMINAL:
            return
        attempts = int(self._inbound_setting("inbound_retry_attempts", 2, 1, 5))
        if retryable and not row["effects_started"] and row["attempts"] < attempts:
            await self._record_request_outcome(message, "deferred", reason)
            return
        # A tool/send may have succeeded before its confirmation was lost.
        # Never automatically repeat the operation, including after restart.
        state = _current_inbound_effects.get()
        completed_without_send = (
            normal_completion
            and state is not None
            and state["message_id"] == str(getattr(message, "id", "") or "")
            and state["tools_completed"] > 0
            and state["tools_running"] == 0
            and not state["send_attempted"]
            and not state["uncertain"]
        )
        if row["effects_started"] and not completed_without_send:
            reason = f"uncertain_after_effect:{reason}"
        elif row["directed"] and self._control.get("error_replies", True):
            try:
                await self._mark_request_effect(message)
                sent = await asyncio.wait_for(
                    self._send_with_slowmode(
                        message.channel,
                        (
                            "I couldn't produce a final reply after using tools. "
                            "I won't repeat those actions automatically."
                            if row["effects_started"]
                            else "I couldn't finish that request. Please try again."
                        ),
                        reply_to=message,
                    ),
                    timeout=10,
                )
                await self._record_delivery(message, sent)
                if (await self._request_state(message)).get("status") == "delivered":
                    return
            except Exception as exc:
                reason = f"fallback_failed:{type(exc).__name__}"
        await self._record_request_outcome(message, "failed", reason)

    async def _run_reliable_turn(self, message, content: str | None = None):
        """Own the entire turn, including preparation, cancellation and cleanup."""
        journal = getattr(self, "_request_journal", None)
        max_attempts = int(self._inbound_setting("inbound_retry_attempts", 2, 1, 5))

        def begin_turn():
            row = journal.get(str(getattr(message, "id", "") or ""))
            if row and row["status"] not in _REQUEST_TERMINAL and not row["effects_started"] and row["attempts"] < max_attempts:
                journal.begin(row["message_id"])
            return row or {}

        token = _current_inbound.set(message)
        inbox_token = _current_inbox_notices.set(())
        from premium_access import current_plan, load_plan
        plan_token = current_plan.set(None)
        request_provider_token = _current_request_provider.set(None)
        byok_budget_token = _current_byok_tool_budget.set({"calls": 0})
        web_references_token = begin_web_references()
        generated_files_token = begin_generated_files()
        effects_token = _current_inbound_effects.set({
            "message_id": str(getattr(message, "id", "") or ""),
            "tools_running": 0,
            "tools_completed": 0,
            "send_attempted": False,
            "uncertain": False,
        })
        cid = str(getattr(getattr(message, "channel", None), "id", "") or "")
        current = asyncio.current_task()
        self._active_requests[cid] = current
        self._active_request_user[cid] = str(getattr(message.author, "id", ""))
        active_messages = getattr(self, "_active_request_messages", None)
        if active_messages is None:
            active_messages = self._active_request_messages = {}
        active_messages[cid] = message
        started = time.monotonic()
        request_provider = None
        try:
            row = await journal.submit(begin_turn) if journal is not None else {}
            if row and row["status"] in _REQUEST_TERMINAL:
                return
            if row and row["effects_started"]:
                await self._record_request_outcome(message, "failed", "interrupted_after_effect")
                return
            if row and row["attempts"] >= max_attempts:
                await self._request_failure(message, "retry_budget_exhausted")
                return
            uid = str(getattr(message.author, "id", ""))
            current_plan.set((uid, await load_plan(self, uid)))
            request_provider = await self._personal_provider(message)
            if request_provider is not None:
                _current_request_provider.set(request_provider)
            from provider_limits import request_turn_timeout_seconds
            timeout = request_turn_timeout_seconds(self, request_provider, premium=current_plan.get() == (uid, "premium"))
            async with asyncio.timeout(timeout):
                if row:
                    reason = self._queued_request_policy_reason(message)
                    if reason:
                        await self._record_request_outcome(message, "suppressed", reason)
                        return
                await self._handle_message(message, content)
            row = (await self._request_state(message))
            if row.get("status") == "delivered":
                await self._mark_inbox_announced()
            if row and row["status"] not in _REQUEST_TERMINAL:
                await self._request_failure(
                    message, "no_visible_output", retryable=True, normal_completion=True
                )
        except asyncio.CancelledError:
            row = (await self._request_state(message))
            await self._record_request_outcome(message,
            "failed" if row.get("effects_started") else "deferred",
            "cancelled_after_effect" if row.get("effects_started") else "cancelled",)
            raise
        except MessageQuotaExceeded as exc:
            try:
                await self._mark_request_effect(message)
                sent = await self._send_with_slowmode(
                    message.channel, str(exc), reply_to=message
                )
                await self._record_delivery(message, sent)
            except Exception:
                logger.exception("Could not deliver message quota notice")
                await self._record_request_outcome(message, "failed", "quota_notice_failed")
        except Exception as exc:
            logger.warning(
                "inbound mid=%s cid=%s stage=turn_error error=%s",
                getattr(message, "id", ""),
                cid,
                type(exc).__name__,
            )
            await self._request_failure(
                message,
                type(exc).__name__,
                retryable=isinstance(
                    exc, (TimeoutError, ConnectionError, aiohttp.ClientError)
                ),
            )
        finally:
            # _handle_message's historic inner finally starts after preparation.
            # This outer cleanup also covers failures before that point.
            state = (getattr(self, "_inflight_context", None) or {}).get(
                str(getattr(message, "id", "") or "")
            )
            await MaxwellBot._release_turn_context(self, state)
            MaxwellBot._clear_turn_flags(self, cid, current)
            if active_messages.get(cid) is message:
                active_messages.pop(cid, None)
            if request_provider is not None:
                with contextlib.suppress(Exception):
                    await request_provider.close()
            current_plan.reset(plan_token)
            _current_request_provider.reset(request_provider_token)
            _current_byok_tool_budget.reset(byok_budget_token)
            reset_web_references(web_references_token)
            reset_generated_files(generated_files_token)
            _current_inbound.reset(token)
            _current_inbox_notices.reset(inbox_token)
            _current_inbound_effects.reset(effects_token)
            logger.info(
                "inbound mid=%s cid=%s stage=turn_finished duration_ms=%d",
                getattr(message, "id", ""),
                cid,
                (time.monotonic() - started) * 1000,
            )

    def _queued_request_policy_reason(self, message) -> str:
        """Policy can change while an accepted request waits in the queue."""
        control = self._control
        cid = str(message.channel.id)
        if not control.get("bot_enabled", True):
            return "bot_disabled"
        author = message.author
        if (
            str(author.id) in (getattr(self, "_blacklist", None) or set())
            or str(author.id) in set(control.get("ignore_users", []) or [])
        ) and not self._is_admin(author.id):
            return "ignored_author"
        if is_user_install_message(message):
            return ""
        if cid in set(control.get("blocked_channels", []) or []):
            return "blocked_channel"
        allowed = set(control.get("allowed_channels", []) or [])
        if allowed and cid not in allowed:
            return "channel_not_allowed"
        if self._solo_blocks(message):
            return "solo_restriction"
        if isinstance(message.channel, discord.DMChannel):
            if not self._dm_replies_allowed(message):
                return "dm_replies_disabled"
        elif isinstance(message.channel, discord.GroupChannel):
            if not control.get("reply_groups", True):
                return "group_replies_disabled"
        elif message.guild and not control.get("reply_mentions", True):
            return "mention_replies_disabled"
        return ""

    async def _dispatch_reply(self, message, content: str, *, directed: bool) -> str:
        """Hand a message to the per-channel reply queue.

        Replaces the old "take the channel lock or drop the message" contract.
        A turn already running in the room no longer costs the newer message
        its reply — it waits its turn.
        """
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if not channel_id:
            await MaxwellBot._record_request_outcome(self, message, "failed", "missing_channel")
            return "dropped"
        if getattr(self, "_draining", False):
            await MaxwellBot._record_request_outcome(self, message, "suppressed", "draining")
            return "dropped"
        if hydration_value(self, message, "hydration_failed", False):
            await MaxwellBot._record_request_outcome(self, message, "suppressed", "unresolved_pointer")
            return "dropped"
        same_user_check = getattr(self, "_is_same_user_interrupt", None)
        same_user_interrupt = bool(same_user_check(message)) if callable(same_user_check) else False
        if same_user_interrupt:
            if not self._note_same_user_latest(message):
                await self._record_request_outcome(message, "superseded", "same_user_interrupt")
                return "superseded"
            drop_author = getattr(self._reply_queue, "drop_author", None)
            if callable(drop_author):
                for entry in drop_author(channel_id, getattr(message.author, "id", "")):
                    await self._record_request_outcome(entry.message, "superseded", "same_user_interrupt")
        journal = getattr(self, "_request_journal", None)
        if journal is not None:
            await journal.submit(journal.accept, message.id,
            channel_id,
            getattr(message.author, "id", ""),
            directed=directed,)
            row = (await MaxwellBot._request_state(self, message))
            if row.get("status") in _REQUEST_TERMINAL:
                return "duplicate"
            if self._reply_queue.contains(channel_id, message.id):
                return "duplicate"
            await journal.submit(journal.update, message.id, "queued", directed=directed)
        burst: list[Any] = []
        with contextlib.suppress(Exception):
            burst = list(getattr(message, "_watch_burst", None) or [])
        outcome = self._reply_queue.submit(
            channel_id,
            message,
            content,
            directed=directed,
            burst=burst,
        )
        if outcome in {"deferred", "dropped"}:
            await MaxwellBot._record_request_outcome(self,
            message,
            "deferred" if directed else "suppressed",
            f"queue_{outcome}",)
        if outcome == "queued":
            logger.info(
                "Reply queued for %s behind %d turn(s) in %s",
                getattr(message, "id", "?"),
                self._reply_queue.depth(channel_id),
                channel_id,
            )
        return outcome

    def _inflight_reply_user(self, channel_id: Any) -> str:
        """User id of the turn currently being answered in this channel."""
        cid = str(channel_id or "")
        active = (getattr(self, "_active_requests", None) or {}).get(cid)
        if active is not None and not active.done():
            uid = (getattr(self, "_active_request_user", None) or {}).get(cid)
            if uid:
                return str(uid)
        queue = getattr(self, "_reply_queue", None)
        state = (getattr(queue, "_channels", None) or {}).get(cid)
        running = getattr(state, "running", None)
        entry = getattr(state, "running_entry", None)
        if entry is None or running is None or running.done():
            return ""
        author = getattr(getattr(entry.message, "author", None), "id", None)
        return "" if author is None else str(author)

    def _should_interrupt_inflight(self, message) -> bool:
        """Stop a generation only when the same user addresses Maxwell again.

        A later message that does not mention, ping, or reply to Maxwell
        leaves the in-flight turn running.
        """
        addresses = getattr(self, "_message_addresses_self", None)
        if not callable(addresses) or not addresses(message):
            return False
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return False
        channel = getattr(message, "channel", None)
        cid = str(getattr(channel, "id", "") or "")
        if not cid:
            return False
        active_task = (getattr(self, "_active_requests", None) or {}).get(cid)
        active_user = (getattr(self, "_active_request_user", None) or {}).get(cid)
        if active_task is not None and not active_task.done() and active_user:
            if str(active_user) == str(getattr(author, "id", "") or ""):
                active_message = (getattr(self, "_active_request_messages", None) or {}).get(cid)
                if active_message is not None and str(getattr(active_message, "id", "") or "") == str(
                    getattr(message, "id", "") or ""
                ):
                    return False
                return True
            # A different user's turn may be running while this user's
            # replacement waits in the queue. Inspect queued entries below;
            # never interrupt the other user's active turn.
        inflight = MaxwellBot._inflight_reply_user(self, cid)
        queued = None
        if not inflight or inflight != str(getattr(author, "id", "") or ""):
            state = getattr(getattr(self, "_reply_queue", None), "_channels", {}).get(cid)
            queued = getattr(state, "queue", None) if state is not None else None
            if any(
                str(
                    getattr(
                        getattr(getattr(entry, "message", None), "author", None),
                        "id",
                        "",
                    )
                    or ""
                )
                == str(getattr(author, "id", "") or "")
                for entry in (queued or [])
            ):
                return True
        if not inflight:
            return False
        if inflight != str(getattr(author, "id", "") or ""):
            return False
        active_message = (getattr(self, "_active_request_messages", None) or {}).get(cid)
        if active_message is not None and str(getattr(active_message, "id", "") or "") == str(
            getattr(message, "id", "") or ""
        ):
            return False
        return True

    def _note_same_user_latest(self, message) -> bool:
        """True when this message is the newest same-user replacement in the room."""
        cid = str(getattr(getattr(message, "channel", None), "id", "") or "")
        try:
            mid = int(getattr(message, "id", 0) or 0)
        except (TypeError, ValueError):
            mid = 0
        if not cid or mid <= 0:
            return True
        latest = getattr(self, "_same_user_latest", None)
        if latest is None:
            latest = self._same_user_latest = {}
        if mid < int(latest.get(cid, 0) or 0):
            return False
        latest[cid] = mid
        return True

    def _mark_same_user_interrupt(self, message) -> None:
        """Remember an interrupt without mutating Discord's slotted Message."""
        mid = str(getattr(message, "id", "") or "")
        if not mid:
            return
        interrupted = getattr(self, "_same_user_interrupts", None)
        if interrupted is None:
            interrupted = self._same_user_interrupts = {}
        interrupted[mid] = None
        if len(interrupted) > 4096:
            for stale in list(interrupted)[:2048]:
                interrupted.pop(stale, None)

    def _is_same_user_interrupt(self, message) -> bool:
        mid = str(getattr(message, "id", "") or "")
        return bool(
            mid
            and mid in (getattr(self, "_same_user_interrupts", None) or {})
        )

    def _interrupt_same_user(self, message) -> None:
        """Cancel the in-flight reply for this user. Other users keep their place."""
        channel = getattr(message, "channel", None)
        cid = str(getattr(channel, "id", "") or "")
        uid = str(getattr(getattr(message, "author", None), "id", "") or "")
        if not cid or not uid:
            return
        active_user = MaxwellBot._inflight_reply_user(self, cid)
        active_message = (getattr(self, "_active_request_messages", None) or {}).get(cid)
        if active_user == uid:
            if active_message is not None and str(getattr(active_message, "id", "") or "") != str(
                getattr(message, "id", "") or ""
            ):
                self._record_request_outcome(active_message, "superseded", "same_user_interrupt")
        queue = getattr(self, "_reply_queue", None)
        if queue is not None and hasattr(queue, "drop_author"):
            for entry in queue.drop_author(cid, uid):
                self._record_request_outcome(entry.message, "superseded", "same_user_interrupt")
            if active_user == uid:
                queue.cancel_channel(cid, clear_queue=False)
        active = (getattr(self, "_active_requests", None) or {}).get(cid)
        if active_user == uid and active is not None and not active.done():
            cancel_once(active)
        cancel_watch = getattr(self, "_cancel_watch_debounce", None)
        if callable(cancel_watch):
            cancel_watch(cid)
        logger.info(
            "Same-user interrupt cid=%s user=%s latest=%s",
            cid,
            uid,
            getattr(message, "id", ""),
        )
