-- The database an importer must produce for `render.py` to read.
--
-- One row per message, whatever software served the archive.  LISTSERV's `wa`
-- pages and MHonArc's `msgNNNNN.html` carry different metadata, so the columns
-- split into three groups:
--
--   * required  — the renderer refuses to work without them;
--   * threading — fill in whichever of the two models the archive supports
--                 (see SCHEMA.md: real `References:` beats a chain of links);
--   * optional  — rendered when present, ignored when NULL.
--
-- An empty header is an absent header: normalise '' to NULL on the way in, or
-- every later `IS NULL` lies.

PRAGMA journal_mode = WAL;

-- The archives themselves.  One row per mailing list.
CREATE TABLE IF NOT EXISTS lists (
  name          TEXT PRIMARY KEY,          -- 'cyber-rights', slug used in URLs
  display_name  TEXT,                      -- 'cyber-rights', as the archive titles it
  message_count INTEGER NOT NULL DEFAULT 0,
  first_at      TEXT,                      -- ISO 8601, from the believable dates
  last_at       TEXT
);

-- The unit the original archive filed messages under: a month for MHonArc
-- (`199812`), an archive file for LISTSERV (`ind0001`, sometimes a whole year).
-- `year`/`month` are what the renderer sorts and labels by; leave `month` NULL
-- for a yearly archive.
CREATE TABLE IF NOT EXISTS periods (
  list          TEXT NOT NULL REFERENCES lists(name),
  period        TEXT NOT NULL,             -- '199812' | 'ind0001'
  label         TEXT,                      -- what the archive called it
  year          INTEGER,
  month         INTEGER,
  message_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (list, period)
);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,

  -- ---- required ---------------------------------------------------------
  list          TEXT NOT NULL,
  period        TEXT NOT NULL,
  local_id      INTEGER NOT NULL,          -- the archive's own id inside the period:
                                           -- MHonArc's msg00042 -> 42, LISTSERV's P=157
                                           -- -> 157.  Identity, not ordering.
  body_html     TEXT NOT NULL,             -- the body as the archive stored it
  body_text     TEXT NOT NULL,             -- same body, tags stripped, for search

  -- ---- what the headers said --------------------------------------------
  seq           INTEGER,                   -- position inside the period, when the
                                           -- archive numbered its messages
  posted_at     TEXT,                      -- ISO 8601 'YYYY-MM-DD HH:MM:SS' or NULL
  date_raw      TEXT,                      -- the Date: header verbatim
  from_name     TEXT,
  from_addr     TEXT,
  subject       TEXT,
  reply_to      TEXT,
  sender        TEXT,
  x_to          TEXT,
  content_type  TEXT,
  headers_json  TEXT,                      -- every header verbatim, JSON object.
                                           -- The columns are the convenience; this
                                           -- is the document.
  font          TEXT NOT NULL DEFAULT 'pre',  -- 'pre' | 'prop': which rendering of
                                           -- the page this body came from

  -- ---- threading, model A: real message ids (MHonArc, mbox, anything that
  --      kept the headers).  `parent_id` is resolved by the importer.
  message_id     TEXT,                     -- Message-Id: verbatim, angle brackets off
  in_reply_to    TEXT,                     -- In-Reply-To: single id
  references_ids TEXT,                     -- References: JSON array, oldest first
  parent_id      INTEGER REFERENCES messages(id),

  -- ---- threading, model B: the links the server drew (LISTSERV).  Values are
  --      `local_id`s inside the same (list, period).
  thread_prev   INTEGER,
  thread_next   INTEGER,
  author_prev   INTEGER,
  author_next   INTEGER,
  prev_local    INTEGER,                   -- plain previous/next in the archive
  next_local    INTEGER,

  thread_id     INTEGER,                   -- filled once threading is resolved

  UNIQUE (list, period, local_id)
);

CREATE INDEX IF NOT EXISTS messages_list_date ON messages(list, posted_at);
CREATE INDEX IF NOT EXISTS messages_addr      ON messages(from_addr);
CREATE INDEX IF NOT EXISTS messages_thread    ON messages(thread_id, posted_at);
CREATE INDEX IF NOT EXISTS messages_subject   ON messages(subject);
CREATE INDEX IF NOT EXISTS messages_msgid     ON messages(message_id);
CREATE INDEX IF NOT EXISTS messages_parent    ON messages(parent_id);

-- A thread is whatever the chosen model says it is: a connected component of
-- the reply graph (model A) or of the topic links inside one period (model B).
CREATE TABLE IF NOT EXISTS threads (
  id            INTEGER PRIMARY KEY,
  list          TEXT NOT NULL,
  period        TEXT NOT NULL,             -- the period of the thread's first message
  subject       TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  first_at      TEXT,
  last_at       TEXT
);

-- Search index.  The renderer does not read it; `make search` and any local
-- `sqlite3` query do.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, from_name, from_addr, body_text,
  content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
