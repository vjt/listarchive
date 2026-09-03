#!/usr/bin/env python3
"""Render an archive database (see SCHEMA.md) into a static mailing-list reader.

The archive is plain-text mail from 1990-2007: no forum markup, no attachments
that survived, and readers who quote each other three levels deep.  The whole
design follows from that.

  * A message body is text, not HTML.  What LISTSERV stored is the text with
    entities escaped and the odd `<a>` it auto-linked; everything else here
    treats it as lines and rebuilds the structure from the `>` prefixes.
  * Quoting is the content.  A reader that shows `> > &gt; ...` as flat grey
    text throws away the argument being had.  `quote_tree()` reconstructs the
    nesting, `attribution()` reads the "X ha scritto:" line when there is one
    and `who_was_quoted()` finds the quoted words in an earlier message of the
    thread when there is not, and anything long or deep collapses behind a
    `<details>` so the reply is what you read first.
  * Threading depends on what the archive kept.  With real `References:`
    headers (`thread_model = "references"`) a thread is the reply tree, it may
    span months and the page indents it.  With nothing but the server's own
    Previous/Next links (`thread_model = "chain"`, LISTSERV) a thread stops at
    the archive-file boundary; rather than guess a merge, each thread page
    links the threads with the same normalised subject in the other periods.

Everything archive-specific — the name in the header, the blurb, the footer,
which permalinks to rewrite, which threading model — comes from a TOML config
(`archive.toml`, see `archive.example.toml`), so the same renderer serves more
than one archive.

Output is static HTML, one `index.html` per directory, relative links only:
`wget -r` walks the lot and the only JavaScript on the site is the search page.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import time
import tomllib
import unicodedata
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

# ------------------------------------------------------------------- config

@dataclass
class Config:
    """What differs between two archives rendered by the same code.

    Defaults describe a generic archive; every real one ships an
    `archive.toml`.  Unknown keys are rejected rather than ignored: a typo in
    `thread_model` silently rendering the wrong threading is the kind of bug
    that survives to production.
    """

    name: str = "Archivio"
    lang: str = "it"
    base_url: str = "https://example.invalid/"
    tagline: str = ""                 # <h1> of the index; defaults to `name`
    intro_html: str = ""              # the panel under it
    footer_html: str = ""
    thread_model: str = "chain"       # 'chain' | 'references'
    link_style: str = "none"          # 'listserv' | 'mhonarc' | 'none'
    original_host: str = ""           # host the archive was served from
    cross_period: bool = True         # link threads with the same subject elsewhere
    cross_period_note: str = ""

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        if path is None:
            return cls()
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        flat: dict = {}
        for section in ("site", "archive"):
            flat.update(raw.get(section) or {})
        flat.update({k: v for k, v in raw.items() if not isinstance(v, dict)})
        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(flat) - known
        if unknown:
            raise SystemExit(f"{path}: unknown config keys: {', '.join(sorted(unknown))}")
        cfg = cls(**flat)
        if cfg.thread_model not in ("chain", "references"):
            raise SystemExit(f"{path}: thread_model must be 'chain' or 'references'")
        if cfg.link_style not in ("listserv", "mhonarc", "none"):
            raise SystemExit(f"{path}: link_style must be 'listserv', 'mhonarc' or 'none'")
        return cfg


# --------------------------------------------------------------- sanitising

# Bodies are someone else's markup from 1998.  The pass-through is deliberate —
# the `<font>` tags and broken tables are part of the record — but anything that
# executes goes, including the unclosed variants an Archive cut leaves behind.
RE_BAD_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed|applet)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
RE_BAD_OPEN = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|applet)\b[^>]*>", re.I)
RE_ON_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
RE_JS_URL = re.compile(r"((?:href|src)\s*=\s*[\"']?)\s*javascript:[^\"'>\s]*", re.I)


def sanitise(body: str) -> str:
    body = RE_BAD_BLOCK.sub("", body)
    body = RE_BAD_OPEN.sub("", body)
    body = RE_ON_ATTR.sub("", body)
    return RE_JS_URL.sub(r"\1#", body)


# ------------------------------------------------------------- line breaking

# Most bodies keep real newlines.  A minority (1104 of 24118) were stored with
# `<br>`/`<p>` instead, and without this they arrive as one enormous line and
# every quote level in them is invisible.
RE_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
RE_P_CLOSE = re.compile(r"<\s*/\s*p\s*>", re.I)
RE_P_OPEN = re.compile(r"<\s*p\b[^>]*>", re.I)
RE_TAG_SPLIT = re.compile(r"(<[^>]*>)")


def to_lines(body: str) -> list[str]:
    body = RE_BR.sub("\n", body)
    body = RE_P_CLOSE.sub("\n", body)
    body = RE_P_OPEN.sub("\n\n", body)
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# ------------------------------------------------------------ quote analysis

# One quote marker: up to three spaces, then `>` (raw or escaped).  An optional
# one-to-three letter prefix before the *first* marker catches the `AN>` style
# some of these people used.  `->` and `=>` are not markers: both start with a
# character this refuses.
RE_MARK = re.compile(r"^[ \t]{0,3}(?:&gt;|>)")
RE_INITIALS = re.compile(r"^[ \t]{0,4}[A-Za-z]{1,3}(?=[ \t]{0,2}(?:&gt;|>))")


def strip_quotes(line: str) -> tuple[int, str]:
    """Return (depth, remainder) for one line."""
    m = RE_INITIALS.match(line)
    s = line[m.end():] if m else line
    depth = 0
    while True:
        m = RE_MARK.match(s)
        if not m:
            break
        depth += 1
        s = s[m.end():]
        if s[:1] == " ":
            s = s[1:]
    return depth, s


def depths(lines: list[str]) -> list[tuple[int, str]]:
    """Depth-tag every line, resolving blank lines to the shallower neighbour.

    A blank line between a quote and the reply belongs to the reply, or the
    quote block swallows the gap and the nesting closes one paragraph too late.
    """
    raw: list[tuple[int | None, str]] = []
    for line in lines:
        d, rest = strip_quotes(line)
        blank = not rest.strip() and d == 0
        raw.append((None if blank else d, rest))

    out: list[tuple[int, str]] = []
    for i, (d, rest) in enumerate(raw):
        if d is not None:
            out.append((d, rest))
            continue
        prev = next((raw[j][0] for j in range(i - 1, -1, -1) if raw[j][0] is not None), None)
        nxt = next((raw[j][0] for j in range(i + 1, len(raw)) if raw[j][0] is not None), None)
        if prev is None and nxt is None:
            out.append((0, rest))
        elif prev is None:
            out.append((nxt, rest))
        elif nxt is None:
            out.append((prev, rest))
        else:
            out.append((min(prev, nxt), rest))
    return out


def quote_tree(lines: list[tuple[int, str]], base: int = 0) -> list:
    """Group depth-tagged lines into a nested tree of text and quote blocks."""
    out: list = []
    i = 0
    while i < len(lines):
        if lines[i][0] <= base:
            buf = []
            while i < len(lines) and lines[i][0] <= base:
                buf.append(lines[i][1])
                i += 1
            out.append(("t", buf))
        else:
            j = i
            while j < len(lines) and lines[j][0] > base:
                j += 1
            out.append(("q", base + 1, quote_tree(lines[i:j], base + 1)))
            i = j
    return out


def count_lines(node) -> int:
    if node[0] == "t":
        return sum(1 for l in node[1] if l.strip())
    return sum(count_lines(c) for c in node[2])


# ---------------------------------------------------------- who was quoted

# "Alfredo E. Cotroneo wrote:", "Il 4 gennaio, Tizio ha scritto:", "Maurizio
# Parodi wrote:" — the line immediately above a quote, when there is one.
RE_ATTR_VERB = re.compile(
    r"^\s*(?P<who>.{0,150}?)\s*(?:ha\s+scritto|hai\s+scritto|scriveva|scrive|"
    r"wrote|writes|said|dice|diceva)\s*[:;]?\s*$", re.I)
RE_ATTR_DATE = re.compile(
    r"^\s*(?:in\s+data|il\s+giorno|on|at|alle\s+ore)\b.{3,180}:\s*$", re.I)
RE_TAGS = re.compile(r"<[^>]{1,400}>")


def plain(s: str) -> str:
    return html.unescape(RE_TAGS.sub(" ", s or "")).strip()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", plain(s)).lower()


def attribution(text_block: list[str]) -> tuple[str | None, list[str]]:
    """Pull an attribution line off the end of a text block.

    Returns (cite, block-without-it).  Only the last non-blank line is
    considered: an attribution further up belongs to a quote already rendered.
    """
    idx = next((i for i in range(len(text_block) - 1, -1, -1)
                if text_block[i].strip()), None)
    if idx is None:
        return None, text_block
    line = plain(text_block[idx])
    if not line or len(line) > 200:
        return None, text_block
    who = None
    m = RE_ATTR_VERB.match(line)
    if m:
        who = m.group("who").strip(" ,;-—>«»\"'") or line
    elif RE_ATTR_DATE.match(line):
        who = line.rstrip(":").strip()
    if not who:
        return None, text_block
    return who, text_block[:idx] + text_block[idx + 1:]


def quote_sample(node, limit: int = 6) -> list[str]:
    """A few substantial lines out of a quote block, normalised for matching."""
    lines: list[str] = []

    def walk(n):
        if len(lines) >= limit:
            return
        if n[0] == "t":
            for l in n[1]:
                s = norm(l)
                if len(s) >= 25:
                    lines.append(s)
                    if len(lines) >= limit:
                        return
        else:
            for c in n[2]:
                walk(c)

    walk(node)
    return lines


def who_was_quoted(node, earlier: list[tuple[str, str]]) -> str | None:
    """Find which earlier message in the thread this block is quoting.

    There is no `References:` header anywhere in this archive, so the only
    honest way to say "X is answering Y" is to look for the quoted words in Y.
    Whitespace is collapsed on both sides first: a quoted line is a contiguous
    run of the original's words however the quoter's mailer re-wrapped it.

    Candidates are walked newest-first — people quote the message they are
    replying to far more often than the one before it — and one long match or
    two shorter ones are required, so a shared signature does not attribute the
    whole thread to whoever wrote it.
    """
    sample = quote_sample(node)
    if not sample:
        return None
    for cite, text in earlier:
        if not text:
            continue
        hits = [s for s in sample if s in text]
        if len(hits) >= 2 or (hits and len(hits[0]) >= 45):
            return cite
    return None


# ---------------------------------------------------------------- signatures

RE_SIGMARK = re.compile(r"^--\s?$")


def split_signature(lines: list[tuple[int, str]]) -> tuple[list, list]:
    """Cut a trailing `-- ` signature off the body, at depth 0 only."""
    for i in range(len(lines) - 1, max(-1, len(lines) - 40), -1):
        d, text = lines[i]
        if d == 0 and RE_SIGMARK.match(text):
            tail = lines[i + 1:]
            if 0 < len(tail) <= 20 and all(d2 == 0 for d2, _ in tail):
                return lines[:i], tail
    return lines, []


# ------------------------------------------------------------------ linking

RE_URL = re.compile(r"(?<![\w@.])((?:https?|ftp)://[^\s<>\"']{4,300}?)(?=[\s<>\"'.,;:!?)\]]|$)")


def linkify(fragment: str, local=None) -> str:
    """Autolink bare URLs, skipping anything already inside a tag or an <a>.

    `local(url)` gets first refusal: in these archives people cite each other by
    pasting the archive's own URL into the mail, and that citation is worth more
    pointing at the message we hold than at a host that stopped answering in
    2003.
    """
    def one(m: re.Match[str]) -> str:
        url = m.group(1)
        here = local(url) if local else None
        if here:
            return (f'<a href="{here}" class="in">{url}</a>')
        return (f'<a href="{html.escape(url, quote=True)}" '
                f'rel="nofollow noopener">{url}</a>')

    parts = RE_TAG_SPLIT.split(fragment)
    depth = 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            low = part.lower()
            if low.startswith("<a "):
                depth += 1
            elif low.startswith("</a"):
                depth = max(0, depth - 1)
            continue
        if depth:
            continue
        parts[i] = RE_URL.sub(one, part)
    return "".join(parts)


RE_WA_HREF = re.compile(r'href\s*=\s*"([^"]*cgi-bin/wa\?[^"]*)"', re.I)
RE_WA_PARAM = re.compile(r"[?&](?:amp;)?([A-Za-z0-9]+)=([^&\"]*)")
RE_HREF = re.compile(r'href\s*=\s*"([^"]*)"', re.I)

# MHonArc's own permalink: `/<list>/<yyyymm>/msg00042.html`, or a bare
# `msg00042.html` inside the same month.  The list name is whatever directory
# sits above the period, so the pattern stays honest about archives served from
# a path other than `/lists/`.
RE_MH_PATH = re.compile(
    r"(?:^|/)(?P<list>[A-Za-z0-9._~-]+)/(?P<period>\d{6})/msg(?P<n>\d+)\.html$", re.I)
RE_MH_BARE = re.compile(r"^(?:\./)?msg(?P<n>\d+)\.html$", re.I)


def wayback(href: str) -> str:
    # The Wayback wants an absolute URL after the timestamp. A bare host —
    # which is what `original_host` is — makes it guess, and the link that used
    # to read `…/web/2007/http://listserv.nic.it/cgi-bin/wa?…` came out as
    # `…/web/2007/listserv.nic.it/…` when the host became configurable. These
    # archives were all served over plain HTTP, which is also how the Archive
    # recorded them.
    if not href.lower().startswith(("http://", "https://")):
        href = "http://" + href.lstrip("/")
    return f'href="https://web.archive.org/web/2007/{html.escape(href, quote=True)}" class="in gone"'


def local_target(url: str, resolve, cfg: "Config", ctx: tuple[str, str]) -> str | None:
    """Resolve one URL from a message body to a page on this site, or None."""
    if cfg.link_style != "mhonarc":
        return None
    href = html.unescape(url).strip().split("#")[0]
    m = RE_MH_BARE.match(href)
    if m:                                    # relative: same list, same month
        lst, period = ctx
    else:
        m = RE_MH_PATH.search(href)
        if not m:
            return None
        lst, period = m.group("list").lower(), m.group("period")
    try:
        return resolve(lst, period, int(m.group("n")))
    except ValueError:
        return None


def rewrite_internal(body: str, resolve, cfg: "Config", ctx: tuple[str, str]) -> str:
    """Point the archive's own permalinks back at this site.

    These are the era's permalinks: people cited each other by URL.  Left alone
    they are 404s on a host that no longer exists.
    """
    def wa(m: re.Match[str]) -> str:
        href = html.unescape(m.group(1)).strip()
        params = {k.upper(): unquote(v) for k, v in RE_WA_PARAM.findall(href)}
        lst = (params.get("L") or "").lower()
        period = params.get("A2") or params.get("A3") or ""
        p = params.get("P") or ""
        target = None
        if lst and period and p:
            try:
                target = resolve(lst, period, int(p))
            except ValueError:
                target = None
        if target:
            return f'href="{target}" class="in"'
        # Not a message we hold — usually a MIME part (`A3=…&B=<boundary>`),
        # which was never crawled.  Left alone these resolve against whatever
        # host serves the site and 404 there; send them to the Wayback instead,
        # which is where the rest of this archive came from.
        if href.startswith("/"):
            if not cfg.original_host:
                return m.group(0)
            href = cfg.original_host.rstrip("/") + href
        elif not href.lower().startswith("http"):
            return m.group(0)
        return wayback(href)

    def mh(m: re.Match[str]) -> str:
        href = html.unescape(m.group(1)).strip()
        target = local_target(href, resolve, cfg, ctx)
        if target:
            return f'href="{target}" class="in"'
        if RE_MH_BARE.match(href) or RE_MH_PATH.search(href):
            # A message of this archive that never made it into the crawl.
            if not href.lower().startswith("http"):
                if not cfg.original_host:
                    return m.group(0)
                base = cfg.original_host.rstrip("/")
                href = f"{base}/{ctx[0]}/{ctx[1]}/{href.lstrip('./')}" \
                    if RE_MH_BARE.match(href) else f"{base}/{href.lstrip('/')}"
            return wayback(href)
        return m.group(0)

    if cfg.link_style == "listserv":
        body = RE_WA_HREF.sub(wa, body)
    elif cfg.link_style == "mhonarc":
        body = RE_HREF.sub(mh, body)
    return RE_ROOTREL.sub(defang, body)


# A root-relative `href="/…"` written in 2001 was relative to the sender's host,
# not ours: left alone it silently points at this site's root. Drop the href and
# keep the URL in the title — the record loses nothing, the reader is not lied to.
RE_ROOTREL = re.compile(r'<a\s([^>]*?)href\s*=\s*"(/[^"]*)"([^>]*)>', re.I)


def defang(m: re.Match[str]) -> str:
    return (f'<a class="dead" title="collegamento originale: '
            f'{html.escape(m.group(2), quote=True)}">')


# ------------------------------------------------------------------- render

QCLASS = 6  # quote colours cycle after six levels; nobody quoted deeper usefully
COLLAPSE_LINES = 9


def block_html(node, cite: str | None = None, local=None) -> str:
    if node[0] == "t":
        lines = node[1]
        while lines and not lines[0].strip():
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        if not lines:
            return ""
        return f'<div class="txt">{linkify(chr(10).join(lines), local)}</div>'

    _, level, kids = node
    inner = "".join(block_html(k, None, local) for k in kids)
    if not inner:
        return ""
    cls = f"q q{((level - 1) % QCLASS) + 1}"
    head = f'<cite>{cite}</cite>' if cite else ""
    n = sum(count_lines(k) for k in kids)
    if n > COLLAPSE_LINES or level >= 3:
        label = cite or "citazione"
        return (f'<details class="{cls}"><summary>{label} '
                f'<span class="n">— {n} righe citate</span></summary>'
                f'{inner}</details>')
    return f'<blockquote class="{cls}">{head}{inner}</blockquote>'


def body_html(raw: str, earlier: list[tuple[str, str]], resolve,
              cfg: "Config", ctx: tuple[str, str]) -> str:
    body = sanitise(raw or "")
    body = rewrite_internal(body, resolve, cfg, ctx)
    local = (lambda u: local_target(u, resolve, cfg, ctx)) \
        if cfg.link_style == "mhonarc" else None
    lines = depths(to_lines(body))
    lines, sig = split_signature(lines)
    tree = quote_tree(lines)

    out: list[str] = []
    for i, node in enumerate(tree):
        if node[0] == "t":
            # Hold the attribution line back only if a quote actually follows.
            if i + 1 < len(tree) and tree[i + 1][0] == "q":
                cite, rest = attribution(node[1])
                tree[i + 1] = tree[i + 1] + (cite,)  # stash for the next round
                out.append(block_html(("t", rest), None, local))
            else:
                out.append(block_html(node, None, local))
            continue
        cite = node[3] if len(node) > 3 else None
        if not cite:
            cite = who_was_quoted(node, earlier)
        out.append(block_html(node[:3], html.escape(cite) if cite else None, local))

    if sig:
        text = "\n".join(t for _, t in sig).strip("\n")
        if text.strip():
            out.append(f'<div class="sig">{linkify(text, local)}</div>')
    return "".join(out) or '<div class="txt empty">(messaggio vuoto)</div>'


# ------------------------------------------------------------------ helpers

def esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def slug(s: str | None, fallback: str = "msg") -> str:
    s = re.sub(r"^(?:\s*(?:re|r|fwd|fw|i)\s*[:\]]\s*)+", "", (s or ""), flags=re.I)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return (s[:60].rstrip("-") or fallback)


RE_RE = re.compile(r"^(?:\s*(?:re|r|fwd|fw|aw|antw)\s*[:\]]\s*)+", re.I)
RE_TAGPFX = re.compile(r"^\s*\[[^\]]{1,30}\]\s*")


def subj_key(s: str | None) -> str:
    s = s or ""
    prev = None
    while prev != s:
        prev = s
        s = RE_RE.sub("", s)
        s = RE_TAGPFX.sub("", s)
    return re.sub(r"\s+", " ", s).strip().lower()


MONTHS = ("", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
          "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")


def parse_when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def fmt_when(dt: datetime | None) -> str:
    if not dt:
        return "data ignota"
    return f"{dt.day} {MONTHS[dt.month]} {dt.year}, {dt:%H:%M}"


def fmt_day(dt: datetime | None) -> str:
    return f"{dt.day} {MONTHS[dt.month]} {dt.year}" if dt else "data ignota"


def period_ym(period: str, year, month, label) -> tuple[int, int]:
    """(year, month) for a period, from the table when it is filled in.

    `ind9912` is December 1999; `ind99` is the whole of 1999 (25 of the 307
    periods are yearly).  A yearly archive sorts before January.
    """
    if year:
        return int(year), int(month or 0)
    if label and label.strip().isdigit():
        return int(label.strip()), 0
    digits = period[3:]
    if len(digits) >= 4:
        yy, mm = int(digits[:2]), int(digits[2:4])
    elif len(digits) == 2:
        yy, mm = int(digits), 0
    else:
        return 0, 0
    # Same pivot as the importer: two digits, no century, and nothing here is
    # from the future.
    y = 2000 + yy
    return (y - 100 if y > datetime.now().year + 1 else y), mm


def period_label(period: str, label, y: int, m: int) -> str:
    if m:
        return f"{MONTHS[m]} {y}"
    return str(y) if y else (label or period)


# --------------------------------------------------------------- page shell

CSS = """\
:root{
 --bg:#d9d5c8;--paper:#fffdf6;--fg:#16150f;--dim:#5f5a4d;--line:#a49c88;
 --bev-hi:#ffffff;--bev-lo:#8d846e;--acc:#000080;--vis:#551a8b;--hot:#8b0000;
 --bar:#000080;--bartx:#ffffff;--sub:#eae5d5;
 --q1:#1a4fa0;--q2:#1c6b3f;--q3:#8a4b00;--q4:#7a1f5c;--q5:#0f6b74;--q6:#6b3f1c}
@media(prefers-color-scheme:dark){:root{
 --bg:#1a1917;--paper:#201f1c;--fg:#e4e0d4;--dim:#9a9483;--line:#3c3931;
 --bev-hi:#4a463c;--bev-lo:#100f0d;--acc:#8fb6ff;--vis:#c39ee0;--hot:#ff8a80;
 --bar:#101a3a;--bartx:#dfe6ff;--sub:#26241f;
 --q1:#8fb6ff;--q2:#7fd6a2;--q3:#e0a45c;--q4:#e79ec9;--q5:#7fd4dc;--q6:#d4b48c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.5 Verdana,Geneva,"DejaVu Sans",sans-serif}
a{color:var(--acc)}a:visited{color:var(--vis)}
a:hover{color:var(--hot)}
.wrap{max-width:60rem;margin:0 auto;padding:0 .6rem 3rem}
header.top{background:var(--bar);color:var(--bartx);margin:0 -.6rem 1rem;
 padding:.55rem .8rem;border-bottom:2px solid var(--bev-lo);
 display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap}
header.top a{color:var(--bartx)}
header.top .site{font-weight:700;letter-spacing:.06em;text-transform:uppercase;
 font-size:.92rem}
header.top .crumb{font-size:.78rem;opacity:.85}
header.top .find a{text-decoration:underline;font-size:.8rem;white-space:nowrap}
h1{font-size:1.25rem;line-height:1.3;margin:.4rem 0 .2rem}
h2{font-size:1.02rem;margin:1.4rem 0 .4rem;border-bottom:1px solid var(--line);
 padding-bottom:.2rem}
h3{font-size:.92rem;margin:1rem 0 .3rem}
p.meta,.meta{font-size:.8rem;color:var(--dim)}
.panel{background:var(--paper);border:2px solid;
 border-color:var(--bev-hi) var(--bev-lo) var(--bev-lo) var(--bev-hi);
 padding:.7rem .8rem;margin:0 0 .9rem}
.sunk{border-color:var(--bev-lo) var(--bev-hi) var(--bev-hi) var(--bev-lo)}
table.grid{border-collapse:collapse;width:100%;font-size:.83rem;background:var(--paper)}
table.grid th,table.grid td{border:1px solid var(--line);padding:.28rem .45rem;
 text-align:left;vertical-align:top}
table.grid th{background:var(--sub);font-weight:700}
table.grid td.n,table.grid th.n{text-align:right;white-space:nowrap}
table.grid tr:hover td{background:var(--sub)}
.cal td{text-align:center;min-width:3.2rem}
.cal td.void{color:var(--dim);background:var(--sub)}
ul.plain{list-style:none;margin:.3rem 0;padding:0}
ul.plain li{padding:.18rem 0}
.nav{font-size:.78rem;color:var(--dim);margin:.5rem 0;font-family:"Courier New",monospace}
.nav a{text-decoration:none}
.pager{margin:1rem 0;font-size:.82rem}
.pager a,.pager span{display:inline-block;padding:.1rem .4rem;border:1px solid var(--line)}
.pager .cur{background:var(--sub);font-weight:700}
article.msg{background:var(--paper);border:2px solid;
 border-color:var(--bev-hi) var(--bev-lo) var(--bev-lo) var(--bev-hi);
 margin:0 0 1.1rem;padding:0;overflow-wrap:anywhere}
article.msg>header{background:var(--sub);border-bottom:1px solid var(--line);
 padding:.4rem .6rem;font-size:.82rem}
article.msg>header .who{font-weight:700}
article.msg>header .subj{display:block;font-size:.9rem;margin-top:.1rem}
article.msg .hdrs{font-size:.76rem;margin:.35rem 0 0}
article.msg .hdrs summary{cursor:pointer;color:var(--dim)}
article.msg .hdrs pre{margin:.3rem 0 0;padding:.4rem .5rem;background:var(--bg);
 border:1px solid var(--line);overflow-x:auto;font-size:.9em;white-space:pre-wrap}
article.msg .body{padding:.6rem .7rem}
.txt{font-family:"Courier New",Courier,ui-monospace,monospace;font-size:.88rem;
 line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 .55rem}
.txt.empty{color:var(--dim);font-style:italic}
.sig{font-family:"Courier New",Courier,monospace;font-size:.8rem;color:var(--dim);
 white-space:pre-wrap;border-top:1px dotted var(--line);margin-top:.7rem;
 padding-top:.35rem}
blockquote.q,details.q{margin:.2rem 0 .55rem;padding:.15rem 0 .15rem .7rem;
 border-left:3px solid var(--line)}
blockquote.q cite,details.q>summary{display:block;font-style:normal;
 font-size:.76rem;color:var(--dim);margin-bottom:.2rem;font-family:Verdana,sans-serif}
details.q>summary{cursor:pointer}
details.q[open]>summary{margin-bottom:.35rem}
details.q>summary .n{opacity:.75}
.q1{border-left-color:var(--q1)}.q1>.txt,.q1>cite{color:var(--q1)}
.q2{border-left-color:var(--q2)}.q2>.txt,.q2>cite{color:var(--q2)}
.q3{border-left-color:var(--q3)}.q3>.txt,.q3>cite{color:var(--q3)}
.q4{border-left-color:var(--q4)}.q4>.txt,.q4>cite{color:var(--q4)}
.q5{border-left-color:var(--q5)}.q5>.txt,.q5>cite{color:var(--q5)}
.q6{border-left-color:var(--q6)}.q6>.txt,.q6>cite{color:var(--q6)}
a.in{border-bottom:1px dotted}
a.gone{opacity:.75}
a.dead{color:inherit;border-bottom:1px dotted var(--line);cursor:help}
.outline{font-size:.82rem}
.outline ol{margin:.2rem 0;padding-left:1.4rem}
.outline li{padding:.1rem 0}
/* Reply depth, when the archive kept real References: headers.  Indentation is
   in rem so it survives a phone, and it stops at six: deeper than that the
   thread is a duel and the arrow in the message header says who with. */
article.msg.d1{margin-left:1.1rem}article.msg.d2{margin-left:2.2rem}
article.msg.d3{margin-left:3.3rem}article.msg.d4{margin-left:4.4rem}
article.msg.d5{margin-left:5.5rem}article.msg.d6{margin-left:6.6rem}
.outline li.d1{padding-left:1rem}.outline li.d2{padding-left:2rem}
.outline li.d3{padding-left:3rem}.outline li.d4{padding-left:4rem}
.outline li.d5{padding-left:5rem}.outline li.d6{padding-left:6rem}
@media(max-width:38rem){article.msg.d1,article.msg.d2,article.msg.d3,
 article.msg.d4,article.msg.d5,article.msg.d6{margin-left:.5rem;
 border-left:3px solid var(--acc)}}
.warn{font-size:.78rem;color:var(--hot)}
#q{width:100%;padding:.45rem .5rem;font:inherit;color:var(--fg);
 background:var(--paper);border:2px solid;
 border-color:var(--bev-lo) var(--bev-hi) var(--bev-hi) var(--bev-lo)}
.res{list-style:none;padding:0;margin:1rem 0}
.res li{padding:.6rem 0;border-top:1px solid var(--line)}
.res a{font-weight:700}
.res .ex{margin:.2rem 0 0;color:var(--dim);font-size:.85rem}
.res mark{background:var(--acc);color:var(--paper);padding:0 .15em}
footer.foot{margin-top:2rem;border-top:2px solid var(--bev-lo);padding-top:.6rem;
 font-size:.76rem;color:var(--dim)}
hr.rule{border:0;border-top:1px solid var(--bev-lo);border-bottom:1px solid var(--bev-hi);
 margin:1.2rem 0}
"""

PAGE = """\
<!doctype html>
<html lang="{lang}"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="stylesheet" href="{root}style.css">
</head>
<body><div class="wrap">
<header class="top" data-pagefind-ignore><div>
<div class="site"><a href="{root}">{site}</a></div>
<div class="crumb">{crumb}</div></div>
<div class="find"><a href="{root}cerca/">cerca</a></div></header>
{body}
<hr class="rule">
<footer class="foot" data-pagefind-ignore>{foot}</footer>
</div></body></html>
"""

FOOT = ('Archivio ricostruito dagli snapshot di '
        '<a href="https://web.archive.org/">Internet Archive</a>. I messaggi '
        'sono dei rispettivi autori e stanno qui a titolo di archivio storico.')

SEARCH_JS = """\
<script type="module">
const box = document.getElementById("q");
const out = document.getElementById("res");
const note = document.getElementById("note");
const pf = await import("../pagefind/pagefind.js");
await pf.options({ bundlePath: "../pagefind/" });
let token = 0;

// `"due parole"` and `+parola` both mean: exactly this, no stemming.
function parse(raw) {
  const exact = [];
  let q = raw.replace(/"([^"]+)"/g, (_m, p) => { exact.push(p.trim()); return " " + p + " "; });
  q = q.replace(/(^|\\s)\\+(\\S+)/g, (_m, s, w) => { exact.push(w); return s + w; });
  return { q: q.trim(), exact: exact.filter(Boolean) };
}

function wordRx(term) {
  const esc = term.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&").replace(/\\s+/g, "\\\\s+");
  try {
    return new RegExp("(?<![\\\\p{L}\\\\p{N}_])" + esc + "(?![\\\\p{L}\\\\p{N}_])", "iu");
  } catch (e) {
    return new RegExp(esc, "iu");
  }
}

function card(d) {
  const sub = (d.sub_results && d.sub_results[0]) || null;
  const href = sub ? sub.url : d.url;
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = href;
  a.textContent = d.meta && d.meta.title ? d.meta.title : d.url;
  const p = document.createElement("p");
  p.className = "ex";
  p.innerHTML = (sub ? sub.excerpt : d.excerpt) || "";
  li.append(a, p);
  return li;
}

async function run() {
  const mine = ++token;
  const raw = box.value.trim();
  out.replaceChildren();
  if (raw.length < 2) { note.textContent = ""; return; }
  const { q, exact } = parse(raw);
  note.textContent = "cerco...";
  const search = await pf.search(q);
  if (mine !== token) return;
  const rx = exact.map(wordRx);
  const kept = [];
  let scanned = 0;
  for (const r of search.results) {
    if (mine !== token) return;
    if (kept.length >= 30) break;
    const d = await r.data();
    scanned++;
    if (rx.length && !rx.every((x) => x.test(d.raw_content || ""))) continue;
    kept.push(d);
    out.append(card(d));
  }
  if (mine !== token) return;
  const more = search.results.length > scanned ? " (primi " + scanned + " esaminati)" : "";
  note.textContent = kept.length
    ? kept.length + " risultati" + (rx.length ? ", filtrati esatti" : "") + more
    : "nessun risultato" + (rx.length ? " con la parola esatta" : "");
}

let t;
box.addEventListener("input", () => { clearTimeout(t); t = setTimeout(run, 250); });
box.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(t); run(); } });
const pre = new URLSearchParams(location.search).get("q");
if (pre) { box.value = pre; run(); }
</script>
"""


def writer(cfg: "Config"):
    """A `write()` bound to this archive's shell: name, language, footer."""
    foot = cfg.footer_html or FOOT

    def write(path: Path, *, title: str, crumb: str, body: str, root: str,
              desc: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            PAGE.format(title=esc(title), crumb=crumb, body=body, root=root,
                        foot=foot, desc=esc(desc[:180]),
                        site=esc(cfg.name), lang=esc(cfg.lang)),
            encoding="utf-8")
    return write


def pager(pages: int, cur: int, base: str) -> str:
    if pages < 2:
        return ""
    out = ['<div class="pager" data-pagefind-ignore>']
    for p in range(1, pages + 1):
        href = base if p == 1 else f"{base}page-{p}/"
        out.append(f'<span class="cur">{p}</span>' if p == cur
                   else f'<a href="{href}">{p}</a>')
    out.append("</div>")
    return "".join(out)


# -------------------------------------------------------------------- model

class Msg:
    __slots__ = ("id", "list", "period", "local_id", "seq", "when", "when_ok", "date_raw",
                 "name", "addr", "subject", "thread", "thread_prev", "thread_next",
                 "aprev", "anext", "url", "anchor", "parent", "depth")


AUTHORS_PER_PAGE = 300


def render(db_path: Path, out: Path, cfg: Config) -> None:
    t0 = time.time()
    write = writer(cfg)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS, encoding="utf-8")

    lists = {r["name"]: r for r in db.execute(
        "SELECT name, display_name, message_count, first_at, last_at FROM lists")}
    periods: dict[tuple[str, str], dict] = {}
    for r in db.execute("SELECT list, period, label, year, month, message_count FROM periods"):
        y, m = period_ym(r["period"], r["year"], r["month"], r["label"])
        periods[(r["list"], r["period"])] = {
            "y": y, "m": m, "label": period_label(r["period"], r["label"], y, m),
            "n": r["message_count"]}

    # --- light index: everything except the bodies ---------------------------
    msgs: list[Msg] = []
    by_id: dict[int, Msg] = {}
    by_key: dict[tuple[str, str, int], Msg] = {}
    by_thread: dict[int, list[Msg]] = defaultdict(list)
    by_period: dict[tuple[str, str], list[Msg]] = defaultdict(list)
    by_author: dict[str, list[Msg]] = defaultdict(list)
    suspect = 0

    # What a period is really about, measured rather than assumed. LISTSERV
    # named the archive file after the date it believed, and it was sometimes
    # lied to: `ita-pe/ind7001` is titled January 1970 because one message
    # carried an epoch-zero stamp, and the five genuine January 2000 messages
    # filed alongside it would be branded unreadable by a check against the
    # file's own name. The median of the plausible dates inside the period is
    # the honest reference; the label still stands as what the archive says.
    ref: dict[tuple[str, str], tuple[int, int]] = {}
    for lst, period, dates in (
            (r[0], r[1], r[2]) for r in db.execute(
                "SELECT list, period, group_concat(substr(posted_at,1,7))"
                "  FROM messages"
                " WHERE posted_at BETWEEN '1985' AND '2013'"
                " GROUP BY list, period")):
        ym = sorted((int(d[:4]), int(d[5:7])) for d in (dates or "").split(",") if len(d) >= 7)
        if ym:
            ref[(lst, period)] = ym[len(ym) // 2]

    for r in db.execute(
            "SELECT id, list, period, local_id, seq, posted_at, date_raw,"
            "       from_name, from_addr, subject, thread_id, parent_id,"
            "       thread_prev, thread_next, author_prev, author_next"
            "  FROM messages"):
        m = Msg()
        m.id, m.list, m.period, m.local_id = r["id"], r["list"], r["period"], r["local_id"]
        m.seq = r["seq"]
        m.date_raw = r["date_raw"]
        m.name = (r["from_name"] or "").strip()
        m.addr = (r["from_addr"] or "").strip()
        m.subject = (r["subject"] or "").strip()
        m.thread = r["thread_id"]
        m.parent = r["parent_id"]
        m.depth = 0
        m.thread_prev, m.thread_next = r["thread_prev"], r["thread_next"]
        m.aprev, m.anext = r["author_prev"], r["author_next"]

        # A date is believed only if it lands in the archive month it was filed
        # under.  The headers carry a 2038 overflow, a 1904, two 1970s and a
        # handful in year 0100; sorting on those alone puts them on top.
        per = periods.get((m.list, m.period))
        dt = parse_when(r["posted_at"])
        anchor = ref.get((m.list, m.period))
        ok = False
        if dt and anchor:
            delta = (dt.year - anchor[0]) * 12 + (dt.month - anchor[1])
            ok = -14 <= delta <= 14
        elif dt and per and per["y"]:
            if per["m"]:
                delta = (dt.year - per["y"]) * 12 + (dt.month - per["m"])
                ok = -1 <= delta <= 2
            else:
                ok = per["y"] - 1 <= dt.year <= per["y"] + 1
        elif dt:
            ok = 1985 <= dt.year <= 2012
        m.when = dt if ok else None
        m.when_ok = ok
        if dt and not ok:
            suspect += 1
        msgs.append(m)
        by_id[m.id] = m
        by_key[(m.list, m.period, m.local_id)] = m
        by_thread[m.thread].append(m)
        by_period[(m.list, m.period)].append(m)
        if m.addr:
            by_author[m.addr.lower()].append(m)

    # --- thread identity: directory name, ordering, subject ------------------
    threads = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, list, period, subject, message_count, first_at, last_at FROM threads")}

    # Ordering is the server's, not the sender's.  `(#184)` is the position LISTSERV
    # gave the message inside the archive file, so it survives a mailer that stamped
    # the year 0100; the Date: header does not.  Only when the sequence is missing
    # (183 of 24118) does the date get a say.
    def sort_key(m: Msg):
        per = periods.get((m.list, m.period)) or {"y": 0, "m": 0}
        return (per["y"], per["m"],
                (0, m.seq) if m.seq is not None else (1, 0),
                m.when or datetime(1, 1, 1), m.local_id)

    def order_thread(group: list[Msg]) -> list[Msg]:
        """Walk `Previous/Next in topic`: it is the order the server itself showed.

        Sorting a thread by date puts a 0100 or a 2038 timestamp at the top of the
        conversation; the chain has no such problem, and what it cannot reach falls
        back to the sequence.
        """
        by_local = {m.local_id: m for m in group}
        heads = [m for m in group if not m.thread_prev or m.thread_prev not in by_local]
        seen: set[int] = set()
        out: list[Msg] = []
        for head in sorted(heads, key=sort_key):
            cur: Msg | None = head
            while cur is not None and cur.local_id not in seen:
                seen.add(cur.local_id)
                out.append(cur)
                cur = by_local.get(cur.thread_next) if cur.thread_next else None
        out += sorted((m for m in group if m.local_id not in seen), key=sort_key)
        return out

    def order_by_parents(group: list[Msg]) -> list[Msg]:
        """Depth-first over the reply tree the `References:` headers describe.

        A root is a message whose parent is not in the group — either it opened
        the thread or the archive never caught what it answered.  Roots and
        siblings alike go in date order, and `depth` is kept so the page can
        indent: an argument on a mailing list is a tree, and flattening it to a
        list is exactly the information MHonArc's own pages threw away.
        """
        ids = {m.id for m in group}
        kids: dict[int | None, list[Msg]] = defaultdict(list)
        for m in group:
            kids[m.parent if m.parent in ids else None].append(m)
        out: list[Msg] = []
        stack: list[tuple[Msg, int]] = [
            (m, 0) for m in sorted(kids[None], key=sort_key, reverse=True)]
        seen: set[int] = set()
        while stack:                      # iterative: a 300-deep thread exists
            m, d = stack.pop()
            if m.id in seen:              # a cycle in the headers, seen in the wild
                continue
            seen.add(m.id)
            m.depth = d
            out.append(m)
            stack += [(k, d + 1) for k in sorted(kids.get(m.id, []),
                                                 key=sort_key, reverse=True)]
        out += sorted((m for m in group if m.id not in seen), key=sort_key)
        return out

    tdir: dict[int, str] = {}
    for tid, group in by_thread.items():
        group[:] = (order_by_parents if cfg.thread_model == "references"
                    else order_thread)(group)
        t = threads.get(tid) or {"list": group[0].list, "period": group[0].period,
                                 "subject": group[0].subject}
        subject = (t.get("subject") or group[0].subject or "").strip()
        tdir[tid] = f"{t['list']}/{t['period']}/t{tid}-{slug(subject, 'senza-oggetto')}/"
        threads.setdefault(tid, dict(t))
        threads[tid]["subject"] = subject
        threads[tid]["list"] = t["list"]
        threads[tid]["period"] = t["period"]
        threads[tid]["msgs"] = group

    for m in msgs:
        m.url = tdir[m.thread]
        m.anchor = f"p{m.local_id}"

    def rel(frm: str, to: str) -> str:
        """Relative href between two site-root-relative directory paths."""
        up = "../" * frm.count("/")
        return up + to

    def author_dir(addr: str) -> str:
        # crc32, not hash(): Python randomises string hashing per process, so
        # `hash()` gave a different suffix on every build — `webmaster@pegas.it`
        # was `…-7339` one run and `…-4762` the next, and every author URL in
        # the site, the sitemap and anyone's bookmarks moved with it.
        return f"autori/{slug(addr, 'anonimo')}-{zlib.crc32(addr.encode()) % 9973:04d}/"

    adirs = {a: author_dir(a) for a in by_author}

    def display(m: Msg) -> str:
        return m.name or m.addr or "mittente ignoto"

    # ------------------------------------------------------------- the index
    n_msg, n_thr = len(msgs), len(threads)
    span = [m.when for m in msgs if m.when]
    lo, hi = (min(span), max(span)) if span else (None, None)
    dates_of: dict[str, list[datetime]] = defaultdict(list)
    for m in msgs:
        if m.when:
            dates_of[m.list].append(m.when)
    order = sorted(lists.values(), key=lambda r: -r["message_count"])
    rows = "".join(
        f'<tr><td><a href="{r["name"]}/">{esc(r["display_name"] or r["name"])}</a></td>'
        f'<td class="n">{r["message_count"]}</td>'
        f'<td class="n">{sum(1 for t in threads.values() if t["list"] == r["name"])}</td>'
        f'<td>{span_text(dates_of[r["name"]])}</td></tr>'
        for r in order)
    # The address is shown next to the name because it is what distinguishes the
    # rows: the same person appears twice with two addresses, and once with a
    # phone number inside the display name, exactly as the headers had it.
    top = sorted(by_author.items(), key=lambda kv: -len(kv[1]))[:25]
    toprows = "".join(
        f'<tr><td><a href="{adirs[a]}">{esc(ms[0].name or a)}</a></td>'
        f'<td><code>{esc(a)}</code></td>'
        f'<td class="n">{len(ms)}</td></tr>' for a, ms in top)

    write(out / "index.html", root="", crumb="indice",
          title=cfg.name,
          desc=(f"{cfg.name}: {n_msg} messaggi "
                f"in {n_thr} discussioni, dal {lo.year if lo else '?'} al "
                f"{hi.year if hi else '?'}."),
          body=(
              f'<h1>{cfg.tagline or esc(cfg.name)}</h1>'
              '<div class="panel">'
              + (cfg.intro_html or "") +
              f'<p class="meta">{n_msg} messaggi, {n_thr} discussioni, '
              f'{len(by_author)} indirizzi, {span_text([d for d in (lo, hi) if d])}.</p>'
              '</div>'
              '<h2>Le liste</h2>'
              '<table class="grid"><thead><tr><th>Lista</th><th class="n">Messaggi</th>'
              '<th class="n">Discussioni</th><th>Periodo</th></tr></thead>'
              f'<tbody>{rows}</tbody></table>'
              '<h2>Chi ha scritto di pi&ugrave;</h2>'
              '<table class="grid"><thead><tr><th>Autore</th><th>Indirizzo</th>'
              '<th class="n">Messaggi</th></tr></thead>'
              f'<tbody>{toprows}</tbody></table>'
              f'<p class="meta"><a href="autori/">Tutti i {len(by_author)} '
              'mittenti</a> &middot; <a href="cerca/">ricerca full-text</a></p>'))

    urls = [""]
    n_files = 1

    # --------------------------------------------------------- one list page
    for name, r in lists.items():
        pers = sorted((p for (l, period), p in periods.items() if l == name),
                      key=lambda p: (p["y"], p["m"]))
        period_of = {(p["y"], p["m"]): period for (l, period), p in periods.items() if l == name}
        years = sorted({p["y"] for p in pers})
        head = ('<tr><th>Anno</th>' +
                "".join(f'<th class="n">{MONTHS[i][:3]}</th>' for i in range(1, 13)) +
                '<th class="n">tot</th></tr>')
        body_rows = []
        for y in years:
            cells = []
            for mm in range(1, 13):
                period = period_of.get((y, mm))
                if period:
                    n = periods[(name, period)]["n"]
                    cells.append(f'<td class="n"><a href="{period}/">{n}</a></td>')
                else:
                    cells.append('<td class="void">&middot;</td>')
            whole = period_of.get((y, 0))
            tot = sum(periods[(name, period_of[(y, m2)])]["n"]
                      for m2 in range(0, 13) if (y, m2) in period_of)
            label = (f'<a href="{whole}/">{y}</a>' if whole else str(y))
            body_rows.append(f'<tr><td>{label}</td>{"".join(cells)}'
                             f'<td class="n">{tot}</td></tr>')
        thr = [t for t in threads.values() if t["list"] == name]
        big = sorted(thr, key=lambda t: -len(t["msgs"]))[:20]
        biglist = "".join(
            f'<li><a href="{rel(name + "/", tdir[t["id"]])}">'
            f'{esc(t["subject"] or "(senza oggetto)")}</a> '
            f'<span class="meta">&mdash; {len(t["msgs"])} messaggi, '
            f'{periods.get((name, t["period"]), {}).get("label", t["period"])}</span></li>'
            for t in big)
        write(out / name / "index.html", root="../",
              title=f"{r['display_name'] or name} — {cfg.name}",
              crumb=f'<a href="../">indice</a> &rsaquo; {esc(r["display_name"] or name)}',
              desc=f"L'archivio della lista {r['display_name'] or name}: "
                   f"{r['message_count']} messaggi.",
              body=(f'<h1>{esc(r["display_name"] or name)}</h1>'
                    f'<p class="meta">{r["message_count"]} messaggi in '
                    f'{len(thr)} discussioni, {span_text(dates_of[name])}.</p>'
                    '<h2>Archivio per mese</h2>'
                    f'<table class="grid cal"><thead>{head}</thead>'
                    f'<tbody>{"".join(body_rows)}</tbody></table>'
                    '<p class="meta">Il numero &egrave; quanti messaggi ha quel mese.'
                    + (' Un anno cliccabile &egrave; un archivio annuale: le liste '
                       'pi&ugrave; quiete non venivano spezzate per mese.'
                       if any(p["m"] == 0 for p in pers) else "") + '</p>'
                    + (f'<h2>Le discussioni pi&ugrave; lunghe</h2>'
                       f'<ul class="plain">{biglist}</ul>' if biglist else "")))
        urls.append(f"{name}/")
        n_files += 1

    # -------------------------------------------------------- one month page
    for (name, period), group in by_period.items():
        per = periods.get((name, period), {"label": period, "y": 0, "m": 0})
        tids = sorted({m.thread for m in group},
                      key=lambda t: sort_key(threads[t]["msgs"][0]))
        rows = []
        for tid in tids:
            ms = threads[tid]["msgs"]
            who = []
            for m in ms:
                d = display(m)
                if d not in who:
                    who.append(d)
            rows.append(
                f'<tr><td><a href="t{tid}-{slug(threads[tid]["subject"], "senza-oggetto")}/">'
                f'{esc(threads[tid]["subject"] or "(senza oggetto)")}</a></td>'
                f'<td class="n">{len(ms)}</td>'
                f'<td>{esc(", ".join(who[:4]))}'
                f'{" &hellip;" if len(who) > 4 else ""}</td>'
                f'<td class="n">{fmt_day(ms[0].when) if ms[0].when else per["label"]}</td></tr>')
        crumb = (f'<a href="../../">indice</a> &rsaquo; '
                 f'<a href="../">{esc(lists[name]["display_name"] or name)}</a> '
                 f'&rsaquo; {esc(per["label"])}')
        write(out / name / period / "index.html", root="../../",
              title=f"{per['label']} — {lists[name]['display_name'] or name}",
              crumb=crumb,
              desc=f"{len(group)} messaggi di {name} in {per['label']}.",
              body=(f'<h1>{esc(lists[name]["display_name"] or name)} '
                    f'&mdash; {esc(per["label"])}</h1>'
                    f'<p class="meta">{len(group)} messaggi in {len(tids)} '
                    f'discussioni. <code>{esc(period)}</code></p>'
                    '<table class="grid"><thead><tr><th>Oggetto</th>'
                    '<th class="n">Msg</th><th>Chi</th><th class="n">Inizio</th>'
                    f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'))
        urls.append(f"{name}/{period}/")
        n_files += 1

    # ----------------------------------------------- threads with same subject
    bykey: dict[tuple[str, str], list[int]] = defaultdict(list)
    for tid, t in threads.items():
        k = subj_key(t["subject"])
        if k:
            bykey[(t["list"], k)].append(tid)

    # ------------------------------------------------------- the thread pages
    def resolve_factory(here: str):
        def resolve(lst: str, period: str, p: int) -> str | None:
            m = by_key.get((lst, period, p))
            if not m:
                return None
            return rel(here, m.url) + f"#{m.anchor}"
        return resolve

    bodies = db.execute(
        "SELECT id, body_html, body_text, headers_json, content_type, font,"
        "       in_reply_to, x_to, sender, reply_to"
        "  FROM messages")
    store: dict[int, sqlite3.Row] = {}
    for r in bodies:
        store[r["id"]] = r

    for tid, t in threads.items():
        ms = t["msgs"]
        here = tdir[tid]
        resolve = resolve_factory(here)
        name, period = t["list"], t["period"]
        per = periods.get((name, period), {"label": period})
        subject = t["subject"] or "(senza oggetto)"

        # Normalise each body once per thread, not once per quote block: a
        # forty-message thread would otherwise re-normalise the same text
        # hundreds of times.
        tnorm = [(display(m), norm(store[m.id]["body_text"])) for m in ms]

        arts = []
        for i, m in enumerate(ms):
            row = store[m.id]
            # Newest first, and only a dozen back: past that the match is more
            # likely a recycled signature than a real citation.
            earlier = list(reversed(tnorm[max(0, i - 12):i]))

            when = (f'{fmt_when(m.when)}' if m.when
                    else f'{per["label"]} <span class="warn">(data d\'invio '
                         f'illeggibile)</span>')
            def jump(p_: int | None, label: str) -> str | None:
                if not p_:
                    return None
                q = by_key.get((m.list, m.period, p_))
                if not q:
                    return None
                # Inside the same thread the target is on this very page: a bare
                # fragment beats a path that walks up three levels and back down.
                href = (f"#{q.anchor}" if q.thread == m.thread
                        else f"{rel(here, q.url)}#{q.anchor}")
                return f'<a href="{href}">{label}</a>'

            # With real headers the useful jump is "what this answers", which the
            # chain of Previous/Next cannot express: two replies to the same mail
            # are siblings, not neighbours.
            up = by_id.get(m.parent) if m.parent else None
            reply_to_link = (
                f'<a href="{"#" + up.anchor if up.thread == m.thread else rel(here, up.url) + "#" + up.anchor}">'
                f'&uarr; in risposta a {esc(display(up))}</a>') if up else None

            navbits = [x for x in (
                reply_to_link,
                jump(m.thread_prev, "&lt; precedente nel filo"),
                jump(m.thread_next, "successivo nel filo &gt;"),
                jump(m.aprev, "&lt; stesso autore"),
                jump(m.anext, "stesso autore &gt;")) if x]
            nav = (f'<div class="nav" data-pagefind-ignore>[ '
                   + " | ".join(navbits) + ' ]</div>') if navbits else ""

            hdrs = ""
            try:
                raw = json.loads(row["headers_json"] or "{}")
            except (ValueError, TypeError):
                raw = {}
            if raw:
                dump = "\n".join(f"{k}: {v}" for k, v in raw.items() if v)
                hdrs = ('<details class="hdrs" data-pagefind-ignore>'
                        '<summary>header originali</summary>'
                        f'<pre>{esc(dump)}</pre></details>')

            who = display(m)
            addr = (f' &lt;<a href="{rel(here, adirs[m.addr.lower()])}">'
                    f'{esc(m.addr)}</a>&gt;' if m.addr else "")
            # The newlines between fields are not formatting: Pagefind strips the
            # tags and concatenates what is left, so without them the search
            # excerpts read "…@elettra.trieste.itProssima Riunione ITA-PE".
            # A message whose subject is the thread's own is not worth repeating
            # under every header — only a changed one carries information.
            own = m.subject if m.subject and subj_key(m.subject) != subj_key(subject) else None
            # Indentation is capped: past six levels it eats the text on a phone,
            # and the "in risposta a" link still says who is answering whom.
            ind = f' d{min(m.depth, 6)}' if cfg.thread_model == "references" and m.depth else ""
            arts.append(
                f'<article class="msg{ind}" id="{m.anchor}">'
                f'<header><span class="who">{esc(who)}</span>{addr}\n'
                + (f'<span class="subj">{esc(own)}</span>\n' if own else "")
                + f'<span class="meta">{when}'
                f'{" &middot; (#%s)" % m.seq if m.seq else ""}</span>\n'
                f'{hdrs}</header>'
                f'<div class="body">'
                f'{body_html(row["body_html"], earlier, resolve, cfg, (m.list, m.period))}</div>'
                f'{nav}</article>')

        outline = ""
        if len(ms) > 3:
            items = "".join(
                f'<li class="d{min(m.depth, 6)}"><a href="#{m.anchor}">{esc(display(m))}</a> '
                f'<span class="meta">&mdash; {fmt_when(m.when) if m.when else per["label"]}'
                f'</span></li>' for m in ms)
            outline = ('<div class="panel outline" data-pagefind-ignore>'
                       f'<strong>Il filo</strong><ol>{items}</ol></div>')

        others = ([x for x in bykey.get((name, subj_key(subject)), []) if x != tid]
                  if cfg.cross_period else [])
        others.sort(key=lambda x: (periods.get((name, threads[x]["period"]), {}).get("y", 0),
                                   periods.get((name, threads[x]["period"]), {}).get("m", 0)))
        same = ""
        if others:
            links = "".join(
                f'<li><a href="{rel(here, tdir[x])}">'
                f'{periods.get((name, threads[x]["period"]), {}).get("label", threads[x]["period"])}'
                f'</a> <span class="meta">&mdash; {len(threads[x]["msgs"])} '
                f'messaggi</span></li>' for x in others[:20])
            note = (cfg.cross_period_note or
                    'I fili si fermano al confine dell\'archivio del mese: questi '
                    'sono gli altri archivi con lo stesso oggetto, non un filo '
                    'ricucito.')
            same = ('<h2>Stesso oggetto, altri mesi</h2>'
                    f'<p class="meta">{note}</p>'
                    f'<ul class="plain" data-pagefind-ignore>{links}</ul>')

        crumb = (f'<a href="{rel(here, "")}">indice</a> &rsaquo; '
                 f'<a href="{rel(here, name + "/")}">'
                 f'{esc(lists[name]["display_name"] or name)}</a> &rsaquo; '
                 f'<a href="{rel(here, f"{name}/{period}/")}">{esc(per["label"])}</a>')
        write(out / here / "index.html", root=rel(here, ""), crumb=crumb,
              title=f"{subject} — {lists[name]['display_name'] or name}",
              desc=plain(store[ms[0].id]["body_text"])[:180],
              body=(f'<div data-pagefind-body>'
                    f'<h1 data-pagefind-meta="title">{esc(subject)}</h1>'
                    f'<p class="meta" data-pagefind-ignore>{len(ms)} messaggi '
                    f'&middot; {esc(lists[name]["display_name"] or name)} '
                    f'&middot; {esc(per["label"])}</p>'
                    f'{outline}{"".join(arts)}</div>{same}'))
        urls.append(here)
        n_files += 1

    # -------------------------------------------------------- author pages
    ordered = sorted(by_author.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for addr, ms in ordered:
        ms = sorted(ms, key=sort_key)
        here = adirs[addr]
        names = []
        for m in ms:
            if m.name and m.name not in names:
                names.append(m.name)
        rows = "".join(
            f'<tr><td><a href="{rel(here, m.url)}#{m.anchor}">'
            f'{esc(m.subject or "(senza oggetto)")}</a></td>'
            f'<td>{esc(lists[m.list]["display_name"] or m.list)}</td>'
            f'<td class="n">{fmt_day(m.when) if m.when else esc(periods.get((m.list, m.period), {}).get("label", m.period))}</td>'
            f'</tr>' for m in ms)
        write(out / here / "index.html", root=rel(here, ""),
              title=f"{names[0] if names else addr} — {cfg.name}",
              crumb=f'<a href="{rel(here, "")}">indice</a> &rsaquo; '
                    f'<a href="{rel(here, "autori/")}">mittenti</a> &rsaquo; '
                    f'{esc(names[0] if names else addr)}',
              desc=f"{len(ms)} messaggi di {names[0] if names else addr}.",
              body=(f'<div data-pagefind-ignore>'
                    f'<h1>{esc(names[0] if names else addr)}</h1>'
                    f'<p class="meta"><code>{esc(addr)}</code>'
                    + (f' &middot; anche come {esc(", ".join(names[1:5]))}'
                       if len(names) > 1 else "")
                    + f' &middot; {len(ms)} messaggi</p>'
                      '<table class="grid"><thead><tr><th>Oggetto</th><th>Lista</th>'
                      '<th class="n">Data</th></tr></thead>'
                      f'<tbody>{rows}</tbody></table></div>'))
        n_files += 1

    pages = (len(ordered) + AUTHORS_PER_PAGE - 1) // AUTHORS_PER_PAGE
    for pi in range(1, pages + 1):
        chunk = ordered[(pi - 1) * AUTHORS_PER_PAGE: pi * AUTHORS_PER_PAGE]
        here = "autori/" if pi == 1 else f"autori/page-{pi}/"
        rows = "".join(
            f'<tr><td><a href="{rel(here, adirs[a])}">'
            f'{esc(ms[0].name or a)}</a></td>'
            f'<td><code>{esc(a)}</code></td>'
            f'<td class="n">{len(ms)}</td></tr>' for a, ms in chunk)
        write(out / here / "index.html", root=rel(here, ""),
              title=f"Mittenti — {cfg.name}",
              crumb=f'<a href="{rel(here, "")}">indice</a> &rsaquo; mittenti',
              desc=f"{len(ordered)} indirizzi che hanno scritto alle liste.",
              body=(f'<div data-pagefind-ignore><h1>Mittenti</h1>'
                    f'<p class="meta">{len(ordered)} indirizzi, ordinati per '
                    f'numero di messaggi.</p>'
                    '<table class="grid"><thead><tr><th>Nome</th><th>Indirizzo</th>'
                    f'<th class="n">Msg</th></tr></thead><tbody>{rows}</tbody></table>'
                    + pager(pages, pi, rel(here, "autori/")) + '</div>'))
        n_files += 1

    # ------------------------------------------------------------ search page
    write(out / "cerca" / "index.html", root="../",
          title=f"Cerca — {cfg.name}",
          crumb='<a href="../">indice</a> &rsaquo; cerca',
          desc=f"Ricerca full-text in {cfg.name}.",
          body=('<h1>Cerca nell\'archivio</h1>'
                '<input id="q" type="search" autocomplete="off" '
                'placeholder="parole da cercare" aria-label="cerca">'
                '<p class="meta">Le virgolette o il <code>+</code> chiedono la parola '
                '<strong>esatta</strong>: <code>"dominio"</code> o '
                '<code>+dominio</code> non tirano su <em>domini</em>. Senza, la '
                'ricerca resta larga.</p>'
                '<p class="meta" id="note"></p>'
                '<ol class="res" id="res"></ol>'
                '<noscript><p class="meta">La ricerca ha bisogno di JavaScript. '
                'Senza, si naviga dall\'<a href="../">indice</a>: ogni pagina '
                '&egrave; HTML statico.</p></noscript>' + SEARCH_JS))
    n_files += 1

    # ---------------------------------------------------------------- sitemap
    base = cfg.base_url.rstrip("/") + "/"
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"<url><loc>{esc(base + u)}</loc></url>" for u in urls]
    sm.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n", encoding="utf-8")

    db.close()
    mb = sum(p.stat().st_size for p in out.rglob("*.html")) / 1048576
    print(f"DONE {n_files} pagine, {mb:.1f} MB, {n_msg} messaggi, {n_thr} discussioni, "
          f"{len(by_author)} mittenti, {suspect} date scartate, "
          f"{len(urls)} url in sitemap, {time.time() - t0:.0f}s")


def span_text(dates: list[datetime]) -> str:
    """The range a list actually covers, measured on the dates that are believable.

    Not on which archive files exist: `ita-pe/ind7001` is a January 1970 archive
    LISTSERV opened for one epoch-zero message, and reading the span off the
    filenames dates the assembly of the Italian Naming Authority to 1970.

    Written as a dash-joined range on purpose — "dal ottobre 1994" is not
    Italian and the article changes shape with every month name.
    """
    if not dates:
        return "periodo ignoto"
    lo, hi = min(dates), max(dates)
    return (f"{MONTHS[lo.month]} {lo.year} &ndash; {MONTHS[hi.month]} {hi.year}"
            if (lo.year, lo.month) != (hi.year, hi.month)
            else f"{MONTHS[lo.month]} {lo.year}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("archive.toml"),
                    help="TOML describing this archive (see archive.example.toml)")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--out", default="site", type=Path)
    ap.add_argument("--base-url", default=None,
                    help="override base_url from the config")
    a = ap.parse_args()
    # No silent fallback to the dataclass defaults: an archive rendered with
    # name="Archivio" and base_url=example.invalid looks plausible and is wrong
    # in every canonical URL and every sitemap entry.
    if not a.config.exists():
        raise SystemExit(f"{a.config}: not found (copy archive.example.toml)")
    cfg = Config.load(a.config)
    if a.base_url:
        cfg.base_url = a.base_url
    render(a.db, a.out, cfg)


if __name__ == "__main__":
    main()
