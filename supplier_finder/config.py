"""Конфигурация через переменные окружения (.env).

pydantic-settings даёт типобезопасный конфиг: если забыли ключ — упадёт
понятной ошибкой, а не где-то в середине запроса.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_GIGACHAT_MODEL = "GigaChat"

_GIGACHAT_MODEL_ALIASES = {
    "gigachat lite": DEFAULT_GIGACHAT_MODEL,
    "gigachat-lite": DEFAULT_GIGACHAT_MODEL,
    "gigachat 2 lite": "GigaChat-2",
    "gigachat-2-lite": "GigaChat-2",
    "lite": DEFAULT_GIGACHAT_MODEL,
    "gigachat2": "GigaChat-2",
    "gigachat 2": "GigaChat-2",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- GigaChat ---
    # Авторизационный ключ из личного кабинета (base64 от Client ID:Client Secret).
    gigachat_credentials: str = Field(default="", alias="GIGACHAT_CREDENTIALS")
    gigachat_scope: str = Field(default="GIGACHAT_API_PERS", alias="GIGACHAT_SCOPE")
    gigachat_model: str = Field(default=DEFAULT_GIGACHAT_MODEL, alias="GIGACHAT_MODEL")
    gigachat_base_url: str = Field(
        default="https://api.giga.chat/v1", alias="GIGACHAT_BASE_URL"
    )
    gigachat_auth_url: str = Field(
        default="https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        alias="GIGACHAT_AUTH_URL",
    )
    # Для GigaChat может понадобиться Russian Trusted Root CA.
    # Отключать TLS-проверку стоит только локально и временно.
    gigachat_verify_ssl: bool = Field(default=True, alias="GIGACHAT_VERIFY_SSL_CERTS")
    gigachat_ca_bundle_file: str = Field(default="", alias="GIGACHAT_CA_BUNDLE_FILE")

    # --- Поиск ---
    # Приоритет движков: Яндекс -> Tavily -> бесплатный DuckDuckGo.
    # Для тестов можно отключить поисковые API и брать фиксированный список URL.
    use_fixed_search_urls: bool = Field(default=False, alias="USE_FIXED_SEARCH_URLS")
    # Yandex Search API v2 требует ключ + идентификатор каталога (folderId).
    yandex_search_api_key: str = Field(default="", alias="YANDEX_SEARCH_API_KEY")
    yandex_folder_id: str = Field(default="", alias="YANDEX_FOLDER_ID")
    # Tavily опционален. Если ключа нет — используется бесплатный DuckDuckGo.
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")

    # --- Доступ к веб-интерфейсу ---
    # Пароль для входа в Streamlit-приложение. Пусто = вход без пароля.
    app_password: str = Field(default="", alias="APP_PASSWORD")

    # --- Скрапинг ---
    request_timeout: float = Field(default=15.0, alias="REQUEST_TIMEOUT")
    max_concurrency: int = Field(default=5, alias="MAX_CONCURRENCY")
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (compatible; SupplierFinderBot/1.0; "
            "+https://example.com/bot)"
        ),
        alias="USER_AGENT",
    )

    @field_validator("gigachat_model", mode="before")
    @classmethod
    def normalize_gigachat_model(cls, value: str | None) -> str:
        if value is None:
            return DEFAULT_GIGACHAT_MODEL

        model = str(value).strip()
        if not model:
            return DEFAULT_GIGACHAT_MODEL

        return _GIGACHAT_MODEL_ALIASES.get(model.lower(), model)


settings = Settings()
