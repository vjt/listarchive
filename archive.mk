# Shared build rules for an archive repository.
#
# An archive repository sets a handful of variables and includes this file:
#
#   LIB      ?= lib                  where this repository is checked out
#   LIST     := cyber-rights         slug of the list being imported
#   IMPORTER := $(LIB)/mhonarc_import.py
#   include $(LIB)/archive.mk
#
# Targets:
#   make            db + site + search index
#   make db         pages/ -> $(DB)
#   make site       $(DB) -> $(SITE)/
#   make search     $(SITE)/ -> $(SITE)/pagefind/
#   make serve      browse it at http://localhost:$(PORT)/
#   make check      the numbers the README quotes
#   make clean      throw away both artifacts
#
# `pages/` is the archive and is versioned. The database and the site are not:
# they come back from the pages in a couple of minutes, so a clone plus `make`
# gives a browsable copy of the whole thing.

PYTHON   ?= python3
PAGEFIND ?= ./bin/pagefind
LIB      ?= lib
DB       ?= archive.db
SITE     ?= site
PAGES    ?= pages
CONFIG   ?= archive.toml
SCHEMA   ?= $(LIB)/schema.sql
RENDERER ?= $(LIB)/render.py
IMPORTER ?= $(LIB)/mhonarc_import.py
PORT     ?= 8000

ifndef LIST
$(error set LIST to the slug of the list being imported, e.g. LIST := cyber-rights)
endif

.PHONY: all db site search serve check clean

all: search

db: $(DB)

# The importer walks every page, so the stamp is the pages directory itself:
# a new month appearing is a reason to rebuild, a page being rewritten in place
# is not (and does not happen — `pages/` is write-once archaeology).
$(DB): $(IMPORTER) $(SCHEMA) $(shell find $(PAGES) -maxdepth 1 -type d 2>/dev/null)
	$(PYTHON) $(IMPORTER) --db $(DB) --pages $(PAGES) --list $(LIST) --schema $(SCHEMA)

site: $(SITE)/index.html

$(SITE)/index.html: $(RENDERER) $(CONFIG) $(DB)
	$(PYTHON) $(RENDERER) --db $(DB) --config $(CONFIG) --out $(SITE)

# Pagefind indexes only what carries `data-pagefind-body`, i.e. the thread
# pages: the period and author indexes would otherwise drown every search in
# tables of contents.
search: $(SITE)/pagefind/pagefind.js

# PAGEFIND can be a path (`./bin/pagefind`, the default) or a command — CI sets
# `PAGEFIND="npx -y pagefind"` and there is no binary to check for, so the test
# is "does it run", not "is it an executable file".
$(SITE)/pagefind/pagefind.js: $(SITE)/index.html
	@$(PAGEFIND) --version >/dev/null 2>&1 || { \
	  echo "$(PAGEFIND) does not run — either drop a release binary from"; \
	  echo "https://github.com/CloudCannon/pagefind into bin/, or build with"; \
	  echo "  make search PAGEFIND='npx -y pagefind'"; exit 1; }
	$(PAGEFIND) --site $(SITE) --output-subdir pagefind

serve: all
	@echo "http://localhost:$(PORT)/"
	@cd $(SITE) && $(PYTHON) -m http.server $(PORT)

# The numbers a README quotes. Re-run after any importer change: a threading
# bug shows up here as a thread count that moved, long before it shows up as a
# page that reads wrong.
check: $(DB)
	@sqlite3 $(DB) \
	  "SELECT 'messages', count(*) FROM messages;" \
	  "SELECT 'threads', count(*) FROM threads;" \
	  "SELECT 'senders', count(DISTINCT from_addr) FROM messages;" \
	  "SELECT 'orphan parents', count(*) FROM messages m WHERE m.parent_id IS NOT NULL \
	     AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.id = m.parent_id);" \
	  "SELECT 'no date', count(*) FROM messages WHERE posted_at IS NULL;" \
	  "SELECT 'no thread', count(*) FROM messages WHERE thread_id IS NULL;"

clean:
	rm -rf $(DB) $(DB)-wal $(DB)-shm $(SITE)
