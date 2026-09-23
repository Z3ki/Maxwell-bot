"""The public runtime no longer starts or exposes a Telegram transport."""

import inspect

import bot
import config


def test_telegram_transport_symbols_are_gone():
    assert not hasattr(bot, "TelegramMessageAdapter")
    assert not hasattr(bot, "TelegramUserAdapter")
    assert not hasattr(bot.MaxwellBot, "_telegram_loop")
    assert not hasattr(bot.MaxwellBot, "_telegram_webhook_loop")
    assert "telegram" not in inspect.getsource(bot.MaxwellBot.setup_hook).lower()


def test_config_cannot_enable_telegram():
    assert not hasattr(config.Config, "ENABLE_TELEGRAM")
    assert not hasattr(config.Config, "TELEGRAM_TOKEN")
    assert not hasattr(config.Config, "TELEGRAM_WEBHOOK_URL")
