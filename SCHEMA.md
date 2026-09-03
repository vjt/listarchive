# The database an importer has to produce

`render.py` reads one SQLite file and writes a static site. The canonical
definition of that file is [`schema.sql`](schema.sql) — this document says what
the columns *mean*, which of them an importer may leave alone, and where the
two threading models differ.

Apply the schema, fill the tables, run the renderer:

```sh
sqlite3 archive.db < schema.sql
python3 my_import.py --db archive.db          # yours
python3 render.py --db archive.db --config archive.toml --out site
```

## Rules that hold everywhere

* **An empty header is an absent header.** Normalise `''` to `NULL` on the way
  in. Otherwise every later `IS NULL` lies and the renderer prints empty
  `From:` lines instead of skipping them.
* **`headers_json` is the document, the columns are the convenience.** Store
  every header verbatim in it, including the ones with no column of their own.
  When a later question needs a header nobody thought of, it is already there.
* **Dates are believed, not trusted.** `posted_at` is ISO 8601
  (`YYYY-MM-DD HH:MM:SS`) or `NULL`; keep the original string in `date_raw`. A
  1970 or 2038 date is a broken `Date:` header, not a fact about the list, and
  the renderer drops such messages out of its date ranges.
* **`local_id` is identity, not order.** It is the archive's own number for a
  message inside its period — MHonArc's `msg00042` → `42`, LISTSERV's `P=157` →
  `157`. Ordering, where the archive gave one, goes in `seq`.

## Tables

### `lists`

One row per mailing list. `name` is the slug used in URLs; `message_count`,
`first_at` and `last_at` are summaries the importer fills at the end, computed
over the *believable* dates.

### `periods`

The unit the original archive filed messages under: a calendar month for
MHonArc (`199812`), an archive file for LISTSERV (`ind0001`, sometimes a whole
year). `year`/`month` are what the renderer sorts and labels by — leave `month`
`NULL` for a yearly archive, and set both from what the *messages* say rather
than from the file name (see the `ind7001` note in `render.py`: one message
with an epoch-zero date named a whole archive "January 1970").

### `messages`

Required: `list`, `period`, `local_id`, `body_html`, `body_text`.
`body_html` is the body as the archive stored it, tags and all — the broken
tables and `<font>` soup are part of the record, and the renderer sanitises
scripts out at render time rather than at import. `body_text` is the same body
with tags stripped, for search.

`font` says which rendering of the page the body came from: `pre` (fixed-width,
the usual) or `prop`. Archives that served both keep the distinction because
ASCII art and quoted diffs need the fixed-width one.

Everything else — `seq`, `posted_at`, `date_raw`, `from_name`, `from_addr`,
`subject`, `reply_to`, `sender`, `x_to`, `content_type` — is rendered when
present and skipped when `NULL`.

`UNIQUE (list, period, local_id)` makes the importer idempotent: re-running it
over the same pages updates rather than duplicates.

### `threads`

One row per reconstructed discussion, filled by the importer *after* threading
is resolved. `period` is the period of the thread's *first* message, which for
model A can differ from the period of its later messages.

### `messages_fts`

An FTS5 external-content index over `subject`, `from_name`, `from_addr` and
`body_text`. The renderer never reads it — the published site searches through
Pagefind — but `make check` and any local `sqlite3` session do. Populate it
after the import:

```sql
INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
```

## The two threading models

Fill in whichever one the archive supports, and say which in `archive.toml`
(`thread_model`). They are not alternatives of equal worth: **model A is the
threading the senders meant, model B is the threading the server drew.**

### Model A — real message ids (`thread_model = "references"`)

Available whenever the archive kept the headers. MHonArc, for instance, writes
them into HTML comments at the top of every page:

```html
<!--X-Message-Id: 3.0.5.32.19981117140115.007e1100@mail.olografix.org -->
<!--X-Reference: 199811/msg00238.html -->
```

Fill `message_id` (angle brackets stripped), `in_reply_to`, and
`references_ids` as a JSON array oldest-first. Then resolve `parent_id`:

1. `in_reply_to`, if it matches a known `message_id`;
2. otherwise the **last** entry of `references_ids` that does;
3. otherwise walk `references_ids` backwards to the first that does;
4. otherwise `NULL` — the message starts a thread.

A thread is then a connected component of the `parent_id` graph. It spans
periods, and the renderer indents each reply under its parent (`.d1`…`.d6`,
collapsed to a flat list on narrow screens).

Two things to guard: a message that quotes an id it also claims as its own
(self-parenting — refuse it), and a `References:` chain that loops after a
mailer rewrote ids (walk with a `seen` set, not with recursion).

### Model B — the links the server drew (`thread_model = "chain"`)

LISTSERV's `wa` pages carry no ids, only navigation: previous and next in the
topic, previous and next by author, previous and next in the file. Store those
as `local_id`s within the same `(list, period)`: `thread_prev`, `thread_next`,
`author_prev`, `author_next`, `prev_local`, `next_local`.

A thread is a connected component of the `thread_prev`/`thread_next` links —
which stops at the edge of the archive file, because that is as far as the
server's links went. Set `cross_period = true` in the config so the renderer
offers, at the foot of a thread, the other threads with the same subject in
other periods: for this model that list is the only way across the boundary.

## What the renderer actually reads

If you are writing an importer, these are the columns whose absence you will
notice, in the order the renderer asks for them:

| Query | Columns |
|---|---|
| lists index | `lists`: `name`, `display_name`, `message_count`, `first_at`, `last_at` |
| period index | `periods`: `list`, `period`, `label`, `year`, `month`, `message_count` |
| message skeleton | `messages`: `id`, `list`, `period`, `local_id`, `seq`, `posted_at`, `date_raw`, `from_name`, `from_addr`, `subject`, `thread_id`, `parent_id`, `thread_prev`, `thread_next`, `author_prev`, `author_next` |
| bodies | `messages`: `id`, `body_html`, `body_text`, `headers_json`, `content_type`, `font`, `in_reply_to`, `x_to`, `sender`, `reply_to` |
| threads | `threads`: `id`, `list`, `period`, `subject`, `message_count`, `first_at`, `last_at` |

Anything not in this table is stored for the archaeology, not for the page.
