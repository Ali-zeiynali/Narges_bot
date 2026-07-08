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
    (
        "012_context_memory_membership_state",
        """
        ALTER TABLE users ADD COLUMN registration_state TEXT NOT NULL DEFAULT 'new';
        ALTER TABLE users ADD COLUMN membership_state TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE users ADD COLUMN last_membership_gate_chat_id INTEGER;
        ALTER TABLE users ADD COLUMN last_membership_gate_message_id INTEGER;

        UPDATE users
        SET registration_state = CASE
            WHEN onboarding_state = 'need_channels' THEN
                CASE
                    WHEN display_name IS NOT NULL AND gender IS NOT NULL THEN 'ready'
                    WHEN display_name IS NOT NULL THEN 'ask_gender'
                    ELSE 'new'
                END
            ELSE onboarding_state
        END;

        UPDATE users
        SET onboarding_state = registration_state
        WHERE onboarding_state = 'need_channels';

        ALTER TABLE conversation_messages ADD COLUMN provider_response_id TEXT;
        ALTER TABLE conversation_messages ADD COLUMN safety_metadata TEXT;
        ALTER TABLE conversation_messages ADD COLUMN tone_metadata TEXT;
        ALTER TABLE conversation_messages ADD COLUMN intent TEXT;

        CREATE TABLE IF NOT EXISTS conversation_summaries (
            user_id INTEGER PRIMARY KEY,
            summary TEXT NOT NULL DEFAULT '',
            summarized_message_id INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_context_states (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'casual',
            topic TEXT,
            recent_intent TEXT NOT NULL DEFAULT 'casual',
            intent_confidence REAL NOT NULL DEFAULT 0.5,
            relationship_stage TEXT NOT NULL DEFAULT 'new',
            trust_level REAL NOT NULL DEFAULT 0,
            familiarity_score REAL NOT NULL DEFAULT 0,
            last_user_message_hash TEXT,
            last_assistant_text_hash TEXT,
            last_assistant_intent TEXT,
            last_interaction_at TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        "013_referrals_and_ai_request_payloads",
        """
        ALTER TABLE users ADD COLUMN referral_code TEXT;
        ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER;
        ALTER TABLE users ADD COLUMN first_question_at TEXT;
        ALTER TABLE users ADD COLUMN referral_bonus_claimed_at TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code);
        CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by_user_id);

        ALTER TABLE conversation_messages ADD COLUMN ai_request_payload TEXT;
        """,
    ),
    (
        "014_admin_broadcast_deliveries",
        """
        CREATE TABLE IF NOT EXISTS admin_broadcast_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            target_type TEXT NOT NULL DEFAULT 'user',
            telegram_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'created',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_admin_broadcast_deliveries_broadcast
            ON admin_broadcast_deliveries(broadcast_id);

        CREATE INDEX IF NOT EXISTS idx_admin_broadcast_deliveries_target
            ON admin_broadcast_deliveries(target_id);
        """,
    ),
    (
        "015_user_prompt_tracking",
        """
        ALTER TABLE users ADD COLUMN last_prompt_chat_id INTEGER;
        ALTER TABLE users ADD COLUMN last_prompt_message_id INTEGER;
        """,
    ),
    (
        "016_user_nudges_and_reengagement",
        """
        ALTER TABLE users ADD COLUMN last_gender_nudge_date TEXT;
        ALTER TABLE users ADD COLUMN last_reengagement_sent_at TEXT;
        """,
    ),
    (
        "017_media_files",
        """
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            telegram_message_id INTEGER,
            telegram_file_id TEXT NOT NULL,
            media_kind TEXT NOT NULL,
            mime_type TEXT,
            original_file_name TEXT,
            storage_path TEXT NOT NULL,
            file_size INTEGER,
            caption TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_media_files_user_kind_created
            ON media_files(user_id, media_kind, created_at);
        """,
    ),
    (
        "018_media_blobs_and_hashes",
        """
        ALTER TABLE media_files ADD COLUMN content_hash TEXT;
        ALTER TABLE media_files ADD COLUMN file_bytes BLOB;

        CREATE INDEX IF NOT EXISTS idx_media_files_content_hash
            ON media_files(content_hash);
        """,
    ),
    (
        "019_group_engine_events",
        """
        CREATE TABLE IF NOT EXISTS group_engine_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            event_type TEXT NOT NULL,
            telegram_message_id INTEGER,
            metadata TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_group_engine_events_chat_type_created
            ON group_engine_events(chat_id, event_type, created_at);

        CREATE INDEX IF NOT EXISTS idx_group_engine_events_user_type_created
            ON group_engine_events(user_id, event_type, created_at);
        """,
    ),
    (
        "020_group_member_count",
        """
        ALTER TABLE group_chats ADD COLUMN member_count INTEGER;
        """,
    ),
    (
        "021_group_invite_rewards",
        """
        CREATE TABLE IF NOT EXISTS group_invite_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            member_granted INTEGER NOT NULL DEFAULT 0,
            admin_granted INTEGER NOT NULL DEFAULT 0,
            bot_status TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_group_invite_rewards_user_chat
            ON group_invite_rewards(user_id, chat_id);

        CREATE INDEX IF NOT EXISTS idx_group_invite_rewards_chat
            ON group_invite_rewards(chat_id);
        """,
    ),
]
