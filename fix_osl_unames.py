#!/usr/bin/env python3
"""Correct the @uname fields in the Oracc Sign List (osl.asl).

Usage
-----
    python3 fix_osl_unames.py [path/to/osl.asl]

Behaviour
---------
Each @sign / @form / @pcun / ... entry is examined on its own. If @ucun is a
single code point assigned in Unicode (not private use), then the @uname is
updated to match its name according to Unicode. If @ucun is absent, holds more
than one character, or is a private-use code point, then any @uname is removed.

Source of Unicode names
-----------------------
Names come from the Unicode Character Database (UCD), version 18.0.0, as
published by the Unicode Consortium:

    https://www.unicode.org/Public/18.0.0/ucd/UnicodeData.txt   (field 1: Name)
    https://www.unicode.org/Public/18.0.0/ucd/NameAliases.txt   (type "correction")

Unicode character names are immutable, so when Unicode has later decided a name
was wrong it publishes a "correction" alias in NameAliases.txt rather than
changing UnicodeData.txt. For those characters (e.g. U+122D4, formally
CUNEIFORM SIGN SHIR TENU, corrected to CUNEIFORM SIGN NU11 TENU) this script
uses the correction, which is the name Unicode itself recommends.
"""

from __future__ import annotations

import argparse
import re
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

UCD_VERSION = "18.0.0"
UCD_URL = f"https://www.unicode.org/Public/{UCD_VERSION}/ucd/"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ASL = SCRIPT_DIR.parent / "osl" / "00lib" / "osl.asl"

CUNEIFORM_BLOCKS = [
    (0x12000, 0x123FF),  # Cuneiform
    (0x12400, 0x1247F),  # Cuneiform Numbers and Punctuation
    (0x12480, 0x1254F),  # Early Dynastic Cuneiform
    (0x12550, 0x1268F),  # Archaic Cuneiform Numerals
]

# Tags that begin a new entry in the sign list, and tags that end one.
ENTRY_START = re.compile(r"^@(sign-?|form[-+]?|pcun|lref|compoundonly|signlist)\b")
ENTRY_END = re.compile(r"^(@@|@end\b)")

# A field line: "@tag<whitespace>value". Only ASCII spaces/tabs are trimmed:
# some @ucun values are deliberately U+00A0 NO-BREAK SPACE.
FIELD = re.compile(r"^@(?P<tag>[a-z]+)(?P<sep>[ \t]+)(?P<value>.*?)[ \t]*$")
LIST_U = re.compile(r"^U\+(?P<hex>[0-9A-Fa-f]{4,6})$")


# ---------------------------------------------------------------------------
# Unicode data
# ---------------------------------------------------------------------------

def fetch_ucd_file(fname: str) -> str:
    with urllib.request.urlopen(UCD_URL + fname) as resp:
        data: bytes = resp.read()
    return data.decode("utf-8")


def load_unicode_names() -> dict[int, str]:
    """Return {code point: preferred name} from the UCD."""
    names: dict[int, str] = {}
    for line in fetch_ucd_file("UnicodeData.txt").splitlines():
        fields = line.split(";")
        # Ranges (<CJK Ideograph, First> etc.) and controls have no real name;
        # none of them matter here.
        if fields[1].startswith("<"):
            continue
        names[int(fields[0], 16)] = fields[1]

    for line in fetch_ucd_file("NameAliases.txt").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        cp, alias, kind = line.split(";")
        if kind == "correction":
            names[int(cp, 16)] = alias
    return names


def is_private_use(cp: int) -> bool:
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD


def in_cuneiform_blocks(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in CUNEIFORM_BLOCKS)


def u(cp: int) -> str:
    return f"U+{cp:04X}"


# ---------------------------------------------------------------------------
# Parsing osl.asl into entries
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """One @sign/@form/... entry: a header line plus the field lines under it."""
    header: str
    first_line: int
    # tag -> list of (line index, separator, value)
    fields: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)

    def get(self, tag: str) -> list[tuple[int, str, str]]:
        return self.fields.get(tag, [])

    def label(self) -> str:
        return f"line {self.first_line + 1}: {self.header}"


def parse_entries(lines: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    current: Entry | None = None
    for i, raw in enumerate(lines):
        text = raw.rstrip("\r\n")
        if ENTRY_START.match(text):
            current = Entry(header=text, first_line=i)
            entries.append(current)
            continue
        if ENTRY_END.match(text):
            current = None
            continue
        if current is None:
            continue
        m = FIELD.match(text)
        if m:
            current.fields.setdefault(m["tag"], []).append((i, m["sep"], m["value"]))
    return entries


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

class LeaveAlone(Enum):
    """Single-code-point @ucun values the rules don't cover; the value is the reason."""
    UNKNOWN = "@ucun is a code point Unicode does not assign"
    NOT_CUNEIFORM = "@ucun is a non-cuneiform character"  # e.g. U+00A0 for the blank sign O


def expected_uname(ucun: str | None, names: dict[int, str]) -> str | LeaveAlone | None:
    """The @uname an entry should have given its @ucun, or None for "no @uname"."""
    if ucun is None or len(ucun) != 1:
        return None
    cp = ord(ucun)
    if is_private_use(cp):
        return None
    if cp not in names:
        return LeaveAlone.UNKNOWN
    if not in_cuneiform_blocks(cp):
        return LeaveAlone.NOT_CUNEIFORM
    return names[cp]


class Report:
    def __init__(self) -> None:
        self.sections: dict[str, list[str]] = {}

    def add(self, section: str, msg: str) -> None:
        self.sections.setdefault(section, []).append(msg)

    def print(self) -> None:
        for section, msgs in self.sections.items():
            print(f"\n== {section} ({len(msgs)}) ==")
            for m in msgs:
                print(f"  {m}")


def fix_unames(lines: list[str], entries: list[Entry], names: dict[int, str],
               report: Report) -> list[str]:
    replace: dict[int, str] = {}         # line index -> new line
    delete: set[int] = set()             # line index to remove
    insert_before: dict[int, str] = {}   # line index -> line to insert before it

    for e in entries:
        ucuns = e.get("ucun")
        if len(ucuns) > 1:
            report.add("Entries with more than one @ucun (left alone)", e.label())
            continue
        ucun = ucuns[0][2] if ucuns else None
        want = expected_uname(ucun, names)
        have = e.get("uname")

        if want is None:
            for idx, _sep, value in have:
                delete.add(idx)
                why = ("no @ucun" if ucun is None
                       else "private-use @ucun" if len(ucun) == 1
                       else "multi-character @ucun")
                report.add("Removed @uname", f"{e.label()}  ({why})  was: {value}")
            continue

        # Anything other than None comes from a single-character @ucun.
        assert ucun is not None
        cp = ord(ucun)

        if isinstance(want, LeaveAlone):
            report.add(f"{want.value} (left alone)",
                       f"{e.label()}  @ucun {u(cp)} {names.get(cp, '')}"
                       + (f"  has @uname {have[0][2]}" if have else ""))
            continue

        if have:
            idx, sep, value = have[0]
            if value != want:
                eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
                replace[idx] = f"@uname{sep}{want}{eol}"
                report.add("Corrected @uname",
                           f"{e.label()}  {u(cp)}\n      was: {value}\n      now: {want}")
            for idx, _sep, value in have[1:]:
                delete.add(idx)
                report.add("Removed duplicate @uname", f"{e.label()}  was: {value}")
        else:
            # Insert before "@list U+xxxx" if present, else before @ucun,
            # using the same separator style as that line.
            anchor = next((f for f in e.get("list") if LIST_U.match(f[2])), None) or ucuns[0]
            idx, sep, _ = anchor
            eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
            insert_before[idx] = f"@uname{sep}{want}{eol}"
            report.add("Added missing @uname", f"{e.label()}  {u(cp)}  {want}")

    out: list[str] = []
    for i, line in enumerate(lines):
        if i in insert_before:
            out.append(insert_before[i])
        if i in delete:
            continue
        out.append(replace.get(i, line))
    return out


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("asl", nargs="?", type=Path, default=DEFAULT_ASL,
                    help=f"sign list to fix (default: {DEFAULT_ASL})")
    args = ap.parse_args()

    names = load_unicode_names()
    original = args.asl.read_bytes().decode("utf-8")
    # Split on "\n" only: str.splitlines() would also split on U+2028, U+0085 etc.
    lines = [line for line in re.split(r"(?<=\n)", original) if line]

    report = Report()
    entries = parse_entries(lines)
    new_text = "".join(fix_unames(lines, entries, names, report))

    print(f"Checked {len(entries)} entries in {args.asl} against Unicode {UCD_VERSION}.")
    report.print()

    if new_text == original:
        print("\nNo @uname changes needed.")
    else:
        args.asl.write_bytes(new_text.encode("utf-8"))
        print(f"\nWrote {args.asl}.")


if __name__ == "__main__":
    main()
