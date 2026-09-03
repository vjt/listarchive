# listarchive

A schema and a renderer for recovered mailing-list archives.

Two archives rescued from the Wayback Machine — the LISTSERV lists of
`listserv.nic.it` and the MHonArc lists of `ecn.org` — are different websites
from different decades built by different software, and yet the work of turning
either of them into something readable is the same work. This repository is
that shared half:

* [`schema.sql`](schema.sql) — the SQLite database an importer must produce,
  with [`SCHEMA.md`](SCHEMA.md) explaining what the columns mean;
* [`render.py`](render.py) — database in, static site out;
* [`mhonarc_import.py`](mhonarc_import.py) — importer for MHonArc pages, which
  carry real `Message-Id:`/`References:` headers and therefore real threading;
* [`archive.example.toml`](archive.example.toml) — everything that differs
  between one archive and the next.

The scraping stays out. Each archive knows its own CDX query, its own URL
shapes and its own rate limits, and hard-won knowledge about *one* Wayback
collection does not generalise into a library.

## Using it

As a submodule of an archive repository, which supplies the pages, the config
and (unless it is MHonArc) its own importer:

```sh
git submodule add https://github.com/vjt/listarchive.git lib
sqlite3 archive.db < lib/schema.sql
python3 lib/mhonarc_import.py --db archive.db --pages pages --list cyber-rights
python3 lib/render.py --db archive.db --config archive.toml --out site
```

`archive.mk` is a Makefile fragment with the usual targets already wired
(`db`, `site`, `search`, `serve`, `check`, `clean`); an archive repository
normally has a three-line `Makefile` that sets `LIST`/`DB`/`IMPORTER` and
includes it. The `Makefile` here does exactly that, so the repository is also
runnable on its own.

## What the renderer produces

Static HTML, no build step, no framework: an index of lists, one page per
period, one page per thread, one page per author, plus `sitemap.xml` and
`robots.txt`. Search is [Pagefind](https://pagefind.app/), run over the thread
pages only — indexing the tables of contents as well drowns every query in
tables of contents.

Bodies are rendered as the archive stored them, minus anything that executes.
The `<font>` tags and the broken tables of 1998 are the record; a `<script>`
that survived two decades of snapshots is not.

## The parts that are opinions

* **Real threading beats drawn threading.** Where `Message-Id:`/`References:`
  survive, threads are the reply graph the senders actually made, and they span
  months. Where only Previous/Next links survive, a thread stops where the
  server's links stopped. Both are supported; only one is right when both are
  available. See *The two threading models* in `SCHEMA.md`.
* **Dates from the headers, ranges from the dates.** Archive file names lie:
  one message with an epoch-zero `Date:` is enough to name a whole archive
  "January 1970", and reading the span of a list off its file names dates the
  founding of the Italian Naming Authority to 1970.
* **Unknown config keys are a hard error.** A misspelled `thread_model` that
  quietly fell back to a default would render the wrong threading and look
  fine.

## Licence

TBD.
