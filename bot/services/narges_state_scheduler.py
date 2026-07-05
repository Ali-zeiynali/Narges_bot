import asyncio
import json
import logging
from datetime import UTC, datetime, time

from bot.persona.texts.state_prompts import STATE_PERSONA
from bot.services.groq_client import GroqChatClient
from bot.services.narges_state_service import NargesStateService


logger = logging.getLogger(__name__)


STATE_SLOTS = {
    "morning": time(8, 0),
    "afternoon": time(15, 0),
    "night": time(22, 0),
}


class NargesStateScheduler:
    def __init__(self, state_service: NargesStateService, groq_client: GroqChatClient) -> None:
        self.state_service = state_service
        self.groq_client = groq_client

    async def run_due_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for slot, slot_time in STATE_SLOTS.items():
            if now.time() < slot_time:
                continue
            run_date = now.date().isoformat()
            if self.state_service.has_scheduler_run(run_date, slot):
                continue
            await self.run_slot(run_date, slot)

    async def run_slot(self, run_date: str, slot: str) -> bool:
        previous_state = self.state_service.get_active()
        messages = [
            {"role": "system", "content": STATE_PERSONA},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "slot": slot,
                        "date": run_date,
                        "previous_state": previous_state.model_dump(mode="json"),
                        "constraints": [
                            "Do not use user messages.",
                            "Keep the state plausible for a single shared Narges.",
                            "Do not include secrets, URLs, or instructions.",
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            candidate, _usage = await asyncio.to_thread(self.groq_client.complete_narges_state, messages)
            saved = self.state_service.save_candidate(candidate, source=f"scheduler:{slot}")
            self.state_service.mark_scheduler_run(run_date, slot, "ok" if saved else "rejected")
            return saved
        except Exception as exc:
            logger.exception("narges_state_scheduler_failed slot=%s", slot)
            self.state_service.mark_scheduler_run(run_date, slot, "error", exc.__class__.__name__)
            return False

    async def run_forever(self) -> None:
        while True:
            await self.run_due_once()
            await asyncio.sleep(300)
