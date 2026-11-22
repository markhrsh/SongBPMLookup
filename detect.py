"""Synth keyword detection over Discogs release data."""

import re

import config

STRONG, POSSIBLE = "STRONG", "POSSIBLE"
TIER_RANK = {STRONG: 2, POSSIBLE: 1}


def _compile(keyword):
    words = [re.escape(w) for w in keyword.split()]
    pattern = r"(?<![a-z0-9])" + r"[\s-]+".join(words) + r"s?(?![a-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


RELEASE_NOTES_EXCLUSIONS = {kw.lower(): re.compile(rx, re.IGNORECASE)
                            for kw, rx in config.RELEASE_NOTES_EXCLUSIONS.items()}

PATTERNS = [(kw.lower(), STRONG, _compile(kw)) for kw in config.STRONG_KEYWORDS] + \
           [(kw.lower(), POSSIBLE, _compile(kw)) for kw in config.POSSIBLE_KEYWORDS]


def scan_text(text, exclusions=None):
    """Return non-overlapping hits [(start, end, keyword, tier)] in text order.

    Overlaps are resolved by keeping the longest span (ties go to STRONG), so
    "Electronic Keyboards" counts as STRONG only and "Fender Rhodes" counts once.
    """
    if not text:
        return []
    hits = []
    for kw, tier, rx in PATTERNS:
        ex = (exclusions or {}).get(kw)
        for m in rx.finditer(text):
            if ex:
                ctx = config.RELEASE_NOTES_CONTEXT
                window = text[max(0, m.start() - ctx):m.end() + ctx]
                if ex.search(window):
                    continue
            hits.append((m.start(), m.end(), kw, tier))
    hits.sort(key=lambda h: (-(h[1] - h[0]), -TIER_RANK[h[3]]))
    chosen = []
    for h in hits:
        if all(h[1] <= c[0] or h[0] >= c[1] for c in chosen):
            chosen.append(h)
    return sorted(chosen)


# --- synth instrument extraction from credit roles -------------------------
_SYNTH_HEADS = {"synthesizer", "synthesizers", "synth", "synths"}
_ALIASES = {k: v for k, v in config.INSTRUMENT_ALIASES.items()}
_IGNORE = {w.lower() for w in config.INSTRUMENT_IGNORE}
_ACRONYMS = re.compile(r"\b(arp|ems|ppg|dx\d+|cs-?\d+|ob-?x\w*)\b", re.IGNORECASE)
_CANON = {}   # key -> first display form seen, so "Poly-moog"/"Polymoog" share one name
_STRONG_RX = [rx for _, tier, rx in PATTERNS if tier == STRONG]


def _split_role(role):
    """'Organ, Synthesizer [Moog, ARP]' -> [('Organ', []), ('Synthesizer', ['Moog', 'ARP'])]"""
    parts, depth, buf = [], 0, ""
    for ch in role + ",":
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    out = []
    for p in parts:
        head, _, rest = p.partition("[")
        details = re.split(r",|&|\band\b", rest.rstrip("]"), flags=re.IGNORECASE) if rest else []
        out.append((head.strip(), [d.strip() for d in details if d.strip()]))
    return out


def _is_strong(text):
    return any(rx.search(text) for rx in _STRONG_RX)


def _key(name):
    return re.sub(r"[\s.\-]", "", name.lower())


def normalize_instrument(name):
    name = re.sub(r"\s+(programming\s+by|programmed\s+by|programming|programmer)$", "",
                  name.strip(), flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name)
    if not name or name.lower() in _IGNORE:
        return None
    key = _key(name)
    if key in _ALIASES:
        return _ALIASES[key]
    # "ARP 2600 Synthesizer" -> "ARP 2600", "Mini-Moog Synthesizer" -> "Minimoog"
    stripped = re.sub(r"\s+(synthesizers?|synths?)$", "", name, flags=re.IGNORECASE)
    if stripped != name and stripped.lower() not in _IGNORE:
        return normalize_instrument(stripped)
    return _CANON.setdefault(key, _ACRONYMS.sub(lambda m: m.group(0).upper(), name))


def instruments_from_role(role):
    """Synth instruments named in a credit role, e.g.
    'Synthesizer [ARP Odyssey]' -> ['ARP Odyssey'], 'Soloist [ARP Soloist]' -> ['ARP Soloist'],
    'Programmed By [Linn, Roland 808]' -> ['Roland 808'], 'Synthesizer' -> ['Synthesizer']."""
    found = []
    for head, details in _split_role(role or ""):
        h = head.lower()
        if h in _SYNTH_HEADS:
            named = [d for d in details if normalize_instrument(d)]
            found += named or [head]            # brackets name the synth; else generic
        elif h == "guitar" and any(d.lower() in _SYNTH_HEADS for d in details):
            found.append("Guitar Synthesizer")
        elif _is_strong(head):
            found.append(head)                  # e.g. Mellotron, Vocoder, Talkbox, Guitar Synthesizer
        else:
            found += [d for d in details if _is_strong(d)]   # e.g. Programmed By [Moog]
    out = []
    for f in found:
        n = normalize_instrument(f)
        if n and n not in out:
            out.append(n)
    return out


def merge_instruments(lists):
    """Combine per-credit lists; drop plain 'Synthesizer' when specific synths are named."""
    seen = {}
    for lst in lists:
        for name in lst:
            seen.setdefault(_key(name), name)
    names = list(seen.values())
    specific = [n for n in names if n != "Synthesizer"]
    return specific or names


def clean_name(artist):
    name = artist.get("anv") or artist.get("name") or ""
    return re.sub(r"\s+\(\d+\)$", "", name).strip()


def _snippet(text, start, end, width=40):
    a, b = max(0, start - width), min(len(text), end + width)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def _best_tier(hits):
    return max((h[3] for h in hits), key=TIER_RANK.get)


def _keywords(hits):
    return ", ".join(dict.fromkeys(h[2] for h in hits))


def _credit_matches(extraartists, source, default_position=""):
    out = []
    for ea in extraartists or []:
        role = ea.get("role") or ""
        hits = scan_text(role)
        if hits:
            out.append({
                "match_tier": _best_tier(hits),
                "source": source,
                "matched_text": role,
                "matched_keywords": _keywords(hits),
                "credited_person": clean_name(ea),
                "track_position": default_position or (ea.get("tracks") or ""),
                "instruments": instruments_from_role(role) if _best_tier(hits) == STRONG else [],
            })
    return out


def _walk_tracks(tracks):
    for t in tracks or []:
        yield t
        yield from _walk_tracks(t.get("sub_tracks"))


def _text_matches(text, source, gap=40, exclusions=None):
    """One row per cluster of hits; hits within `gap` chars are merged."""
    clusters = []
    for hit in scan_text(text, exclusions):
        if clusters and hit[0] - clusters[-1][-1][1] <= gap:
            clusters[-1].append(hit)
        else:
            clusters.append([hit])
    return [{
        "match_tier": _best_tier(c),
        "source": source,
        "matched_text": _snippet(text, c[0][0], c[-1][1]),
        "matched_keywords": _keywords(c),
        "credited_person": "",
        "track_position": "",
    } for c in clusters]


def find_matches(release, collection_item, field_names=None):
    """Return a list of match dicts for one release."""
    matches = []
    if release:
        matches += _credit_matches(release.get("extraartists"), "release credit")
        for t in _walk_tracks(release.get("tracklist")):
            pos = t.get("position") or t.get("title") or ""
            matches += _credit_matches(t.get("extraartists"), "track credit", pos or "?")
        matches += _text_matches(release.get("notes") or "", "release notes",
                                 exclusions=RELEASE_NOTES_EXCLUSIONS)
    field_names = field_names or {}
    seen = set()
    for note in collection_item.get("notes", []):
        value = str(note.get("value") or "")
        key = (note.get("field_id"), value)
        if not value or key in seen:
            continue
        seen.add(key)
        label = field_names.get(note.get("field_id"))
        for m in _text_matches(value, "my notes"):
            if label:
                m["matched_text"] = f"[{label}] {m['matched_text']}"
            matches.append(m)
    return matches
