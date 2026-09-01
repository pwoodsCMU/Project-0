"""A card corpus for judging replaceability.

``edhrec_rank`` says how widely a card is played across all of Commander. That
is a popularity measure, and using it directly answers the wrong question: it
tells you Burnished Hart is a reasonably popular card (rank 488), not that four
better green ramp spells exist at the same cost, which is why upgrade guides cut
it from three decks in four.

This module downloads Scryfall's ``oracle-cards`` bulk file once, keeps a small
index of every Commander-legal card that EDHREC ranks, and answers the question
that actually matters:

    of the cards this deck could play that do the same job at a similar cost,
    where does this one sit?

That is *replaceability*, and it is what a cut list is really ranking. A card in
the bottom quarter of its class has plenty of better options; a card at the top
of its class is the best answer available to this deck, however obscure it looks
in a global ranking.

The index is about 1 MB gzipped and takes a few seconds to build.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import scryfall
from .roles import card_roles

CORPUS_SCHEMA = 1
MAX_AGE_DAYS = 30

# Colour identity as a bitmask, so "can this deck cast that card" is one AND.
COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16}

# Peers must cost within this much of the card being judged. A one-mana
# difference is a fair comparison; three is a different card entirely.
MV_WINDOW = 1.0

# Below this many peers the percentile is noise rather than a measurement.
MIN_PEERS = 6


def color_mask(identity: Sequence[str]) -> int:
    mask = 0
    for color in identity:
        mask |= COLOR_BITS.get(color, 0)
    return mask


class Corpus(object):
    """Every Commander-legal, EDHREC-ranked card, bucketed by role."""

    def __init__(self, entries: Dict[str, tuple]):
        # oracle_id -> (rank, mana_value, color_mask, roles, game_changer)
        self.entries = entries
        self.by_role: Dict[str, List[tuple]] = {}
        for rank, mana_value, mask, roles, _gc in entries.values():
            for role in roles:
                self.by_role.setdefault(role, []).append((rank, mana_value, mask))

    def __len__(self) -> int:
        return len(self.entries)

    def game_changers(self) -> Set[str]:
        return {oid for oid, row in self.entries.items() if row[4]}

    def better_fraction(self, roles: Sequence[str], mana_value: float,
                        rank: Optional[int], deck_mask: int) -> Optional[float]:
        """0..1 - the share of the comparison class that outranks this card.

        0.0 means nothing this deck could play does the same job better at a
        similar cost; 1.0 means almost everything does. ``None`` means there is
        no meaningful comparison class, so the caller should fall back rather
        than guess.

        Measuring how many cards are *better* rather than what fraction the
        card beats matters: every class has a long tail of unplayable cards, so
        a percentile flatters anything remotely reasonable. Sol Ring and
        Cultivate come out at 0.00, Generous Ent at 0.20, Orchard Strider at
        0.48 - which is the ordering upgrade guides act on.
        """
        if rank is None or not roles:
            return None

        peers: Set[Tuple[int, float, int]] = set()
        for role in roles:
            for peer in self.by_role.get(role, ()):
                peer_rank, peer_mv, peer_mask = peer
                if abs(peer_mv - mana_value) > MV_WINDOW:
                    continue
                if peer_mask & ~deck_mask:
                    continue        # this deck could not cast it
                peers.add(peer)

        if len(peers) < MIN_PEERS:
            return None
        better = sum(1 for peer_rank, _, _ in peers if peer_rank < rank)
        return better / float(len(peers))


# --------------------------------------------------------------------------- #
# building and caching
# --------------------------------------------------------------------------- #

def annotate(analysis, index: Optional["Corpus"]) -> None:
    """Score every card in a deck for replaceability against the corpus.

    Peers are restricted to what this deck could actually cast, so the answer
    is "of the cards available to *you* that do this job at this cost, how many
    are more played than this one".
    """
    if index is None:
        return
    deck_mask = color_mask(analysis.commander_identity or analysis.color_identity)
    for entry in analysis.entries:
        if entry.is_land:
            continue
        entry.replaceability = index.better_fraction(
            sorted(entry.roles), entry.effective_mana_value,
            entry.edhrec_rank, deck_mask)


def _cache_file() -> str:
    return os.path.join(scryfall.cache_dir(), "card_corpus.json.gz")


def _read_cache() -> Optional[Corpus]:
    path = _cache_file()
    try:
        age = (time.time() - os.path.getmtime(path)) / 86400.0
    except OSError:
        return None
    if age > MAX_AGE_DAYS:
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if payload.get("schema") != CORPUS_SCHEMA:
        return None
    entries = {oid: (row[0], row[1], row[2], row[3], row[4])
               for oid, row in payload.get("entries", {}).items()}
    return Corpus(entries)


def build(progress=None) -> Corpus:
    """Download the bulk card file and index it. A few seconds, once a month."""
    if progress:
        progress("downloading Scryfall card corpus (about 25MB, once a month)")
    meta = json.loads(scryfall._http(scryfall.API + "/bulk-data/oracle-cards",
                                     timeout=60))
    uri = meta.get("jsonl_download_uri")
    if not uri:
        raise scryfall.ScryfallError("no oracle-cards download URI advertised")
    raw = scryfall._http(uri, timeout=300)

    tags = scryfall.oracle_tag_index()
    entries: Dict[str, tuple] = {}
    if progress:
        progress("indexing cards by role, cost and colour")
    with gzip.open(_bytes_reader(raw), "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            raw_card = json.loads(line)
            if (raw_card.get("legalities") or {}).get("commander") != "legal":
                continue
            rank = raw_card.get("edhrec_rank")
            if rank is None:
                continue        # unranked cards cannot place in a percentile
            card = scryfall._face_aware(raw_card)
            oracle_id = card.get("oracle_id")
            if not oracle_id:
                continue
            roles = sorted(card_roles(card, tags.get(oracle_id, [])))
            entries[oracle_id] = (int(rank), card["mana_value"],
                                  color_mask(card.get("color_identity") or []),
                                  roles, bool(raw_card.get("game_changer")))

    payload = {"schema": CORPUS_SCHEMA, "entries": entries}
    tmp = _cache_file() + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, _cache_file())
    return Corpus(entries)


def _bytes_reader(raw: bytes):
    import io
    return io.BytesIO(raw)


def load(offline: bool = False, refresh: bool = False,
         progress=None) -> Optional[Corpus]:
    """The corpus, or ``None`` if it is unavailable and cannot be built.

    Callers must cope with ``None``: the tool still works without it, just with
    a cruder sense of card quality.
    """
    if not refresh:
        cached = _read_cache()
        if cached is not None:
            return cached
    if offline:
        return None
    try:
        return build(progress=progress)
    except scryfall.ScryfallError:
        return None
