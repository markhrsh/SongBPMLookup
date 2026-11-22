"""Editable settings for the Discogs synth finder."""

from pathlib import Path

# --- Discogs ---------------------------------------------------------------
USERNAME = "markhrsh"
USER_AGENT = "MarkSynthFinder/1.0"
BASE_URL = "https://api.discogs.com"

# --- Filter: a release is kept if ANY genre or ANY style matches ------------
FILTER_GENRES = ["Funk / Soul"]
FILTER_STYLES = ["Jazz-Funk"]

# --- Synth keywords (case-insensitive, whole-word matching) ----------------
STRONG_KEYWORDS = [
    "synthesizer", "synth", "moog", "minimoog", "arp", "oberheim", "prophet",
    "sequential circuits", "mellotron", "vocoder", "clavinet", "polymoog",
    "jupiter", "juno", "yamaha dx", "dx7", "korg", "roland", "talk box",
    "electronic keyboards"
]

POSSIBLE_KEYWORDS = [
    "keyboards", "electric piano", "fender rhodes", "rhodes", "organ",
]

# --- Paths -----------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "cache" / "releases"
OUTPUT_DIR = PROJECT_DIR / "output"

TOP_PLAYERS = 15
