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


def provider_status_text(user_id: int) -> str:
    current = context_manager.get_provider(user_id)
    model = config.PROVIDERS[current]["model"]
    return (
        "Текущий провайдер: " + current + " (модель " + model + ").\n"
        "Нажми кнопку ниже или отправь команду /api " + " | /api ".join(config.PROVIDERS) + "."
    )


def provider_keyboard(user_id: int) -> telebot.types.InlineKeyboardMarkup:
    current = context_manager.get_provider(user_id)
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
    for name, settings in config.PROVIDERS.items():
        label = ("✅ " if name == current else "") + name + " (" + settings["model"] + ")"
        keyboard.add(telebot.types.InlineKeyboardButton(text=label, callback_data="prov:" + name))
    return keyboard


def switch_provider(user_id: int, name: str) -> tuple[bool, str]:
    try:
        settings = config.get_provider(name)
    except ValueError:
        logger.info("Запрошен неизвестный провайдер %r (user_id=%s)", name, user_id)
        return False, "Неизвестный провайдер: " + name + ". Доступные: " + ", ".join(config.PROVIDERS)
    try:
        config.get_api_key(name)
    except RuntimeError as error:
        logger.error("Переключение %s невозможно: %s", name, error)
        return False, "Не получилось переключить провайдер: " + str(error)
    context_manager.set_provider(user_id, name)
    logger.info("user_id=%s переключён на %s", user_id, name)
    return True, "Переключено на провайдер " + name + " (модель " + settings["model"] + ")."


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: telebot.types.Message) -> None:
    send_with_retry(bot.reply_to, message, HELP_TEXT)


@bot.message_handler(commands=["api"])
def cmd_api(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    logger.info("Команда %r от user_id=%s", message.text, user_id)
    args = (message.text or "").split()

    if len(args) == 1:
        send_with_retry(
            bot.reply_to,
            message,
            provider_status_text(user_id),
            reply_markup=provider_keyboard(user_id),
        )
        return

    name = args[1].lower().split("@")[0]
    ok, result = switch_provider(user_id, name)
    send_with_retry(
        bot.reply_to,
        message,
        result + "\n\n" + provider_status_text(user_id),
        reply_markup=provider_keyboard(user_id),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("prov:"))
def cb_provider(callback: telebot.types.CallbackQuery) -> None:
    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    ok, result = switch_provider(user_id, name)
    logger.info("Кнопка prov:%s от user_id=%s (ok=%s)", name, user_id, ok)
    new_text = result + "\n\n" + provider_status_text(user_id)
    msg = callback.message
    try:
        if getattr(msg, "is_editable", False):
            bot.edit_message_text(
                new_text,
                msg.chat.id,
                msg.message_id,
                reply_markup=provider_keyboard(user_id),
            )
        else:
            send_with_retry(
                bot.send_message,
                msg.chat.id,
                new_text,
                reply_markup=provider_keyboard(user_id),
            )
    except Exception:
        logger.exception("Не удалось обновить сообщение с кнопками")
    try:
        bot.answer_callback_query(callback.id, "Готово: " + name if ok else result[:120])
    except Exception:
        logger.debug("answer_callback_query не доставлен", exc_info=True)


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
