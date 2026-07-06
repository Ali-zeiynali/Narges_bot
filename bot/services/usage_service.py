from datetime import UTC, datetime

from bot.storage.database import Database
from bot.storage.orm import UsageLogORM


class UsageService:
    def __init__(self, database: Database, model: str, provider: str = "groq") -> None:
        self.database = database
        self.model = model
        self.provider = provider

    def log(
        self,
        user_id: int | None,
        chat_id: int | None,
        estimated_tokens: int,
        usage: dict[str, int | None],
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        if total_tokens is None:
            total_tokens = estimated_tokens
        with self.database.orm.session() as session:
            session.add(
                UsageLogORM(
                    user_id=user_id,
                    chat_id=chat_id,
                    provider=provider or self.provider,
                    model=model or self.model,
                    estimated_tokens=estimated_tokens,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    created_at=datetime.now(UTC),
                )
            )
