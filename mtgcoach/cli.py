"""Command line interface.

    python3 -m mtgcoach analyze decks/my_deck.txt
    python3 -m mtgcoach analyze - < decklist.txt
    python3 -m mtgcoach archetypes
    python3 -m mtgcoach roles
    python3 -m mtgcoach card "Beast Within"
    python3 -m mtgcoach fit aristocrats decks/*.txt --out profiles.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from . import archetypes as archetypes_mod
from . import advice, classify, decklist, features, report, scryfall
from .roles import ROLES, card_roles


def _progress(quiet: bool):
    if quiet:
        return lambda message: None
    return lambda message: sys.stderr.write("  ... %s\n" % message)


def _load_analysis(path: str, args) -> tuple:
    if path == "-":
        parsed = decklist.parse_decklist(sys.stdin.read(), source="<stdin>",
                                         commander_override=args.commander)
        label = "<stdin>"
    else:
        parsed = decklist.load_decklist(path, commander_override=args.commander)
        label = path

    if not parsed.entries:
        raise SystemExit("error: no cards found in %s" % label)

    progress = _progress(args.quiet)
    cards, missing = scryfall.fetch_cards(parsed.unique_names(),
                                          offline=args.offline,
                                          progress=progress)
    tags = scryfall.oracle_tag_index(offline=args.offline,
                                     refresh=args.refresh_tags,
                                     progress=progress)
    analysis = features.analyze(parsed, cards, tags)
    return parsed, analysis, label


def cmd_analyze(args) -> int:
    if args.profiles:
        archetypes_mod.load_profiles(args.profiles)

    target = None
    if args.target:
        target = archetypes_mod.ARCHETYPES_BY_KEY.get(args.target)
        if target is None:
            raise SystemExit("error: unknown archetype %r (see 'mtgcoach "
                             "archetypes')" % args.target)

    _, analysis, label = _load_analysis(args.deck, args)
    classification = classify.classify(analysis.vector)
    recommendations = advice.all_recommendations(analysis, classification,
                                                 blend_top=args.blend,
                                                 target_archetype=target)
    cuts = advice.cut_candidates(analysis, classification, blend_top=args.blend,
                                 target_archetype=target, limit=args.cuts)

    if args.json:
        payload = report.to_json(analysis, classification, recommendations,
                                 label, target=target, cuts=cuts)
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            sys.stderr.write("wrote %s\n" % args.json)
        if args.json != "-":
            print(report.render(analysis, classification, recommendations, label,
                                show_roles=args.roles, show_archetypes=args.top,
                                style=report.Style(_use_color(args)),
                                target=target, cuts=cuts))
        return 0

    print(report.render(analysis, classification, recommendations, label,
                        show_roles=args.roles, show_archetypes=args.top,
                        style=report.Style(_use_color(args)), target=target,
                        cuts=cuts))
    return 0


def _use_color(args) -> bool:
    if args.no_color:
        return False
    return report.supports_color()


def cmd_archetypes(args) -> int:
    if args.profiles:
        archetypes_mod.load_profiles(args.profiles)
    print(report.render_archetypes(report.Style(_use_color(args))))
    return 0


def cmd_roles(args) -> int:
    print(report.render_roles(report.Style(_use_color(args))))
    return 0


def cmd_card(args) -> int:
    cards, missing = scryfall.fetch_cards(args.name, offline=args.offline,
                                          progress=_progress(args.quiet))
    tags = scryfall.oracle_tag_index(offline=args.offline,
                                     progress=_progress(args.quiet))
    for name in args.name:
        card = cards.get(scryfall.normalize_name(name))
        if card is None:
            print("%s: not found" % name)
            continue
        card_tags = tags.get(card.get("oracle_id") or "", [])
        roles = sorted(card_roles(card, card_tags))
        print("")
        print("%s  %s" % (card["name"], card.get("mana_cost", "")))
        print("  %s" % card.get("type_line", ""))
        print("  roles: %s" % (", ".join(roles) or "(none)"))
        print("  scryfall tags: %s" % (", ".join(card_tags) or "(none)"))
    print("")
    return 0


def cmd_fit(args) -> int:
    """Average several decks into an archetype profile (empirical centroid)."""
    vectors = []
    for path in args.decks:
        _, analysis, label = _load_analysis(path, args)
        vectors.append(analysis.vector)
        sys.stderr.write("  measured %s\n" % label)
    if not vectors:
        raise SystemExit("error: no decks measured")

    profile = {}
    for name in features.FEATURE_NAMES:
        profile[name] = round(
            sum(v.get(name, 0.0) for v in vectors) / len(vectors), 4)

    payload = {}
    if args.out and args.merge:
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            payload = {}
    payload[args.key] = {
        "name": args.name or args.key.replace("_", " ").title(),
        "blurb": args.blurb or "",
        "plan": args.plan or "",
        "profile": profile,
        "fitted_from": [str(d) for d in args.decks],
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        sys.stderr.write("wrote %s (%d deck(s))\n" % (args.out, len(vectors)))
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtgcoach",
        description="Descriptive stats and archetype coaching for Magic: The "
                    "Gathering Commander decks.")
    parser.add_argument("--offline", action="store_true",
                        help="use only cached Scryfall data")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="suppress progress messages")
    parser.add_argument("--no-color", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    analyze = subparsers.add_parser("analyze", help="analyse a decklist")
    analyze.add_argument("deck", help="path to a decklist, or - for stdin")
    analyze.add_argument("--commander", action="append", default=None,
                         help="name your commander (repeat for partners)")
    analyze.add_argument("--top", type=int, default=6,
                         help="how many archetypes to list (default 6)")
    analyze.add_argument("--roles", type=int, default=12,
                         help="how many roles to list (default 12)")
    analyze.add_argument("--cuts", type=int, default=8,
                         help="how many cut candidates to list (default 8)")
    analyze.add_argument("--blend", type=int, default=2,
                         help="how many archetypes to blend into the target "
                              "profile (default 2)")
    analyze.add_argument("--target", metavar="ARCHETYPE",
                         help="the archetype you are trying to build; advice "
                              "is measured against it instead of against the "
                              "deck's own nearest match")
    analyze.add_argument("--json", metavar="PATH",
                         help="also write the full analysis as JSON (- for stdout)")
    analyze.add_argument("--profiles", metavar="FILE",
                         help="load archetype profiles produced by 'fit'")
    analyze.add_argument("--refresh-tags", action="store_true",
                         help="re-download the Scryfall oracle tag data")
    analyze.set_defaults(func=cmd_analyze)

    arche = subparsers.add_parser("archetypes",
                                  help="show the reference archetype profiles")
    arche.add_argument("--profiles", metavar="FILE")
    arche.set_defaults(func=cmd_archetypes)

    roles_cmd = subparsers.add_parser("roles",
                                      help="show the functional role vocabulary")
    roles_cmd.set_defaults(func=cmd_roles)

    card = subparsers.add_parser("card",
                                 help="show the tags and roles of single cards")
    card.add_argument("name", nargs="+")
    card.set_defaults(func=cmd_card)

    fit = subparsers.add_parser(
        "fit", help="build an archetype profile by averaging example decks")
    fit.add_argument("key", help="archetype key, e.g. my_aristocrats")
    fit.add_argument("decks", nargs="+")
    fit.add_argument("--name")
    fit.add_argument("--blurb")
    fit.add_argument("--plan")
    fit.add_argument("--out", metavar="FILE")
    fit.add_argument("--merge", action="store_true",
                     help="merge into an existing profiles file")
    fit.add_argument("--commander", action="append", default=None)
    fit.add_argument("--refresh-tags", action="store_true")
    fit.set_defaults(func=cmd_fit)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Convenience: `mtgcoach deck.txt` means `mtgcoach analyze deck.txt`.
    known = {"analyze", "archetypes", "roles", "card", "fit"}
    global_flags = {"--offline", "--quiet", "-q", "--no-color"}
    if argv and not any(token in known for token in argv):
        index = 0
        while index < len(argv) and argv[index] in global_flags:
            index += 1
        argv.insert(index, "analyze")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except scryfall.OfflineError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except scryfall.ScryfallError as exc:
        sys.stderr.write("error: could not reach Scryfall: %s\n" % exc)
        return 2
    except FileNotFoundError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
