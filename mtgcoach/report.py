"""Terminal rendering of an analysis."""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Sequence

from .advice import Recommendation, color_sources_needed, recommended_lands
from .classify import Classification
from .features import (COLOR_NAMES, COLORS, CURVE_BUCKETS, DeckAnalysis,
                       FEATURE_NAMES)
from .roles import ROLES, ROLES_BY_KEY

WIDTH = 78


class Style(object):
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled else text

    def bold(self, text): return self._wrap("1", text)
    def dim(self, text): return self._wrap("2", text)
    def red(self, text): return self._wrap("31", text)
    def yellow(self, text): return self._wrap("33", text)
    def green(self, text): return self._wrap("32", text)
    def cyan(self, text): return self._wrap("36", text)


def supports_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def bar(value: float, maximum: float, width: int = 28, fill: str = "#") -> str:
    if maximum <= 0:
        return ""
    filled = int(round(width * min(value / maximum, 1.0)))
    return fill * filled + "." * (width - filled)


def wrap(text: str, indent: int = 0, width: int = WIDTH) -> List[str]:
    import textwrap
    pad = " " * indent
    return textwrap.wrap(text, width=width - indent,
                         initial_indent=pad, subsequent_indent=pad) or [pad]


def _rule(title: str, st: Style) -> str:
    title = " %s " % title
    return st.bold(title + "-" * max(0, WIDTH - len(title)))


def render(analysis: DeckAnalysis, classification: Classification,
           recommendations: Sequence[Recommendation],
           deck_name: str = "", show_roles: int = 12,
           show_archetypes: int = 6, style: Optional[Style] = None,
           target=None) -> str:
    st = style or Style(False)
    out: List[str] = []
    add = out.append

    # ---- header ---------------------------------------------------------- #
    add("")
    add(st.bold("MTG COMMANDER DECK REPORT"))
    if deck_name:
        add(st.dim(deck_name))
    commanders = ", ".join(c.name for c in analysis.commanders) or "(none detected)"
    identity = "".join(analysis.commander_identity or analysis.color_identity) or "C"
    add("Commander: %s   |   Colour identity: %s" % (commanders, identity))
    if target is not None:
        add("Building toward: %s" % target.name)
    add("")

    # ---- overview -------------------------------------------------------- #
    add(_rule("OVERVIEW", st))
    target_lands = recommended_lands(analysis, classification)
    add("  %-22s %d" % ("Cards", analysis.total))
    add("  %-22s %d  (target for this curve: %d)"
        % ("Lands", analysis.land_count, target_lands))
    add("  %-22s %d" % ("Spells", analysis.nonland_count))
    add("  %-22s %.2f  (median %.1f)" % ("Average mana value", analysis.avg_mv,
                                         analysis.median_mv))
    add("  %-22s %d  (%d lands + %d ramp)"
        % ("Mana sources", analysis.mana_sources(), analysis.land_count,
           analysis.role_counts.get("ramp", 0)))
    types = ", ".join("%s %d" % (k, v) for k, v in sorted(
        analysis.type_counts.items(), key=lambda kv: -kv[1]))
    for line in wrap("Types: " + types, indent=2):
        add(line)
    add("")

    # ---- curve ----------------------------------------------------------- #
    add(_rule("MANA CURVE (spells only)", st))
    peak = max(analysis.curve.values() or [1])
    for bucket in CURVE_BUCKETS:
        count = analysis.curve.get(bucket, 0)
        add("  %2s | %-30s %2d" % (bucket, bar(count, peak, 30), count))
    add("")

    # ---- colours --------------------------------------------------------- #
    identity_colors = set(analysis.commander_identity or analysis.color_identity)
    active = [c for c in COLORS
              if analysis.pips.get(c) or c in identity_colors]
    if active:
        add(_rule("COLOUR REQUIREMENTS vs SOURCES", st))
        peak = max(max(analysis.pips.get(c, 0) for c in active),
                   max(analysis.color_sources.get(c, 0) for c in active), 1)
        for color in active:
            pips = analysis.pips.get(color, 0)
            srcs = analysis.color_sources.get(color, 0)
            needed = color_sources_needed(pips)
            flag = "  <- thin" if pips >= 8 and srcs < needed else ""
            add("  %-6s pips %3d  sources %3d  %s%s"
                % (COLOR_NAMES[color], pips, srcs, bar(srcs, peak, 20), flag))
        add("")

    # ---- roles ----------------------------------------------------------- #
    add(_rule("WHAT YOUR CARDS DO", st))
    ranked = [(analysis.role_counts.get(r.key, 0), r) for r in ROLES]
    ranked = [(count, role) for count, role in ranked if count]
    ranked.sort(key=lambda item: -item[0])
    peak = ranked[0][0] if ranked else 1
    for count, role in ranked[:show_roles]:
        share = 100.0 * count / (analysis.nonland_count or 1)
        add("  %-26s %3d  %4.0f%%  %s"
            % (role.label, count, share, bar(count, peak, 22)))
    add(st.dim("  (a card can fill several roles; percentages are of your %d "
               "spells)" % analysis.nonland_count))
    add("")

    # ---- archetypes ------------------------------------------------------ #
    add(_rule("ARCHETYPE DISTANCE", st))
    add(st.dim("  affinity = share of the deck's identity; fit = how close the "
               "match is"))
    for match in classification.top(show_archetypes):
        add("  %-24s %5.1f%%  fit %3.0f%%  d=%.3f  %s"
            % (match.archetype.name, 100 * match.affinity, 100 * match.fit,
               match.distance, bar(match.affinity, 1.0, 20)))
    add("")
    best = classification.best
    if target is not None and target.key != best.archetype.key:
        aimed = next((m for m in classification.matches
                      if m.archetype.key == target.key), None)
        if aimed is not None:
            for line in wrap(
                    "You are aiming at %s (%.0f%% affinity, fit %.0f%%) but "
                    "the deck currently reads as %s."
                    % (target.name, 100 * aimed.affinity, 100 * aimed.fit,
                       best.archetype.name), indent=2):
                add(line)
            add("")
    for line in wrap("Closest: %s - %s" % (best.archetype.name,
                                           best.archetype.blurb), indent=2):
        add(line)
    for line in wrap("Game plan: " + best.archetype.plan, indent=2):
        add(line)
    focus = classification.focus
    label = ("sharply focused" if focus >= 0.70 else
             "reasonably focused" if focus >= 0.45 else "unfocused")
    add("  Focus score: %.2f (%s)" % (focus, label))
    add("")

    # ---- recommendations ------------------------------------------------- #
    add(_rule("RECOMMENDATIONS", st))
    if not recommendations:
        add("  Nothing pressing - the fundamentals look sound.")
    colorize = {"high": st.red, "medium": st.yellow, "low": st.cyan}
    for rec in recommendations:
        tag = colorize.get(rec.priority, st.dim)("[%s]" % rec.priority.upper())
        add("  %s %s" % (tag, st.bold(rec.title)))
        if rec.detail:
            for line in wrap(rec.detail, indent=6):
                add(line)
        if rec.evidence:
            add(st.dim("      (%s)" % rec.evidence))
        add("")

    # ---- footnotes ------------------------------------------------------- #
    notes = []
    if analysis.untagged:
        notes.append("%d card(s) had no Scryfall Tagger data; their roles came "
                     "from rules text alone." % analysis.untagged)
    if analysis.unresolved:
        notes.append("%d card name(s) did not resolve and were excluded."
                     % len(analysis.unresolved))
    if notes:
        add(_rule("NOTES", st))
        for note in notes:
            for line in wrap("- " + note, indent=2):
                add(line)
        add("")

    return "\n".join(out)


def render_archetypes(style: Optional[Style] = None) -> str:
    from .archetypes import ARCHETYPES
    st = style or Style(False)
    out = [""]
    out.append(st.bold("ARCHETYPE REFERENCE PROFILES"))
    out.append("")
    for arch in ARCHETYPES:
        out.append(st.bold("  %s  (%s)" % (arch.name, arch.key)))
        if arch.blurb:
            out.extend(wrap(arch.blurb, indent=4))
        if arch.plan:
            out.extend(wrap("Plan: " + arch.plan, indent=4))
        top = sorted(arch.profile.items(), key=lambda kv: -kv[1])[:6]
        out.append("    Profile: " + ", ".join(
            "%s %.0f%%" % (ROLES_BY_KEY[k].label if k in ROLES_BY_KEY
                           else k.replace("_", " "), 100 * v)
            for k, v in top))
        out.append("")
    return "\n".join(out)


def render_roles(style: Optional[Style] = None) -> str:
    st = style or Style(False)
    out = ["", st.bold("FUNCTIONAL ROLE VOCABULARY"), ""]
    for role in ROLES:
        marker = "*" if role.axis else " "
        out.append("  %s %s" % (marker, role.label))
        out.extend(wrap(role.blurb, indent=6))
    out.append("")
    out.append(st.dim("  * = also used as a dimension of the archetype "
                      "distance calculation"))
    out.append("")
    return "\n".join(out)


def to_json(analysis: DeckAnalysis, classification: Classification,
            recommendations: Sequence[Recommendation],
            deck_name: str = "", target=None) -> Dict:
    return {
        "deck": deck_name,
        "target_archetype": target.key if target is not None else None,
        "commanders": [c.name for c in analysis.commanders],
        "color_identity": analysis.commander_identity or analysis.color_identity,
        "stats": {
            "total_cards": analysis.total,
            "lands": analysis.land_count,
            "spells": analysis.nonland_count,
            "recommended_lands": recommended_lands(analysis, classification),
            "average_mana_value": round(analysis.avg_mv, 3),
            "median_mana_value": analysis.median_mv,
            "mana_sources": analysis.mana_sources(),
            "curve": dict(analysis.curve),
            "types": dict(analysis.type_counts),
            "pips": dict(analysis.pips),
            "color_sources": dict(analysis.color_sources),
        },
        "roles": {key: {"count": count,
                        "share": round(analysis.role_share(key), 4)}
                  for key, count in sorted(analysis.role_counts.items())
                  if count},
        "feature_vector": {k: round(v, 4) for k, v in analysis.vector.items()},
        "classification": {
            "focus": round(classification.focus, 4),
            "separation": round(classification.separation, 4),
            "matches": [
                {"archetype": m.archetype.key,
                 "name": m.archetype.name,
                 "distance": round(m.distance, 4),
                 "affinity": round(m.affinity, 4),
                 "fit": round(m.fit, 4),
                 "cosine": round(m.cosine, 4)}
                for m in classification.matches
            ],
        },
        "recommendations": [r.as_dict() for r in recommendations],
        "legality": list(analysis.legality),
        "unresolved": list(analysis.unresolved),
    }
