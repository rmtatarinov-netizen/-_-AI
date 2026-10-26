import os
import re

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")

# Прокси только для Telegram API, например socks5h://127.0.0.1:1080 или http://127.0.0.1:8080
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()

PROVIDERS = {
    "proxyapi": {
        "base_url": "https://api.proxyapi.ru/v1",
        "key_env": "PROXYAPI_API_KEY",
        "model": "openai/gpt-5-mini-2025-08-07",
        "token_param": "max_completion_tokens",
        "supports_reasoning": True,
        "supports_temperature": False,
    },
    "genapi": {
        # OpenAI-совместимый SDK сам допишет /chat/completions к base_url
        "base_url": "https://proxy.gen-api.ru/v1",
        "key_env": "GENAPI_API_KEY",
        "model": "gpt-4.1-mini",
        "token_param": "max_tokens",
        "supports_reasoning": False,
        "supports_temperature": True,
    },
}

DEFAULT_PROVIDER = os.getenv("API_PROVIDER", "proxyapi").strip().lower()
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = "proxyapi"

MAX_CONTEXT_MESSAGES = 20
MAX_TOKENS = 1500
TEMPERATURE = 0.7
REASONING_EFFORT = "low"

CLEAR_COMMANDS = {"очистить контекст", "/reset"}

SYSTEM_PROMPT = "Ты полезный ассистент в Telegram. Отвечай кратко и по делу, по-русски."


def get_provider(name: str) -> dict:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError("Неизвестный провайдер: " + str(name)) from None


def get_api_key(name: str) -> str:
    settings = get_provider(name)
    key = os.getenv(settings["key_env"])
    if not key:
        raise RuntimeError("Не задана переменная окружения " + settings["key_env"] + ". Проверьте файл .env")
    return key


def validate() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    elif not BOT_TOKEN_RE.fullmatch(BOT_TOKEN.strip()):
        raise RuntimeError(
            "BOT_TOKEN в .env не похож на настоящий токен (нужен формат 123456789:AA...). "
            "Получите токен у @BotFather и сохраните файл .env"
        )
    default_key_env = PROVIDERS[DEFAULT_PROVIDER]["key_env"]
    if not os.getenv(default_key_env):
        missing.append(default_key_env)
    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Проверьте файл .env"
        )
