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
    (
        "004_user_moderation_and_message_dates",
        """
        CREATE TABLE IF NOT EXISTS user_warning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL,
            source_message_id INTEGER,
            warning_count_after INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_user_warning_events_user_created
            ON user_warning_events(user_id, created_at);

        CREATE TABLE IF NOT EXISTS user_blocks (
            user_id INTEGER PRIMARY KEY,
            warning_count INTEGER NOT NULL,
            blocked_until TEXT NOT NULL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        "005_account_debug_and_phone_bonus",
        """
        ALTER TABLE users ADD COLUMN phone_number TEXT;
        ALTER TABLE users ADD COLUMN phone_verified_at TEXT;
        ALTER TABLE users ADD COLUMN phone_bonus_claimed INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE IF NOT EXISTS debug_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_debug_logs_event_created
            ON debug_logs(event, created_at);
        """,
    ),
    (
        "006_billing_invoices",
        """
        CREATE TABLE IF NOT EXISTS billing_invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            stars_cost INTEGER NOT NULL,
            message_quota INTEGER NOT NULL,
            status TEXT NOT NULL,
            payment_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_billing_invoices_user_created
            ON billing_invoices(user_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_billing_invoices_status
            ON billing_invoices(status);
        """,
    ),
    (
        "007_provider_usage_and_message_types",
        """
        ALTER TABLE conversation_messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'chat';
        ALTER TABLE conversation_messages ADD COLUMN provider TEXT;
        ALTER TABLE conversation_messages ADD COLUMN model TEXT;
        ALTER TABLE conversation_messages ADD COLUMN input_tokens INTEGER;
        ALTER TABLE conversation_messages ADD COLUMN output_tokens INTEGER;
        ALTER TABLE conversation_messages ADD COLUMN total_tokens INTEGER;

        ALTER TABLE usage_logs ADD COLUMN provider TEXT NOT NULL DEFAULT 'groq';
        """,
    ),
    (
        "008_user_gender",
        """
        ALTER TABLE users ADD COLUMN gender TEXT;
        """,
    ),
    (
        "009_scale_quota_cost_units",
        """
        UPDATE quota_events
        SET cost = cost * 5
        WHERE cost > 0
          AND (
            kind = 'quota_consume'
            OR kind = 'extra_consume'
            OR kind LIKE 'extra_grant%'
          );
        """,
    ),
    (
        "010_admin_panel_and_ai_provider_status",
        """
        CREATE TABLE IF NOT EXISTS ai_provider_key_statuses (
            provider TEXT NOT NULL,
            key_index INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            disabled_until TEXT,
            last_success_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(provider, key_index)
        );

        CREATE TABLE IF NOT EXISTS admin_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            target_count INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'created',
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        );
        """,
    ),
    (
        "011_groups_broadcasts_and_schedules",
        """
        ALTER TABLE admin_broadcasts ADD COLUMN target_type TEXT NOT NULL DEFAULT 'users';
        ALTER TABLE admin_broadcasts ADD COLUMN target_value TEXT;

        CREATE TABLE IF NOT EXISTS group_chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            chat_type TEXT NOT NULL,
            bot_status TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduled_group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,
            last_run_at TEXT,
            sent_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]
