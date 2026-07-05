from bot.services.history_service import HistoryService


class ConversationSearchTool:
    """Limited search tool scoped to one user's own conversation history."""

    def __init__(self, history_service: HistoryService) -> None:
        self.history_service = history_service

    def search(self, user_id: int, query: str, limit: int = 5) -> list[dict[str, str]]:
        limit = max(1, min(limit, 10))
        return self.history_service.search_user_messages(user_id, query, limit)
