from collections.abc import Callable
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.runnables import Runnable

from ticketpilot.config import settings

Configure = Callable[[BaseChatModel], Runnable[LanguageModelInput, Any]]


def get_llm(model: str = settings.primary_model) -> BaseChatModel:
    return init_chat_model(model, temperature=0, max_retries=settings.max_retries)


def with_fallback(configure: Configure) -> Runnable[LanguageModelInput, Any]:
    primary = configure(get_llm(settings.primary_model))
    return primary.with_fallbacks([configure(get_llm(settings.fallback_model))])
