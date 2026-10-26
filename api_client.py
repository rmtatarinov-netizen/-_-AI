import logging

import openai
from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_clients: dict[str, OpenAI] = {}


class ApiError(Exception):
    pass


def _get_client(provider: str) -> OpenAI:
    if provider not in _clients:
        settings = config.get_provider(provider)
        try:
            api_key = config.get_api_key(provider)
        except RuntimeError as error:
            raise ApiError(str(error)) from error
        _clients[provider] = OpenAI(api_key=api_key, base_url=settings["base_url"])
    return _clients[provider]


def get_ai_response(provider: str, messages: list[dict]) -> str:
    settings = config.get_provider(provider)
    model = settings["model"]
    logger.info(
        "Запрос к API: провайдер=%s, model=%s, сообщений=%s, max_tokens=%s",
        provider,
        model,
        len(messages),
        config.MAX_TOKENS,
    )
    logger.debug("Отправляемые сообщения: %s", messages)

    kwargs = {settings["token_param"]: config.MAX_TOKENS}
    if settings.get("supports_reasoning"):
        kwargs["reasoning_effort"] = config.REASONING_EFFORT
    if settings.get("supports_temperature"):
        kwargs["temperature"] = config.TEMPERATURE

    client = _get_client(provider)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": config.SYSTEM_PROMPT}, *messages],
            **kwargs,
        )
    except openai.AuthenticationError as error:
        logger.error("Ошибка авторизации (%s): %s", provider, error)
        raise ApiError("Неверный ключ API. Проверьте " + settings["key_env"] + " в .env") from error
    except openai.RateLimitError as error:
        logger.error("Превышен лимит запросов или средств (%s): %s", provider, error)
        raise ApiError("Лимит запросов или средств исчерпан. Попробуйте позже") from error
    except openai.APIConnectionError as error:
        logger.error("Нет соединения с API (%s): %s", provider, error)
        raise ApiError("Сервер API недоступен. Попробуйте позже") from error
    except openai.APIStatusError as error:
        logger.error("API (%s) вернул статус %s: %s", provider, error.status_code, error.body)
        raise ApiError("Ошибка API (" + str(error.status_code) + "). Детали в логе") from error
    except Exception as error:
        logger.exception("Неизвестная ошибка при запросе к API (%s)", provider)
        raise ApiError("Неизвестная ошибка при обращении к API") from error

    content = response.choices[0].message.content
    usage = getattr(response, "usage", None)
    logger.info(
        "Ответ получен (%s): finish_reason=%s, токенов всего=%s",
        provider,
        response.choices[0].finish_reason,
        usage.total_tokens if usage else "н/д",
    )
    return content or "Модель вернула пустой ответ"
