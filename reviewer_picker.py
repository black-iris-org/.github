#!/usr/bin/env python3
"""
Beacon PR reviewer picker.

Pure logic, no GitHub/Slack calls. Give it facts about a PR; it returns two
reviewers (slot 1 = risk seat, slot 2 = round-robin) plus the reasoning.

Wire it into the PR-open GitHub Action later. Test it standalone now:
    python3 reviewer_picker.py        # runs the demo cases at the bottom

Tier ladder (severity, not backend-vs-frontend):
  Tier 1 -> Yazan      (Olga steps in when Yazan is away/is the author)
  Tier 2 -> Olga       (Yazan backs up when Olga is away/is the author)
  Tier 3 -> Shabab / Farhad / Nick   (round-robin, the default home)
  Tier 4 -> Alex / Gillian folded into Tier 3 ~1-in-3 on trivial PRs only
"""

from __future__ import annotations

import re
import fnmatch
from dataclasses import dataclass, field
from datetime import date

# ---------------------------------------------------------------------------
# PEOPLE  ->  GitHub handles
# ---------------------------------------------------------------------------
YAZAN = "yazan9"
OLGA = "Olga-h-h"
SHABAB = "shababmoali"
FARHAD = "farhad-blackiris"
NICK = "NickEgorenkovBlackIris"
GILLIAN = "gillian-blackiris"
ALEX = "realkalash"

TIER3_POOL = [SHABAB, FARHAD, NICK]   # normal backend + frontend, round-robin
THROTTLE_IN = [ALEX, GILLIAN]         # folded into slot 2 ~1-in-3 on trivial PRs
THROTTLE_EVERY = 3                    # 1-in-3

# When the Tier-3 pool can't produce a second reviewer (author out, someone
# away), fall back to Olga for slot 2 — but only if she isn't already buried.
# "Buried" = this many or more open review requests already on her plate.
# If no review_load data is supplied, we can't tell, so we pull her in.
OLGA_OVERLOAD_CAP = 5

# ===========================================================================
# >>> TUNE THESE TO BEACON'S ACTUAL REPO LAYOUT <<<
# These are educated guesses for a Rails + Angular repo. Adjust the globs /
# regexes to match where things really live in Beacon, then everything else
# downstream just works.
# ===========================================================================

# --- Tier 1: always you (or Olga when you're out) -------------------------

MIGRATION_GLOBS = [
    "db/migrate/*",
    "db/schema.rb",
    "db/structure.sql",
]

# Model association changes are detected from DIFF CONTENT, not just the path,
# so a model file that only tweaks a method doesn't false-trigger.
MODEL_FILE_GLOBS = ["app/models/**", "app/models/*"]
ASSOCIATION_KEYWORDS = re.compile(
    r"\b(has_many|has_one|belongs_to|has_and_belongs_to_many)\b"
)

# New env variables / secret keys.
ENV_FILE_GLOBS = [
    ".env*",
    "config/application.yml",
    "config/settings*.yml",
    "config/credentials*",
    "**/*.env",
]
# Also catch newly-referenced ENV[...] in any changed file's diff.
ENV_REFERENCE = re.compile(r"""ENV\[['"]([A-Z0-9_]+)['"]\]""")

# Deployment-affecting changes.
DEPLOY_GLOBS = [
    "Dockerfile*",
    "docker-compose*.yml",
    ".github/workflows/*",
    "*.tf",
    "**/*.tf",
    "ecs/**",
    "**/*.task.json",
    "**/nginx*.conf",
    "**/nginx/**",
    "Procfile",
    "bin/deploy*",
]

# --- Tier 2: Olga (you backup) --------------------------------------------

ANGULAR_MODULE_GLOBS = ["**/*.module.ts"]
WEBSOCKET_MQTT_GLOBS = [
    "app/channels/**",            # Rails ActionCable
    "**/*mqtt*",
    "**/*websocket*",
    "**/*cable*",
]
SERVICE_GLOBS = [
    "**/*.service.ts",            # Angular services
    "app/services/**",            # Rails service objects
]
AUTH_GLOBS = [
    "**/*auth*",
    "**/*keycloak*",
    "**/*devise*",
    "**/*session*",
]

# "Big / sprawling" heuristic for Tier 2.
SPRAWL_DISTINCT_AREAS = 4         # touches >= this many top-level dirs
SPRAWL_FILE_COUNT = 25            # or >= this many files

# --- "Trivial" definition for the Tier-4 throttle -------------------------
TRIVIAL_MAX_FILES = 5             # small PR, no triggers fired -> trivial

# ===========================================================================
# END TUNING BLOCK
# ===========================================================================


@dataclass
class PR:
    number: int
    author: str                              # GitHub handle of the author
    changed_files: list[str]                 # paths relative to repo root
    diff_text: str = ""                      # optional; enables precise checks
    claude_critical: bool = False            # Claude Code's escalate flag
    claude_reason: str = ""                  # one-line why, if flagged
    already_requested: list[str] = field(default_factory=list)
    # handle -> count of that person's currently-open review requests.
    # Used to load-balance slot 2. Empty dict => fall back to PR-number rotation.
    review_load: dict[str, int] = field(default_factory=dict)


@dataclass
class Pick:
    reviewers: list[str]
    tier: int
    reasons: list[str]

    def summary(self) -> str:
        who = ", ".join(self.reviewers) if self.reviewers else "(none)"
        why = "; ".join(self.reasons) if self.reasons else "no triggers"
        return f"Tier {self.tier} -> {who}  [{why}]"


# ---------------------------------------------------------------------------
# Away-list
# ---------------------------------------------------------------------------
def parse_away_list(yaml_text: str, today: date | None = None) -> set[str]:
    """
    Read unavailable.yml. Returns the set of handles currently away.
    Entries whose `until` date has passed are ignored automatically.

    Expected shape:
        - dev: nick
          until: 2026-07-15

    NOTE: `dev` values must be GitHub handles (or map them here). Done without
    a YAML lib so the picker has zero dependencies; swap in PyYAML if you like.
    """
    today = today or date.today()
    away: set[str] = set()
    cur_dev: str | None = None
    for raw in yaml_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- dev:") or line.startswith("dev:"):
            cur_dev = line.split(":", 1)[1].strip()
        elif line.startswith("until:") and cur_dev:
            until_str = line.split(":", 1)[1].strip()
            try:
                until = date.fromisoformat(until_str)
                if until >= today:
                    away.add(cur_dev)
            except ValueError:
                # Unparseable date -> treat as away (fail safe, not silent).
                away.add(cur_dev)
            cur_dev = None
    return away


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------
def _any_match(paths: list[str], globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(p, g) for p in paths for g in globs)


def _matches(paths: list[str], globs: list[str]) -> list[str]:
    return [p for p in paths if any(fnmatch.fnmatch(p, g) for g in globs)]


def detect_tier1(pr: PR) -> list[str]:
    reasons: list[str] = []

    if _matches(pr.changed_files, MIGRATION_GLOBS):
        reasons.append("DB migration / schema change")

    # Model association change: model file touched AND diff adds/removes an
    # association. If no diff is supplied, fall back to flagging any model
    # change (conservative — better an extra senior look than a missed one).
    model_files = _matches(pr.changed_files, MODEL_FILE_GLOBS)
    if model_files:
        if pr.diff_text:
            added_or_removed = [
                ln for ln in pr.diff_text.splitlines()
                if ln[:1] in "+-" and ASSOCIATION_KEYWORDS.search(ln)
            ]
            if added_or_removed:
                reasons.append("model association change")
        else:
            reasons.append("model file changed (no diff to confirm association)")

    if _matches(pr.changed_files, ENV_FILE_GLOBS):
        reasons.append("env / secrets file changed")
    elif pr.diff_text:
        added_env = [
            m.group(1)
            for ln in pr.diff_text.splitlines() if ln.startswith("+")
            for m in [ENV_REFERENCE.search(ln)] if m
        ]
        if added_env:
            reasons.append(f"new env var referenced ({', '.join(sorted(set(added_env)))})")

    if _matches(pr.changed_files, DEPLOY_GLOBS):
        reasons.append("deployment-affecting change")

    if pr.claude_critical:
        reasons.append(f"Claude flagged critical: {pr.claude_reason or 'no reason given'}")

    return reasons


def detect_tier2(pr: PR) -> list[str]:
    reasons: list[str] = []
    if _matches(pr.changed_files, ANGULAR_MODULE_GLOBS):
        reasons.append("Angular module change")
    if _matches(pr.changed_files, WEBSOCKET_MQTT_GLOBS):
        reasons.append("websocket / MQTT change")
    if _matches(pr.changed_files, SERVICE_GLOBS):
        reasons.append("service change")
    if _matches(pr.changed_files, AUTH_GLOBS):
        reasons.append("auth change")

    top_dirs = {p.split("/", 1)[0] for p in pr.changed_files}
    if len(top_dirs) >= SPRAWL_DISTINCT_AREAS or len(pr.changed_files) >= SPRAWL_FILE_COUNT:
        reasons.append(
            f"large/sprawling PR ({len(pr.changed_files)} files, {len(top_dirs)} areas)"
        )
    return reasons


# ---------------------------------------------------------------------------
# Round-robin / load-balanced pool draw
# ---------------------------------------------------------------------------
def draw_from_pool(pool: list[str], pr: PR, exclude: set[str]) -> str | None:
    """
    Pick one reviewer from `pool`, skipping anyone in `exclude` (author,
    away devs, already-picked). Least-loaded first using review_load;
    ties (and missing load data) broken deterministically by PR number so
    it rotates instead of always hitting the same person.
    """
    candidates = [p for p in pool if p not in exclude]
    if not candidates:
        return None
    if pr.review_load:
        candidates.sort(key=lambda h: (pr.review_load.get(h, 0), h))
        least = pr.review_load.get(candidates[0], 0)
        tied = [h for h in candidates if pr.review_load.get(h, 0) == least]
        return tied[pr.number % len(tied)]
    return candidates[pr.number % len(candidates)]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def pick_reviewers(pr: PR, away: set[str] | None = None) -> Pick:
    away = away or set()
    reasons: list[str] = []

    # Who can't be picked, ever, for this PR.
    base_exclude = set(away) | {pr.author} | set(pr.already_requested)

    t1 = detect_tier1(pr)
    t2 = detect_tier2(pr)
    tier = 1 if t1 else (2 if t2 else 3)
    reasons.extend(t1 + t2)

    # --- Slot 1: the risk seat -------------------------------------------
    slot1: str | None = None
    if tier == 1:
        # Yazan, with Olga stepping in when he's away OR is the author.
        if YAZAN not in base_exclude:
            slot1 = YAZAN
        elif OLGA not in (set(away) | set(pr.already_requested)) and pr.author != OLGA:
            slot1 = OLGA
            reasons.append("Yazan unavailable/author -> Olga steps into Tier 1")
        else:
            reasons.append("WARNING: both Yazan and Olga unavailable for Tier 1 — PR will wait")
    elif tier == 2:
        # Olga primary, Yazan backs up.
        if OLGA not in base_exclude:
            slot1 = OLGA
        elif YAZAN not in (set(away) | set(pr.already_requested)) and pr.author != YAZAN:
            slot1 = YAZAN
            reasons.append("Olga unavailable/author -> Yazan backs up Tier 2")
        else:
            slot1 = draw_from_pool(TIER3_POOL, pr, base_exclude)
            reasons.append("both seniors unavailable -> Tier 3 covers")
    else:
        # No triggers: slot 1 just comes from the normal pool.
        slot1 = draw_from_pool(TIER3_POOL, pr, base_exclude)

    # --- Slot 2: always round-robin from the qualified pool --------------
    exclude2 = set(base_exclude)
    if slot1:
        exclude2.add(slot1)

    # Build slot-2 pool. Trivial PRs fold in Alex/Gillian ~1-in-3.
    is_trivial = (tier == 3) and (len(pr.changed_files) <= TRIVIAL_MAX_FILES)
    slot2_pool = list(TIER3_POOL)
    if is_trivial and (pr.number % THROTTLE_EVERY == 0):
        slot2_pool += THROTTLE_IN
        reasons.append("trivial PR — Alex/Gillian eligible (1-in-3 throttle)")

    slot2 = draw_from_pool(slot2_pool, pr, exclude2)

    # Fallback: pool couldn't fill slot 2 (author out, someone away, thin pool).
    # Pull in Olga as the second reviewer if she's free and not overwhelmed.
    if slot2 is None and OLGA not in exclude2:
        olga_buried = (
            bool(pr.review_load) and pr.review_load.get(OLGA, 0) >= OLGA_OVERLOAD_CAP
        )
        if olga_buried:
            reasons.append(
                f"pool couldn't fill slot 2 — Olga is at capacity "
                f"({pr.review_load.get(OLGA, 0)} open), leaving one reviewer"
            )
        else:
            slot2 = OLGA
            reasons.append("pool couldn't fill slot 2 — Olga pulled in as second")

    reviewers = [r for r in (slot1, slot2) if r]
    # De-dup defensively (GitHub no-ops a repeat anyway).
    seen: list[str] = []
    for r in reviewers:
        if r not in seen:
            seen.append(r)

    return Pick(reviewers=seen, tier=tier, reasons=reasons)


# ---------------------------------------------------------------------------
# CLI — how the GitHub Action talks to this.
#
#   echo '<json>' | python3 reviewer_picker.py --away unavailable.yml
#
# Input JSON (stdin):
#   {
#     "number": 142,
#     "author": "shababmoali",
#     "changed_files": ["db/migrate/x.rb", "app/models/incident.rb"],
#     "diff_text": "+ has_many :dispatches",          # optional
#     "claude_critical": false,                          # from the API scan
#     "claude_reason": "",
#     "already_requested": [],
#     "review_load": {"Olga-h-h": 2}                     # optional
#   }
#
# Output JSON (stdout):
#   {"reviewers": ["yazan9", "farhad-blackiris"], "tier": 1, "reasons": [...]}
# ---------------------------------------------------------------------------
def _run_cli(argv: list[str]) -> int:
    import argparse, json, sys

    ap = argparse.ArgumentParser(description="Pick PR reviewers for Beacon.")
    ap.add_argument("--away", help="path to unavailable.yml", default=None)
    ap.add_argument("--input", help="path to PR-facts JSON, or '-' for stdin",
                    default="-")
    ap.add_argument("--demo", action="store_true", help="run built-in examples")
    args = ap.parse_args(argv)

    if args.demo:
        _run_demo()
        return 0

    raw = sys.stdin.read() if args.input == "-" else open(args.input).read()
    payload = json.loads(raw)

    away: set[str] = set()
    if args.away:
        try:
            away = parse_away_list(open(args.away).read())
        except FileNotFoundError:
            pass  # no away-list yet -> nobody away

    pr = PR(
        number=int(payload["number"]),
        author=payload["author"],
        changed_files=payload.get("changed_files", []),
        diff_text=payload.get("diff_text", ""),
        claude_critical=bool(payload.get("claude_critical", False)),
        claude_reason=payload.get("claude_reason", ""),
        already_requested=payload.get("already_requested", []),
        review_load=payload.get("review_load", {}),
    )
    pick = pick_reviewers(pr, away=away)
    print(json.dumps({
        "reviewers": pick.reviewers,
        "tier": pick.tier,
        "reasons": pick.reasons,
    }))
    return 0


# ---------------------------------------------------------------------------
# Demo — run `python3 reviewer_picker.py --demo`
# ---------------------------------------------------------------------------
def _run_demo() -> None:
    sample_away = parse_away_list(
        """
        # unavailable.yml
        - dev: NickEgorenkovBlackIris
          until: 2099-01-01
        """
    )

    cases = [
        PR(  # Tier 1: migration
            number=101, author=SHABAB,
            changed_files=["db/migrate/20260613_add_index.rb", "app/models/incident.rb"],
            diff_text="+    has_many :dispatches\n",
        ),
        PR(  # Tier 1: new env var in diff only
            number=102, author=FARHAD,
            changed_files=["app/services/notifier.rb"],
            diff_text='+  url = ENV["TWILIO_WEBHOOK_URL"]\n',
        ),
        PR(  # Tier 1: deployment file
            number=103, author=NICK,
            changed_files=["docker-compose.prod.yml", "app/cop/dashboard.tsx"],
        ),
        PR(  # Tier 2: Angular module + service
            number=104, author=GILLIAN,
            changed_files=["src/app/dispatch/dispatch.module.ts",
                           "src/app/dispatch/dispatch.service.ts"],
        ),
        PR(  # Tier 3 trivial, throttle fires (104%3 != 0, this one 105%3==0)
            number=105, author=FARHAD,
            changed_files=["app/views/home/index.html.erb"],
        ),
        PR(  # Tier 3 normal, Shabab is author so excluded from his own pool
            number=106, author=SHABAB,
            changed_files=["app/controllers/reports_controller.rb",
                           "app/helpers/reports_helper.rb"],
        ),
        PR(  # Tier 1 but Shabab author + Nick away -> still Yazan
            number=107, author=YAZAN,   # Yazan opens a migration himself
            changed_files=["db/schema.rb"],
        ),
    ]

    for c in cases:
        print(f"PR #{c.number} by {c.author}:")
        print("   ", pick_reviewers(c, away=sample_away).summary())
        print()


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_cli(sys.argv[1:]))
