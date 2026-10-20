"""/dashboard: a private, one-time sign-in link to the web dashboard.

The API process serves the dashboard; this side only proves who's asking.
Discord already authenticated the person pressing the slash command, so the
bot mints a single-use token for them and hands it over ephemerally. No OAuth
app, no client secret, no separate dashboard host required.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def setup(bot, ctx):
    from .links import DashboardLinks
    from .ui import install

    try:
        links = DashboardLinks(bot)
    except ValueError:
        logger.error("Dashboard configuration is invalid; /dashboard is off. See docs/DASHBOARD.md.")
        return []
    if not links.available:
        logger.info("Web dashboard not configured (no MAXWELL_PUBLIC_BASE_URL); /dashboard is off")
        return []
    bot._dashboard_links = links
    ctx.register_service("dashboard_links", links)
    install(bot, links)
    return []


def teardown(bot, ctx):
    from .ui import uninstall

    uninstall()
    if getattr(bot, "_dashboard_links", None) is not None:
        bot._dashboard_links = None
