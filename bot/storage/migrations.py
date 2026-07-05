MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_initial",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS global_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_events (
            id TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            payload TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_message_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_memories_user_active
            ON memories(user_id, active, kind);

        CREATE TABLE IF NOT EXISTS relationships (
            user_id INTEGER PRIMARY KEY,
            familiarity INTEGER NOT NULL,
            trust INTEGER NOT NULL,
            respect INTEGER NOT NULL,
            comfort INTEGER NOT NULL,
            joke_permission INTEGER NOT NULL,
            nickname TEXT,
            boundary_warnings INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            text_preview TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_user_created
            ON conversation_history(user_id, created_at);

        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            model TEXT NOT NULL,
            estimated_tokens INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            created_at TEXT NOT NULL
        );
        """,
    )
    ,
    (
        "002_onboarding_channels_quota",
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language_code TEXT,
            display_name TEXT,
            suggested_name TEXT,
            pending_name TEXT,
            onboarding_state TEXT NOT NULL DEFAULT 'new',
            name_confirm_attempted INTEGER NOT NULL DEFAULT 0,
            plan TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            join_url TEXT,
            position INTEGER NOT NULL DEFAULT 100,
            is_private INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT NOT NULL,
            channel_id INTEGER,
            before_payload TEXT,
            after_payload TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS membership_cache (
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            status TEXT,
            is_member INTEGER NOT NULL,
            error TEXT,
            checked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(user_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS admin_bypasses (
            user_id INTEGER PRIMARY KEY,
            bypass_until TEXT NOT NULL,
            reason TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quota_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            cost INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_quota_events_user_created
            ON quota_events(user_id, created_at, kind);
        """,
    ),
    (
        "003_professional_memory_and_narges_state",
        """
        ALTER TABLE memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 3;
        ALTER TABLE memories ADD COLUMN metadata TEXT;
        ALTER TABLE memories ADD COLUMN last_seen_at TEXT;

        ALTER TABLE relationships ADD COLUMN intimacy_level INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE relationships ADD COLUMN current_chat_feeling TEXT NOT NULL DEFAULT 'neutral';

        CREATE TABLE IF NOT EXISTS memory_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            memory_id INTEGER,
            action TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            before_payload TEXT,
            after_payload TEXT,
            source_message_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            telegram_message_id INTEGER,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_created
            ON conversation_messages(user_id, created_at);

        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_messages_fts
            USING fts5(text, content='conversation_messages', content_rowid='id');

        CREATE TRIGGER IF NOT EXISTS conversation_messages_ai
            AFTER INSERT ON conversation_messages
            BEGIN
                INSERT INTO conversation_messages_fts(rowid, text) VALUES (new.id, new.text);
            END;

        CREATE TABLE IF NOT EXISTS narges_self_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL,
            mood TEXT NOT NULL,
            energy INTEGER NOT NULL,
            activity TEXT NOT NULL,
            location TEXT NOT NULL,
            is_alone INTEGER NOT NULL,
            companions TEXT,
            mind_topics TEXT NOT NULL,
            source TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_narges_self_states_active
            ON narges_self_states(is_active)
            WHERE is_active = 1;

        CREATE TABLE IF NOT EXISTS narges_state_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            before_payload TEXT,
            after_payload TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS narges_state_scheduler_runs (
            run_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_date, slot)
        );
        """,
    ),
]
