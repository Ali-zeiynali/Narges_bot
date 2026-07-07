# Model Payload Guide

This document describes what is sent to the chat model on each user turn, where each field comes from, and what must not be shared across users.

## Request Shape

Every chat completion receives exactly two top-level messages:

1. `system`
   - Built by `PersonaCompiler.compile()` in `bot/persona/compiler.py`.
   - Contains the static persona, engine rules, current Narges state, and user-scoped runtime context.
2. `user`
   - Built by `ChatService._build_messages()` in `bot/services/chat_service.py`.
   - Contains a compact JSON payload with the current user message, compact user profile, and remaining quota units.

## Static System Sections

These sections are shared by design:

- `STABLE_SYSTEM_PREFIX` from `bot/persona/texts/engine_prompts.py`
- Persona shards from `bot/persona/shards/core.py`
- `ENGINE_RULES` from `bot/persona/texts/engine_prompts.py`
- Current Narges self state from `NargesStateService.get_active()`

The static prompt is cached by persona version and persona file fingerprint. It must not include per-user memory or per-user conversation history.

## User-Scoped Runtime Context

The runtime context is appended to the static system prompt as JSON. It is rebuilt per request and must only contain data for the current Telegram user.

Source: `ContextBuilder.build()` in `bot/services/context_builder.py`.

Fields:

- `current_user_message`: the current normalized message text.
- `last_user_messages`: only the last few user messages for the same user, from `HistoryService.recent_user_messages()`.
- `pending_user_thread`: a compact hint when short consecutive messages look like one thread.
- `inferred_intent` and `recent_intent`: inferred from the current message and pending thread.
- `directly_relevant_memories` and `relevant_memories`: selected by `MemoryService.retrieve_for_context(user_id=...)`.
- `state.mode`: `sexual` only if the current message itself is explicitly sexual; otherwise `normal`.
- `anti_loop`: hash and intent of the last assistant reply for the same user.

Memory lines include `created_at`, `updated_at`, and `expires_at`. The model is instructed to prefer newer active memories when memories conflict and to set `expires_in_days` for temporary memory suggestions.

## Memory Scope Rules

Memory retrieval is always called with the current `user_id`.

Relevant code:

- `MemoryRepository.list_active(user_id, ...)`
- `MemoryRetriever.retrieve(user_id, ...)`
- `MemoryService.retrieve_for_context(user_id, ...)`

No memory from another user should ever enter `context.for_prompt()`. If a prompt payload shows memory not owned by the current user, treat it as a bug in the retrieval path.

## User Message Payload

Source: `ChatService._build_messages()`.

Fields:

- `user_message`: current message text only.
- `user_profile`: compact profile from the current Telegram user only (`display_name`, `gender`, `language_code`).
- `remaining_quota_units_today`: quota units from `QuotaService.begin_generation()`.

## Stored Audit Payload

Every message row stores `ai_request_payload`.

- AI user rows store the actual model messages and compiled section names.
- AI assistant rows store the linked request payload, provider, model, usage, and model state.
- Non-AI rows store a minimal audit payload explaining why no model request was created.

Admin display reads this from `ConversationMessageORM.ai_request_payload_json`.

## Media and Vision

Images and voice are the only media types stored by default.

Images are downloaded temporarily, hashed with SHA-256, stored in `media_files.file_bytes`, and the temporary file is removed. If another image with the same hash already has a vision description, that description is reused instead of calling the vision provider again.

Legacy rows may still have `storage_path`; admin media serving first tries DB bytes, then falls back to legacy file paths.

## Concurrency Rules

The webhook queue tracks the latest update per `(chat_id, user_id)` actor. For each actor:

- stale queued jobs are skipped before handler execution;
- offline backlog keeps only the newest message;
- if a job for the actor is active, newly received updates return `busy` and are not queued.

`QuotaService.begin_generation()` remains the provider-level guard so there is never more than one active generation for a user.
