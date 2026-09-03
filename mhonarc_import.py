#!/usr/bin/env python3
"""MHonArc pages -> the SQLite database described in SCHEMA.md.

MHonArc wrote every message page with the metadata it used to build the
indexes still in it, as HTML comments above the markup:

    <!-- MHonArc v2.3.3 -->
    <!--X-Subject: Re: MICROSOFT &#45; STORIA DI -->
    <!--X-From: Carlo Gubitosa <gubi@olografix.org> -->
    <!--X-Date: Tue, 17 Nov 1998 14:01:15 +0100 -->
    <!--X-Message-Id: 3.0.5.32.19981117132544.008d5b10@mail.olografix.org -->
    <!--X-Reference: 002601be1205$676bb140$2feaf3c2@pc&#45;iosto -->

That is the whole difference between this importer and the LISTSERV one: real
`Message-Id:` and `References:` values, so threads are the reply graph the
senders actually made rather than a guess reconstructed from Previous/Next
links.  Everything downstream (`render.py`, `thread_model = "references"`)
depends on `message_id` and `parent_id` being filled here.

Two measurements this code is shaped by, both made on the ecn.org/cyber-rights
pages on 2026-09-03:

  * `X-Date` is NOT the `Date:` header.  It is later than the `Date:` shown in
    the message's own header table on every page checked — by 35 minutes in the
    ordinary case and by three days in `199812/msg00028.html`, which the sender
    dated 30 November.  It is when the list saw the message, not when it was
    written.  So `posted_at` comes from the header table, `X-Date` is kept in
    `headers_json` as `X-MHonArc-Date`, and a message can legitimately sit in a
    period that does not match its date.

  * The Previous/Next arrows in these pages point *backwards*: the "prev" icon
    on `msg00239` links to `msg00240`.  Nothing here reads them, and
    `prev_local`/`next_local` are deliberately left NULL rather than filled
    with a direction that is wrong half the time — the reply graph is what this
    archive threads on.
"""

from __future__ import annotations

import argparse
import codecs
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

# `msg00239.html`, and the odd `msg00239.htm` a mirror produced.
RE_FILE = re.compile(r"^msg(?P<n>\d+)\.html?$", re.I)
# A period directory is the month MHonArc filed under: `199812`.  The 1997
# pages sit loose in the archive root, with no directory at all.
RE_PERIOD_DIR = re.compile(r"^\d{6}$")

RE_XCOMMENT = re.compile(r"<!--X-(?P<field>[A-Za-z0-9-]+):(?P<val>.*?)-->", re.S)
RE_HEAD_TABLE = re.compile(
    r"<!--X-Head-of-Message-->(?P<t>.*?)<!--X-Head-of-Message-End-->", re.S)
RE_BODY = re.compile(
    r"<!--X-Body-of-Message-->(?P<b>.*?)<!--X-Body-of-Message-End-->", re.S)
RE_PRE = re.compile(r"^\s*<pre>(?P<b>.*)</pre>\s*$", re.S | re.I)
RE_TR = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
RE_TD = re.compile(r"<td\b[^>]*>(?P<v>.*?)</td>", re.S | re.I)
RE_TAG = re.compile(r"<[^>]+>")
RE_BR = re.compile(r"<br\s*/?>", re.I)
RE_WS = re.compile(r"[ \t]+")
RE_NL = re.compile(r"\n{3,}")
RE_ADDR = re.compile(r"<\s*([^<>@\s]+@[^<>@\s]+)\s*>")
RE_MSGID = re.compile(r"^<(.*)>$", re.S)

# Fields carried as repeatable comments: one per value, in the order MHonArc
# emitted them (References oldest first).
MULTI = {"REFERENCE", "REFERENCES", "FOLLOW-UPS", "FOLLOWUPS"}
# Structural comments — they mark regions of the page, they are not metadata.
STRUCTURAL = re.compile(
    r"^(HEAD-END|BODY-BEGIN|BODY-OF-MESSAGE|MSGBODY|TOPPNI|BOTPNI|USER-HEADER|"
    r"USER-FOOTER|SUBJECT-HEADER|HEAD-OF-MESSAGE|HEAD-BODY-SEP|FOLLOW-UPS|"
    r"REFERENCES|ML-.*)(-BEGIN|-END)?$")


def strip_html(fragment: str) -> str:
    text = RE_BR.sub("\n", fragment)
    text = RE_TAG.sub("", text)
    text = html.unescape(text)
    text = RE_WS.sub(" ", text)
    return RE_NL.sub("\n\n", text).strip()


def parse_date(raw: str) -> str | None:
    """RFC 822-ish, as the mailers of 1998 wrote it.

    `email.utils` handles the zoo (two-digit years, `-0000`, obsolete zone
    names) better than any regex here would.  A date that lands outside the
    plausible life of a web mailing list is a broken header, not a fact, and
    comes back as None so the renderer keeps it out of its ranges.
    """
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None or not 1980 <= dt.year <= 2030:
        return None
    try:
        return dt.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return None


def split_from(value: str) -> tuple[str | None, str | None]:
    """`Carlo Gubitosa <gubi@olografix.org>` -> ('Carlo Gubitosa', 'gubi@…').

    MHonArc also passes through `user@host (Real Name)` and, for the messages
    a gateway rewrote, `owner-list@host (by way of Someone)`.
    """
    value = " ".join(value.split())
    m = RE_ADDR.search(value)
    if m:
        return (value[: m.start()].strip().strip('"') or None), m.group(1).lower()
    m = re.match(r"^(?P<addr>[^\s<>@]+@[^\s<>@]+)\s*(?:\((?P<name>.*)\))?$", value)
    if m:
        return (m.group("name") or "").strip() or None, m.group("addr").lower()
    return value or None, None


def clean_msgid(value: str) -> str | None:
    """Message ids as stored: no angle brackets, no surrounding space.

    MHonArc strips the brackets itself in `X-Message-Id` but not always in
    `X-Reference`, and a reference that keeps its brackets never matches the id
    it points at.
    """
    value = value.strip()
    m = RE_MSGID.match(value)
    if m:
        value = m.group(1).strip()
    return value or None


def rot13(value: str) -> str:
    return codecs.encode(value, "rot13")


def parse_page(raw: bytes, lst: str, period: str, local_id: int) -> dict | None:
    # Latin-1 never fails and these pages are from 1998; the few that were
    # really UTF-8 already reached the Archive mojibaked, and re-guessing here
    # would only produce a different wrong answer.
    text = raw.decode("iso-8859-1", "replace")

    comments: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for m in RE_XCOMMENT.finditer(text):
        field = m.group("field").upper()
        if STRUCTURAL.match(field):
            continue
        val = html.unescape(m.group("val")).strip()
        if not val:
            continue
        if field in MULTI:
            multi.setdefault(field, []).append(val)
        else:
            comments.setdefault(field, val)

    body_m = RE_BODY.search(text)
    if body_m is None:
        return None                      # an index page, or a truncated snapshot
    body_html = body_m.group("b")
    font = "prop"
    pre = RE_PRE.match(body_html)
    if pre:
        body_html, font = pre.group("b"), "pre"
    body_html = body_html.strip("\n")

    # The header table MHonArc drew above the body: From/Date/Subject, and
    # whatever else the list was configured to show.  This is where the real
    # `Date:` lives (see the module docstring).
    table: dict[str, str] = {}
    head = RE_HEAD_TABLE.search(text)
    if head:
        for tr in RE_TR.findall(head.group("t")):
            tds = RE_TD.findall(tr)
            if len(tds) != 2:
                continue
            key = strip_html(tds[0]).rstrip(":").strip()
            val = strip_html(tds[1])
            if key and val:
                table.setdefault(key, val)

    from_raw = table.get("From") or comments.get("FROM") or ""
    if not from_raw and "FROM-R13" in comments:
        from_raw = rot13(comments["FROM-R13"])       # MHonArc 2.4's spam armour
    from_name, from_addr = split_from(from_raw) if from_raw else (None, None)

    date_raw = table.get("Date") or comments.get("DATE")
    posted_at = parse_date(date_raw) if date_raw else None
    if posted_at is None and comments.get("DATE"):
        # Last resort: the arrival date is wrong by minutes to days, but a
        # message with no date at all falls out of every index.
        posted_at = parse_date(comments["DATE"])

    refs = [clean_msgid(v) for v in multi.get("REFERENCE", []) + multi.get("REFERENCES", [])]
    refs = [r for r in refs if r]
    message_id = clean_msgid(comments.get("MESSAGE-ID", "")) or None
    in_reply_to = clean_msgid(comments.get("IN-REPLY-TO", "")) or (refs[-1] if refs else None)

    headers = {k: v for k, v in table.items()}
    headers.update({f"X-MHonArc-{k.title()}": v for k, v in comments.items()})
    for k, vals in multi.items():
        headers[f"X-MHonArc-{k.title()}"] = vals

    rec = {
        "list": lst,
        "period": period,
        "local_id": local_id,
        "seq": local_id,          # MHonArc numbers by arrival inside the period
        "posted_at": posted_at,
        "date_raw": date_raw,
        "from_name": from_name,
        "from_addr": from_addr,
        "subject": table.get("Subject") or comments.get("SUBJECT"),
        "reply_to": table.get("Reply-To") or comments.get("REPLY-TO"),
        "sender": table.get("Sender") or comments.get("SENDER"),
        "x_to": table.get("To") or comments.get("TO"),
        "content_type": comments.get("CONTENT-TYPE"),
        "headers_json": json.dumps(headers, ensure_ascii=False),
        "body_html": body_html,
        "body_text": strip_html(body_html),
        "font": font,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references_ids": json.dumps(refs, ensure_ascii=False) if refs else None,
    }
    # An empty header is an absent header (SCHEMA.md): normalise here, or every
    # later `IS NULL` lies.
    for k, v in list(rec.items()):
        if isinstance(v, str) and not v.strip() and k not in ("body_html", "body_text"):
            rec[k] = None
    return rec


def period_of(path: Path, pages: Path) -> str:
    """`pages/199812/msg00025.html` -> '199812'.

    The 1997 pages of cyber-rights sit loose in the archive root with no
    directory: they become one yearly period, which the schema allows (`month`
    NULL) and the renderer labels by year.
    """
    parent = path.parent.name
    if path.parent != pages and RE_PERIOD_DIR.match(parent):
        return parent
    return "flat"


def period_year_month(period: str, dates: list[str]) -> tuple[int | None, int | None]:
    """What a period is really about, measured rather than assumed.

    A directory name is believed when it is a month; `flat` has no name to
    believe, so the year comes from the *median* of the dates in it — the mean
    or the min would be dragged out of the decade by one message with a broken
    `Date:`.
    """
    if RE_PERIOD_DIR.match(period):
        year, month = int(period[:4]), int(period[4:])
        if 1 <= month <= 12:
            return year, month
    good = sorted(d for d in dates if d)
    if not good:
        return None, None
    return int(good[len(good) // 2][:4]), None


def resolve_parents(conn: sqlite3.Connection) -> tuple[int, int]:
    """Fill `parent_id` from the reply headers.  Returns (resolved, cycles cut).

    In order: `In-Reply-To`, then the last entry of `References` that is a
    message we hold, then any earlier one.  A message that references itself,
    or a chain that loops because a mailer rewrote an id, is cut rather than
    followed — the walk below would otherwise never terminate.
    """
    rows = conn.execute(
        "SELECT id, message_id, in_reply_to, references_ids FROM messages").fetchall()
    by_msgid: dict[str, int] = {}
    for mid, message_id, _, _ in rows:
        if message_id and message_id not in by_msgid:
            by_msgid[message_id] = mid

    parent_of: dict[int, int] = {}
    for mid, message_id, in_reply_to, refs_json in rows:
        candidates: list[str] = []
        if in_reply_to:
            candidates.append(in_reply_to)
        if refs_json:
            candidates.extend(reversed(json.loads(refs_json)))
        for cand in candidates:
            other = by_msgid.get(cand)
            if other is not None and other != mid:
                parent_of[mid] = other
                break

    # Cut cycles: follow each chain up, and drop the edge that closes a loop.
    cycles = 0
    for mid in list(parent_of):
        seen = {mid}
        cur = parent_of.get(mid)
        while cur is not None:
            if cur in seen:
                del parent_of[mid]
                cycles += 1
                break
            seen.add(cur)
            cur = parent_of.get(cur)

    conn.executemany("UPDATE messages SET parent_id = ? WHERE id = ?",
                     [(p, m) for m, p in parent_of.items()])
    return len(parent_of), cycles


def build_threads(conn: sqlite3.Connection) -> None:
    """A thread is a connected component of the `parent_id` graph.

    Not of the subject line: `Re: [no subject]` would merge a decade of
    unrelated messages into one thread, and the ids are right here.
    """
    rows = conn.execute(
        "SELECT id, list, period, subject, posted_at, parent_id FROM messages").fetchall()

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for r in rows:
        parent.setdefault(r[0], r[0])
    for r in rows:
        if r[5] is not None:
            union(r[0], r[5])

    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(find(r[0]), []).append(r)

    threads, updates = [], []
    for tid, members in enumerate(
            sorted(groups.values(), key=lambda g: min(m[0] for m in g)), 1):
        members.sort(key=lambda m: (m[4] or "", m[0]))
        head = members[0]
        dates = [m[4] for m in members if m[4]]
        threads.append((tid, head[1], head[2], head[3], len(members),
                        min(dates) if dates else None, max(dates) if dates else None))
        updates.extend((tid, m[0]) for m in members)

    conn.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", threads)
    conn.executemany("UPDATE messages SET thread_id = ? WHERE id = ?", updates)


COLS = ["list", "period", "local_id", "seq", "posted_at", "date_raw", "from_name",
        "from_addr", "subject", "reply_to", "sender", "x_to", "content_type",
        "headers_json", "body_html", "body_text", "font", "message_id",
        "in_reply_to", "references_ids"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pages", default="pages", type=Path)
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--schema", default="schema.sql", type=Path)
    ap.add_argument("--list", required=True, help="slug of the list, e.g. cyber-rights")
    ap.add_argument("--display-name", help="how the archive titled it (default: --list)")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    # A rebuild, not a merge: the pages are the source of truth and re-importing
    # into a half-filled database would leave the old threads behind.
    if args.db.exists():
        args.db.unlink()
    for suffix in ("-wal", "-shm"):
        p = args.db.with_name(args.db.name + suffix)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(args.db)
    conn.executescript(args.schema.read_text(encoding="utf-8"))

    insert = (f"INSERT OR IGNORE INTO messages ({','.join(COLS)}) "
              f"VALUES ({','.join('?' * len(COLS))})")

    seen = skipped = 0
    period_dates: dict[str, list[str]] = {}

    for path in sorted(args.pages.rglob("*.htm*")):
        m = RE_FILE.match(path.name)
        if not m:
            continue
        period = period_of(path, args.pages)
        rec = parse_page(path.read_bytes(), args.list, period, int(m.group("n")))
        if rec is None:
            skipped += 1
            continue
        conn.execute(insert, [rec[c] for c in COLS])
        period_dates.setdefault(period, []).append(rec["posted_at"])
        seen += 1
        if args.limit and seen >= args.limit:
            break
        if seen % 2000 == 0:
            conn.commit()
            print(f"  {seen} messages", file=sys.stderr)

    conn.commit()

    conn.execute(
        "INSERT INTO lists (name, display_name, message_count, first_at, last_at) "
        "SELECT ?, ?, count(*), min(posted_at), max(posted_at) FROM messages WHERE list = ?",
        (args.list, args.display_name or args.list, args.list))
    for (period,) in conn.execute(
            "SELECT DISTINCT period FROM messages WHERE list = ?", (args.list,)).fetchall():
        year, month = period_year_month(period, period_dates.get(period, []))
        label = (f"{year}-{month:02d}" if year and month else
                 str(year) if year else period)
        conn.execute(
            "INSERT INTO periods (list, period, label, year, month, message_count) "
            "SELECT ?, ?, ?, ?, ?, count(*) FROM messages WHERE list = ? AND period = ?",
            (args.list, period, label, year, month, args.list, period))

    resolved, cycles = resolve_parents(conn)
    build_threads(conn)
    conn.execute("INSERT INTO messages_fts(rowid, subject, from_name, from_addr, body_text) "
                 "SELECT id, subject, from_name, from_addr, body_text FROM messages")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    threads = conn.execute("SELECT count(*) FROM threads").fetchone()[0]
    no_date = conn.execute(
        "SELECT count(*) FROM messages WHERE posted_at IS NULL").fetchone()[0]
    no_msgid = conn.execute(
        "SELECT count(*) FROM messages WHERE message_id IS NULL").fetchone()[0]
    conn.close()

    print(f"imported {seen} messages ({skipped} pages without a body), "
          f"{threads} threads, {resolved} replies linked"
          + (f", {cycles} reference cycles cut" if cycles else "")
          + f", {no_date} without a date, {no_msgid} without a Message-Id -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
