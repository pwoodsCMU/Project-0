"""Partial (soft) classification of a deck against the archetype profiles.

A deck is rarely one thing, so instead of picking a single label we report a
*mixture*: a weighted distance to every archetype, turned into affinity
percentages.  A deck that is 55% Aristocrats / 25% Go-Wide Aggro is a more
honest description than "Aristocrats", and the recommendations downstream use
that mixture rather than a single winner.

Distance is a weighted Euclidean distance, normalised by the total weight, so
it reads as a root-mean-square deviation per feature:

    d(deck, archetype) = sqrt( sum_i w_i (x_i - c_i)^2 / sum_i w_i )

Typical values: ~0.04 for a deck that really is the archetype, ~0.10 for a
loose fit, >0.15 for "not this at all".
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .archetypes import ARCHETYPES, Archetype
from .features import FEATURE_NAMES, FEATURE_WEIGHTS

# Softmax temperature over distances.  Small enough that a clear archetype
# dominates, large enough that genuinely hybrid decks show both halves.
TEMPERATURE = 0.025

# A deck this far from every archetype has no recognisable plan at all.
MAX_MEANINGFUL_DISTANCE = 0.20


class Match(object):
    __slots__ = ("archetype", "distance", "affinity", "cosine", "gaps")

    def __init__(self, archetype: Archetype, distance: float, affinity: float,
                 cosine: float, gaps: Dict[str, float]):
        self.archetype = archetype
        self.distance = distance
        self.affinity = affinity          # 0..1, sums to 1 across archetypes
        self.cosine = cosine
        self.gaps = gaps                  # feature -> (archetype - deck)

    @property
    def fit(self) -> float:
        """0..1 readability score for "how well does this archetype fit?"."""
        return max(0.0, 1.0 - self.distance / MAX_MEANINGFUL_DISTANCE)


class Classification(object):
    def __init__(self, matches: List[Match]):
        self.matches = matches            # sorted, closest first

    @property
    def best(self) -> Match:
        return self.matches[0]

    def top(self, n: int = 3) -> List[Match]:
        return self.matches[:n]

    @property
    def focus(self) -> float:
        """1.0 = the deck is squarely one archetype, 0.0 = evenly spread.

        This is 1 minus the normalised entropy of the affinity distribution: a
        direct read on whether the deck has committed to a plan.
        """
        probs = [m.affinity for m in self.matches if m.affinity > 1e-9]
        if len(probs) <= 1:
            return 1.0
        entropy = -sum(p * math.log(p) for p in probs)
        return max(0.0, min(1.0, 1.0 - entropy / math.log(len(self.matches))))

    @property
    def separation(self) -> float:
        """Affinity gap between the best and second-best archetype."""
        if len(self.matches) < 2:
            return 1.0
        return self.matches[0].affinity - self.matches[1].affinity


def _weights() -> Dict[str, float]:
    return {name: FEATURE_WEIGHTS.get(name, 1.0) for name in FEATURE_NAMES}


def distance(deck_vector: Dict[str, float], profile: Dict[str, float]) -> float:
    weights = _weights()
    total_w = sum(weights.values())
    acc = 0.0
    for name in FEATURE_NAMES:
        delta = deck_vector.get(name, 0.0) - profile.get(name, 0.0)
        acc += weights[name] * delta * delta
    return math.sqrt(acc / total_w)


def cosine(deck_vector: Dict[str, float], profile: Dict[str, float]) -> float:
    weights = _weights()
    dot = num_a = num_b = 0.0
    for name in FEATURE_NAMES:
        w = weights[name]
        a = deck_vector.get(name, 0.0) * w
        b = profile.get(name, 0.0) * w
        dot += a * b
        num_a += a * a
        num_b += b * b
    if num_a <= 0 or num_b <= 0:
        return 0.0
    return dot / math.sqrt(num_a * num_b)


def classify(deck_vector: Dict[str, float],
             archetypes: Optional[Sequence[Archetype]] = None,
             temperature: float = TEMPERATURE) -> Classification:
    pool = list(archetypes if archetypes is not None else ARCHETYPES)
    distances = [(a, distance(deck_vector, a.profile)) for a in pool]
    best = min(d for _, d in distances)

    exps = [math.exp(-(d - best) / temperature) for _, d in distances]
    total = sum(exps) or 1.0

    matches = []
    for (arch, dist), weight in zip(distances, exps):
        gaps = {name: arch.profile.get(name, 0.0) - deck_vector.get(name, 0.0)
                for name in FEATURE_NAMES}
        matches.append(Match(arch, dist, weight / total,
                             cosine(deck_vector, arch.profile), gaps))
    matches.sort(key=lambda m: m.distance)
    return Classification(matches)


# A second archetype only joins the blend if it is a genuinely good match in
# its own right.  Rank alone is not enough: every deck has a second-nearest
# archetype, and for a focused deck that runner-up is usually junk.
BLEND_MIN_AFFINITY = 0.15
BLEND_MIN_FIT = 0.40


def blend_matches(classification: Classification, top_n: int = 2,
                  min_affinity: float = BLEND_MIN_AFFINITY,
                  min_fit: float = BLEND_MIN_FIT) -> List[Match]:
    """The archetypes worth steering toward - the best one, plus any close
    runner-up that is a real match rather than merely second in line.

    ``top_n`` is a maximum, not a quota.  A deck that is squarely one thing
    gets a single archetype back, so its advice is not diluted by a 4%-affinity
    runner-up it has nothing in common with.
    """
    matches = classification.matches
    kept = [matches[0]]
    for match in matches[1:top_n]:
        if match.affinity >= min_affinity and match.fit >= min_fit:
            kept.append(match)
    return kept


def blended_target(classification: Classification, top_n: int = 2) -> Dict[str, float]:
    """The feature vector the deck is being steered toward.

    Recommendations compare the deck against a blend of its nearest archetypes
    rather than a single one, so a genuinely hybrid deck is not told to abandon
    half of itself.
    """
    top = blend_matches(classification, top_n)
    if len(top) == 1:
        return dict(top[0].archetype.profile)
    # Soften the affinities (sqrt) before blending: a 70/10 split should not
    # produce a target that is 88% the winner, or hybrid decks get told to
    # abandon their second half.
    weights = [math.sqrt(m.affinity) for m in top]
    total = sum(weights) or 1.0
    target: Dict[str, float] = {}
    for name in FEATURE_NAMES:
        target[name] = sum(w * m.archetype.profile.get(name, 0.0)
                           for w, m in zip(weights, top)) / total
    return target
