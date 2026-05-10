CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    importance INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
    category TEXT NOT NULL DEFAULT 'interaction'
        CHECK(category IN (
            'milestone','commitments','turning_points','deep_talks',
            'interaction','preferences','real_world',
            'daily_life','emotional','habits','erotic'
        )),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending','final')),
    enabled_in_context INTEGER NOT NULL DEFAULT 1,
    last_referenced_at TIMESTAMP,
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cards_active ON cards(review_status, enabled_in_context);