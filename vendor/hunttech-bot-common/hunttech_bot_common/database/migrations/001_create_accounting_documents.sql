-- Migration 001: Create hunttech_accounting_document and hunttech_accounting_document_event tables
-- Based on storage_db.py schema mapping
-- PostgreSQL 11 compatible (uses uuid-ossp extension)

-- Enable uuid-ossp extension for uuid_generate_v4()
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- hunttech_accounting_document table
CREATE TABLE IF NOT EXISTS hunttech_accounting_document (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    create_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    update_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    flow_type INTEGER NOT NULL CHECK (flow_type IN (1, 2)),  -- 1=PRIMARY, 2=ADVANCE_REPORT
    document_type INTEGER NOT NULL DEFAULT 99,  -- 1=ACT, 2=UPD, 3=INVOICE, 4=CONTRACT, 5=TASK, 6=RECEIPT, 7=REPORT, 8=DECISION, 9=RECONCILIATION, 10=APPLICATION, 11=NOTICE, 12=ORDER, 13=PROTOCOL, 14=LETTER, 99=OTHER
    status INTEGER NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3)),  -- 1=RECOGNIZED, 2=CONFIRMED, 3=SENT
    gph BOOLEAN NOT NULL DEFAULT FALSE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    document_date DATE,
    document_number VARCHAR(255),
    recognized_company_name VARCHAR(500),
    recognized_inn VARCHAR(20),
    amount NUMERIC(18, 2),
    original_file_name VARCHAR(500),
    final_file_name VARCHAR(500),
    yandex_disk_original_path VARCHAR(1000),
    yandex_disk_final_path VARCHAR(1000),
    file_hash VARCHAR(64),
    telegram_chat_id VARCHAR(64),
    telegram_message_id VARCHAR(64),
    telegram_file_id VARCHAR(255),
    comment_ TEXT,
    created_by VARCHAR(100) NOT NULL DEFAULT 'hunttech_docs_bot'
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_had_flow_type ON hunttech_accounting_document(flow_type);
CREATE INDEX IF NOT EXISTS idx_had_status ON hunttech_accounting_document(status);
CREATE INDEX IF NOT EXISTS idx_had_create_ts ON hunttech_accounting_document(create_ts DESC);
CREATE INDEX IF NOT EXISTS idx_had_recognized_inn ON hunttech_accounting_document(recognized_inn);
CREATE INDEX IF NOT EXISTS idx_had_telegram_chat_id ON hunttech_accounting_document(telegram_chat_id);

-- hunttech_accounting_document_event table
CREATE TABLE IF NOT EXISTS hunttech_accounting_document_event (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version INTEGER NOT NULL DEFAULT 1,
    create_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    document_id UUID NOT NULL REFERENCES hunttech_accounting_document(id) ON DELETE CASCADE,
    event_type INTEGER NOT NULL,  -- 1=RECOGNIZED, 2=CONFIRMED, 3=SENT, 10=CONTRACTS_LAYOUT, 11=CONTRACTS_LAYOUT_FAILED, 12=ADVANCE_LAYOUT, 20=MANUAL_EDIT, 99=OTHER
    message TEXT,
    created_by VARCHAR(100) NOT NULL DEFAULT 'hunttech_docs_bot'
);

-- Index for events by document
CREATE INDEX IF NOT EXISTS idx_hade_document_id ON hunttech_accounting_document_event(document_id);
CREATE INDEX IF NOT EXISTS idx_hade_event_at ON hunttech_accounting_document_event(event_at DESC);
CREATE INDEX IF NOT EXISTS idx_hade_event_type ON hunttech_accounting_document_event(event_type);