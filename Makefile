# Standalone build, for working on the renderer itself.
#
# An archive repository does not use this file: it writes its own three-line
# Makefile setting LIST / DB / IMPORTER and includes `archive.mk` from here.
# This one just points the shared rules at the current directory, so that
# `make` inside a checkout of listarchive builds whatever pages/ holds.

LIB      := .
LIST     ?= cyber-rights
CONFIG   ?= archive.toml

include archive.mk
