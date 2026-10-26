import logging
import logging.handlers
import sys
import time
from pathlib import Path

import requests
import telebot

import config
import context_manager
from api_client import ApiError, get_ai_response


class TokenMaskFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        token = config.BOT_TOKEN
        if token:
            text = text.replace(token, "***BOT_TOKEN***")
        return text


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_formatter = TokenMaskFormatter(_LOG_FORMAT)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

_file_handler = logging.handlers.RotatingFileHandler(
    Path(__file__).resolve().parent / "bot.log",
    encoding="utf-8",
    maxBytes=5_000_000,
    backupCount=3,
)
_file_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
logger = logging.getLogger("bot")

config.validate()

if config.TELEGRAM_PROXY:
    telebot.apihelper.proxy = {"https": config.TELEGRAM_PROXY}
    logger.info("Telegram API работает через прокси: %s", config.TELEGRAM_PROXY)
else:
    logger.warning(
        "TELEGRAM_PROXY не задан: если провайдер блокирует api.telegram.org, "
        "запросы будут отваливаться с SSL-ошибками"
    )

bot = telebot.TeleBot(config.BOT_TOKEN.strip())


def send_with_retry(func, *args, **kwargs):
    delay = 2
    for attempt in range(1, 4):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as error:
            if attempt == 3:
                raise
            logger.warning(
                "Отправка не удалась (попытка %s из 3): %s — повтор через %s с",
                attempt,
                type(error).__name__,
                delay,
            )
            time.sleep(delay)
            delay *= 2


_providers_list = ", ".join(
    name + " (" + settings["model"] + ")" for name, settings in config.PROVIDERS.items()
)

HELP_TEXT = (
    "Привет! Я бот-ассистент.\n\n"
    "Просто напиши сообщение — я отвечу и запомню контекст диалога.\n"
    "Фраза «очистить контекст» или команда /reset стирают историю переписки.\n\n"
    "Доступные провайдеры ИИ: " + _providers_list + ".\n"
    "Текущий провайдер: /api\n"
    "Переключение: /api proxyapi или /api genapi"
)

TG_MAX_LEN = 4096


def split_text(text: str) -> list[str]:
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= TG_MAX_LEN:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, TG_MAX_LEN)
        cut = cut + 1 if cut != -1 else TG_MAX_LEN
        parts.append(rest[:cut])
        rest = rest[cut:]
    return parts


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: telebot.types.Message) -> None:
    send_with_retry(bot.reply_to, message, HELP_TEXT)


@bot.message_handler(commands=["api"])
def cmd_api(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    args = (message.text or "").split()

    if len(args) == 1:
        current = context_manager.get_provider(user_id)
        model = config.PROVIDERS[current]["model"]
        send_with_retry(
            bot.reply_to,
            message,
            "Текущий провайдер: " + current + " (модель " + model + ").\n"
            "Доступные: " + _providers_list + "\n"
            "Переключение: /api " + " | /api ".join(config.PROVIDERS),
        )
        return

    provider = args[1].lower().split("@")[0]
    if provider not in config.PROVIDERS:
        send_with_retry(
            bot.reply_to,
            message,
            "Неизвестный провайдер: " + provider + ". Доступные: " + ", ".join(config.PROVIDERS),
        )
        return

    try:
        config.get_api_key(provider)
    except RuntimeError as error:
        logger.error("Переключение /api %s невозможно: %s", provider, error)
        send_with_retry(bot.reply_to, message, "Не получилось переключить провайдер: " + str(error))
        return

    context_manager.set_provider(user_id, provider)
    logger.info("Команда /api %s от user_id=%s", provider, user_id)
    model = config.PROVIDERS[provider]["model"]
    send_with_retry(
        bot.reply_to,
        message,
        "Переключено на провайдер " + provider + " (модель " + model + ").\n"
        "История диалога сохранена, но новые ответы будет генерировать эта модель.",
    )


@bot.message_handler(commands=["reset"])
def cmd_reset(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    logger.info("Команда /reset от user_id=%s", user_id)
    context_manager.clear(user_id)
    send_with_retry(bot.reply_to, message, "Контекст очищен.")


@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() in config.CLEAR_COMMANDS)
def cmd_clear_by_text(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    logger.info("Фраза «очистить контекст» от user_id=%s", user_id)
    context_manager.clear(user_id)
    send_with_retry(bot.reply_to, message, "Контекст очищен.")


@bot.message_handler(content_types=["text"])
def handle_dialog(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    if not text:
        return

    history = context_manager.get_history(user_id)
    provider = context_manager.get_provider(user_id)
    logger.info(
        "Сообщение от user_id=%s: провайдер=%s, длина=%s, история=%s сообщений",
        user_id,
        provider,
        len(text),
        len(history),
    )
    logger.debug("Текст сообщения user_id=%s: %r", user_id, text)

    try:
        send_with_retry(bot.send_chat_action, chat_id, "typing")
    except Exception:
        pass

    try:
        answer = get_ai_response(provider, history + [{"role": "user", "content": text}])
    except ApiError as error:
        logger.error("API-ошибка для user_id=%s: %s", user_id, error)
        send_with_retry(bot.reply_to, message, "Не получилось: " + str(error))
        return
    except Exception:
        logger.exception("Необработанная ошибка при обработке сообщения user_id=%s", user_id)
        send_with_retry(bot.reply_to, message, "Произошла внутренняя ошибка. Попробуйте позже.")
        return

    context_manager.commit_exchange(user_id, text, answer)
    parts = split_text(answer)
    send_with_retry(bot.reply_to, message, parts[0])
    for part in parts[1:]:
        send_with_retry(bot.send_message, chat_id, part)


if __name__ == "__main__":
    logger.info(
        "Бот запускается. Провайдеры: %s, провайдер по умолчанию: %s",
        ", ".join(
            name + " -> " + s["base_url"] + " (" + s["model"] + ")"
            for name, s in config.PROVIDERS.items()
        ),
        config.DEFAULT_PROVIDER,
    )
    try:
        me = bot.get_me()
        logger.info("Соединение с Telegram OK: @%s (id=%s)", me.username, me.id)
    except telebot.apihelper.ApiTelegramException as error:
        logger.critical(
            "Telegram отклонил BOT_TOKEN (код %s). Проверьте, что настоящий токен от @BotFather "
            "вписан в .env и файл сохранён",
            error.error_code,
        )
        sys.exit(1)
    except Exception:
        logger.critical(
            "Нет соединения с Telegram API (SSL/сеть). Проверьте сеть, VPN или задайте "
            "TELEGRAM_PROXY в .env"
        )
        sys.exit(1)
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=30)
        except telebot.apihelper.ApiTelegramException as error:
            if error.error_code in (401, 404):
                logger.critical(
                    "Токен недействителен или отозван (код %s). Перезапуск невозможен — "
                    "обновите BOT_TOKEN в .env",
                    error.error_code,
                )
                sys.exit(1)
            logger.exception("Ошибка Telegram API, перезапуск через 5 секунд")
            time.sleep(5)
        except Exception:
            logger.exception("Polling аварийно остановлен, перезапуск через 5 секунд")
            time.sleep(5)
