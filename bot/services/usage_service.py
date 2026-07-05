from datetime import UTC, datetime

from bot.storage.database import Database


class UsageService:
    def __init__(self, database: Database, model: str) -> None:
        self.database = database
        self.model = model

    def log(
        self,
        user_id: int | None,
        chat_id: int | None,
        estimated_tokens: int,
        usage: dict[str, int | None],
    ) -> None:
        self.database.execute(
            """
            INSERT INTO usage_logs(
                user_id, chat_id, model, estimated_tokens,
                prompt_tokens, completion_tokens, total_tokens, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                chat_id,
                self.model,
                estimated_tokens,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                datetime.now(UTC).isoformat(),
            ),
        )
