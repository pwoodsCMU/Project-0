"""Decklist parsing.

Accepts the plain-text exports people actually have on hand: Moxfield,
Archidekt, MTGGoldfish, MTGO ``.dek`` text, or a bare list of names.

Recognised shapes::

    1 Sol Ring
    1x Sol Ring
    4 Lightning Bolt (LEA) 161 *F*
    Sol Ring                       # quantity defaults to 1
    1 Korvold, Fae-Cursed King *CMDR*
    SB: 1 Pithing Needle
    // Commander                   # section headers, with or without slashes
    Commander (1)

Commander detection, in priority order:
  1. an explicit ``--commander`` flag (handled by the CLI),
  2. a ``*CMDR*`` / ``*Commander*`` marker on the line,
  3. a "Commander" section header,
  4. a leading one-or-two-card block separated by a blank line from a 100-card
     list, which is how Moxfield and Archidekt export Commander decks.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

LINE_RE = re.compile(r"""
    ^\s*
    (?:(?P<qty>\d+)\s*[xX]?\s+)?      # optional quantity
    (?P<name>.+?)                     # card name (non-greedy)
    (?:\s+\((?P<set>[A-Za-z0-9]{2,6})\)(?:\s+(?P<num>[A-Za-z0-9\-★]+))?)?  # set/collector
    (?P<flags>(?:\s+\*[^*]+\*)*)      # *F*, *CMDR*, *E* ...
    \s*$
""", re.VERBOSE)

SECTION_RE = re.compile(
    r"^\s*(?://+\s*)?(commander|commanders|deck|mainboard|main|maybeboard|"
    r"considering|sideboard|companion|tokens?)\b\s*(?:\(\d+\)|:)?\s*$",
    re.IGNORECASE)

CMDR_FLAG_RE = re.compile(r"\*\s*(cmdr|commander)\s*\*", re.IGNORECASE)

IGNORED_SECTIONS = {"maybeboard", "considering", "sideboard", "token", "tokens"}


class DeckEntry(object):
    __slots__ = ("quantity", "name", "is_commander", "section", "line_no")

    def __init__(self, quantity: int, name: str, is_commander: bool,
                 section: str, line_no: int):
        self.quantity = quantity
        self.name = name
        self.is_commander = is_commander
        self.section = section
        self.line_no = line_no

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DeckEntry(%d, %r, cmdr=%s)" % (
            self.quantity, self.name, self.is_commander)


class ParsedDeck(object):
    def __init__(self, entries: List[DeckEntry], excluded: List[DeckEntry],
                 warnings: List[str], source: str = ""):
        self.entries = entries          # maindeck + commanders
        self.excluded = excluded        # sideboard / maybeboard, kept for reference
        self.warnings = warnings
        self.source = source

    @property
    def total_cards(self) -> int:
        return sum(e.quantity for e in self.entries)

    @property
    def commander_names(self) -> List[str]:
        return [e.name for e in self.entries if e.is_commander]

    def unique_names(self) -> List[str]:
        seen: Dict[str, None] = {}
        for entry in self.entries:
            seen.setdefault(entry.name, None)
        return list(seen)

    def quantities(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for entry in self.entries:
            out[entry.name] = out.get(entry.name, 0) + entry.quantity
        return out


def _clean_name(name: str) -> str:
    name = name.strip().strip(",")
    # "Fire // Ice" stays intact; strip trailing separators people leave behind.
    return re.sub(r"\s+", " ", name)


def parse_decklist(text: str, source: str = "",
                   commander_override: Optional[Sequence[str]] = None) -> ParsedDeck:
    entries: List[DeckEntry] = []
    excluded: List[DeckEntry] = []
    warnings: List[str] = []

    section = "deck"
    blocks: List[List[int]] = [[]]     # indices into `entries`, split on blank lines
    saw_section_header = False

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            if blocks[-1]:
                blocks.append([])
            continue
        header = SECTION_RE.match(line)
        if header:
            label = header.group(1).lower()
            section = {"commanders": "commander", "mainboard": "deck",
                       "main": "deck"}.get(label, label)
            saw_section_header = True
            if blocks[-1]:
                blocks.append([])
            continue
        if line.startswith("#") or line.startswith("//"):
            continue          # a genuine comment
        if line.lower().startswith("about") or line.lower().startswith("name "):
            continue          # Moxfield "About / Name" preamble

        current_section = section
        if line.upper().startswith("SB:"):
            line = line[3:].strip()
            current_section = "sideboard"

        match = LINE_RE.match(line)
        if not match:
            warnings.append("line %d: could not parse %r" % (line_no, raw.strip()))
            continue
        name = _clean_name(match.group("name"))
        if not name:
            continue
        flags = match.group("flags") or ""
        qty = int(match.group("qty") or 1)
        is_commander = bool(CMDR_FLAG_RE.search(flags)) or current_section == "commander"

        entry = DeckEntry(qty, name, is_commander, current_section, line_no)
        if current_section in IGNORED_SECTIONS:
            excluded.append(entry)
        else:
            blocks[-1].append(len(entries))
            entries.append(entry)

    if commander_override:
        wanted = {n.strip().lower() for n in commander_override}
        for entry in entries:
            entry.is_commander = entry.name.lower() in wanted
        found = {e.name.lower() for e in entries if e.is_commander}
        for name in wanted - found:
            warnings.append("commander %r was not found in the decklist" % name)
    elif not any(e.is_commander for e in entries):
        # Export convention: commander(s) alone in the first block.
        first = [i for i in blocks[0]]
        total = sum(e.quantity for e in entries)
        if (first and len(blocks) > 1
                and len(first) <= 2
                and sum(entries[i].quantity for i in first) <= 2
                and 98 <= total <= 100 and not saw_section_header):
            for i in first:
                entries[i].is_commander = True

    if not any(e.is_commander for e in entries):
        warnings.append(
            "no commander detected - pass --commander \"Name\" so the analysis "
            "can weigh your commander's colours and plan")

    return ParsedDeck(entries, excluded, warnings, source=source)


def load_decklist(path: str, commander_override: Optional[Sequence[str]] = None) -> ParsedDeck:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return parse_decklist(fh.read(), source=path,
                              commander_override=commander_override)
