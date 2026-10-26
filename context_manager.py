import logging

import config

logger = logging.getLogger(__name__)

_context: dict[int, list[dict]] = {}
_providers: dict[int, str] = {}


def get_provider(user_id: int) -> str:
    return _providers.get(user_id, config.DEFAULT_PROVIDER)


def set_provider(user_id: int, provider: str) -> None:
    config.get_provider(provider)  # проверка, что провайдер существует
    _providers[user_id] = provider
    logger.info("user_id=%s переключён на провайдер %s", user_id, provider)


def get_history(user_id: int) -> list[dict]:
    return list(_context.get(user_id, []))


def add_message(user_id: int, role: str, content: str) -> None:
    history = _context.setdefault(user_id, [])
    history.append({"role": role, "content": content})
    if len(history) > config.MAX_CONTEXT_MESSAGES:
        del history[: len(history) - config.MAX_CONTEXT_MESSAGES]
    logger.debug("Контекст user_id=%s обновлён, длина=%s", user_id, len(history))


def commit_exchange(user_id: int, user_text: str, assistant_text: str) -> None:
    add_message(user_id, "user", user_text)
    add_message(user_id, "assistant", assistant_text)


def clear(user_id: int) -> None:
    _context.pop(user_id, None)
    logger.info("Контекст user_id=%s очищен", user_id)
