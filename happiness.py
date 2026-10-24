"""
happiness.py - how the PEOPLE of each region feel, and what they do about it.

A region's happiness is built from five drivers (see world.py): affordability,
jobs, safety, freedom and pride. Each kind of region weighs them differently.

Three things move the drivers:
  1. judge_impact()  - Groq judges how a decision or answer lands with the
                       people of every region.
  2. monthly_tick()  - code only: things drift back to normal, the era and the
                       season push on them, and unhappy regions act.
  3. mood_report()   - Groq writes what people are saying. Flavor only.

The model supplies judgment. The code owns the numbers: every change is
capped, every driver stays 0-100, and a failed call never stops the game.
"""

from __future__ import annotations

from world import (DRIVERS, DRIVER_MEANING, Region, World, call_tool, clamp,
                   describe_world, llm_on, note, to_int, valid_rows)

MAX_DRIVER_DELTA = 10     # biggest change one event can cause to one driver
UNREST_LINE = 30          # happiness below this counts as a month of unrest
BREAKAWAY_MONTHS = 3      # months of unrest before a held region breaks away
SOFT_POWER_GAP = 15       # happiness gap at which free regions notice your example
LASTING_SHARE = 0.5       # this much of a decision's effect is permanent; the rest fades
ERA_REACH = 20            # an era can pull a driver this far from normal, no further

MOODS = ((75, "thriving"), (55, "content"), (40, "restless"),
         (UNREST_LINE, "angry"), (0, "in revolt"))

# What each era does to every region, every month.
ERA_PRESSURE = {
    "peace":     {},
    "boom":      {"jobs": +2, "affordability": +2},
    "famine":    {"affordability": -3},
    "plague":    {"safety": -3, "jobs": -2},
    "war scare": {"safety": -3},
    "revival":   {"pride": +2, "freedom": -2},
}
# Extra era effects for particular kinds of region.
ERA_BY_TRAIT = {
    ("famine", "farming"):    {"jobs": -2},          # failed harvests are lost wages
    ("famine", "trade"):      {"jobs": -1},
    ("plague", "trade"):      {"jobs": -2},          # ports and roads close
    ("war scare", "military"): {"pride": +2, "jobs": +2},   # the army is hiring
    ("war scare", "trade"):   {"jobs": -2},
    ("revival", "religious"): {"pride": +2},
    ("boom", "energy"):       {"jobs": +1},
}
# The yearly rhythm of each kind of region.
SEASON_BY_TRAIT = {
    ("farming", "autumn"):  {"affordability": +2, "jobs": +2},   # harvest
    ("farming", "winter"):  {"affordability": -2},               # stores run down
    ("trade", "summer"):    {"jobs": +2},                        # sea lanes open
    ("trade", "winter"):    {"jobs": -2},
    ("military", "spring"): {"safety": -2},                      # soldiers home to plant
    ("energy", "winter"):   {"jobs": +2},                        # fuel sells itself
    ("energy", "summer"):   {"jobs": -2},
    ("religious", "spring"): {"pride": +2},                      # festival season
}


# ---------------------------------------------------------------- reading it

def mood(region: Region) -> str:
    score = region.happiness
    return next(label for floor, label in MOODS if score >= floor)


def top_grievance(region: Region) -> str:
    """The driver hurting this region most: how far it falls short of 'good
    enough' (75), scaled by how much the region cares. If nothing falls short,
    it is simply the lowest driver."""
    short = {d: region.weights[d] * max(0, 75 - region.drivers[d]) for d in DRIVERS}
    if not any(short.values()):
        return min(DRIVERS, key=lambda d: region.drivers[d])
    return max(DRIVERS, key=lambda d: short[d])


def apply_changes(region: Region, deltas: dict, limit: int = MAX_DRIVER_DELTA) -> dict:
    """Clamp and apply driver changes. Returns what really changed."""
    applied = {}
    for driver in DRIVERS:
        delta = clamp(to_int(deltas.get(driver, 0)), -limit, limit)
        before = region.drivers[driver]
        region.drivers[driver] = clamp(before + delta, 0, 100)
        if region.drivers[driver] != before:
            applied[driver] = region.drivers[driver] - before
    return applied


# ---------------------------------------------------------------- 1. Groq judges an event

PEOPLE_SYSTEM = """You simulate public opinion for a political strategy game.
You are told what just happened and you judge how the ORDINARY PEOPLE of every
region feel about it. Rulers and councils are judged elsewhere; you speak only
for farmers, traders, soldiers' families, priests and workers.

Reason the way real populations do:
- People feel prices and jobs before they feel principles.
- A gain for one region is often a loss for its rival or its trading partner.
- People resent their rulers bowing to foreigners, and enjoy seeing a rival humbled.
- Refugees, embargoes, tariffs, garrisons and closed borders all have winners
  and losers inside the same region; report the net effect.
- Fear of war or disease can make people accept less freedom, for a while.
- Regions with no stake in the matter barely notice it.

Rules:
- Report every region. Use 0 for a driver the event does not touch.
- Each number is an integer from -10 to 10. Most events move one or two
  drivers by a few points. Save large numbers for dramatic acts.
- Weigh the season, the era, what each region cares most about, and its
  rivals and allies.
- reason is one short sentence, as someone in that region's street would say it."""

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "reactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    **{d: {"type": "integer", "description": f"change to {DRIVER_MEANING[d]}"}
                       for d in DRIVERS},
                    "reason": {"type": "string"},
                },
                "required": ["region", *DRIVERS, "reason"],
            },
        },
    },
    "required": ["reactions"],
}


def _offline_impact(world: World, event: str) -> dict:
    """Stand-in for typed-in decisions when there is no key. Rough guesswork only."""
    text = event.lower()
    rows = []
    for r in world.regions.values():
        driver = world.rng.choice(DRIVERS)
        if r.name.lower() in text:
            size = world.rng.randint(2, 5)
        elif any(rival.lower() in text for rival in r.rivals):
            size = -world.rng.randint(1, 4)
        else:
            size = world.rng.randint(-1, 1)
        rows.append({"region": r.name, driver: size, "reason": "(offline guess)"})
    return {"reactions": rows}


def judge_impact(world: World, event: str, use_llm: bool | None = None,
                 fallback: dict | None = None) -> list[dict]:
    """
    Work out how the people of every region react to `event`, and apply it.

    fallback: {region name: {driver: change}} - known effects to use if Groq
    is off or the call fails (the questions in ripple.py supply these).

    Returns one entry per region that changed:
      {"region", "changes": {driver: n}, "before", "after", "mood", "reason"}
    """
    raw = None
    if llm_on(use_llm):
        try:
            raw = call_tool(PEOPLE_SYSTEM,
                            f"{describe_world(world)}\n\nWhat just happened: {event}",
                            "report_public_mood",
                            "Report how the people of each region react.",
                            PEOPLE_SCHEMA)
        except Exception as err:
            note(f"happiness: Groq call failed, using fallback ({err})")

    if raw is None and fallback is not None:
        jitter = lambda n: n + world.rng.choice((-1, 0, 0, 1)) if n else 0
        raw = {"reactions": [
            {"region": name, "reason": "",
             **{d: jitter(to_int(v)) for d, v in deltas.items()}}
            for name, deltas in fallback.items() if name]}
    if raw is None and not llm_on(use_llm):
        raw = _offline_impact(world, event)
    if raw is None:
        return []                    # Groq failed and nothing is known: no change

    results = []
    for region, row in valid_rows(world, raw):
        before = region.happiness
        changes = apply_changes(region, row)
        for d, n in changes.items():     # part of it becomes the new normal
            region.baseline[d] = clamp(region.baseline[d] + round(n * LASTING_SHARE), 10, 90)
        if changes:
            results.append({"region": region.name, "changes": changes,
                            "before": before, "after": region.happiness,
                            "mood": mood(region), "reason": str(row.get("reason", ""))})
    return results


# ---------------------------------------------------------------- 2. the monthly tick

def monthly_tick(world: World) -> list[str]:
    """
    Advance every region's mood by one month. No model call.
    Returns plain-English events for the UI. May set world.game_over.
    """
    events: list[str] = []
    regions = list(world.regions.values())

    for r in regions:
        # Things drift back toward normal, so the shock of a decision fades.
        for d in DRIVERS:
            gap = r.baseline[d] - r.drivers[d]
            r.drivers[d] += (gap > 0) - (gap < 0)
        # The era pushes on top of that, but only so far: a famine makes a
        # region fragile, and it is your decisions that tip it over.
        for table, key in ((ERA_PRESSURE, world.era), (ERA_BY_TRAIT, (world.era, r.trait))):
            for d, push in table.get(key, {}).items():
                low, high = r.baseline[d] - ERA_REACH, r.baseline[d] + ERA_REACH
                if low <= r.drivers[d] <= high:
                    r.drivers[d] = clamp(clamp(r.drivers[d] + push, low, high), 0, 100)
        # So does the season.
        apply_changes(r, SEASON_BY_TRAIT.get((r.trait, world.season), {}))

    # People in a miserable region leave for its allies, straining them.
    for r in regions:
        if r.happiness < UNREST_LINE:
            hosts = [world.regions[n] for n in r.allies if n in world.regions]
            for host in hosts:
                apply_changes(host, {"affordability": -1})
            if hosts and r.unrest == 0:          # announce it once, not monthly
                events.append(f"People are leaving {r.name} for "
                              f"{' and '.join(h.name for h in hosts)}, pushing up prices there.")

    # Soft power: free regions compare their lives with life under you.
    held = world.controlled()
    if held:
        yours = sum(r.happiness for r in held) / len(held)
        for r in world.independent():
            gap = yours - r.happiness
            if gap >= SOFT_POWER_GAP:
                r.wariness = clamp(r.wariness - 1, 0, 100)
            elif gap <= -SOFT_POWER_GAP:
                r.wariness = clamp(r.wariness + 1, 0, 100)

    # Unrest builds month by month, and then something breaks.
    for r in regions:
        if r.happiness >= UNREST_LINE:
            if r.unrest:
                events.append(f"The unrest in {r.name} has died down.")
            r.unrest = 0
            continue
        r.unrest += 1
        if r.unrest == 1:
            events.append(f"Unrest in {r.name}: crowds are in the streets over "
                          f"{top_grievance(r)}.")
        if r.unrest < BREAKAWAY_MONTHS:
            continue
        if r.is_home and r.controlled:
            world.game_over = (f"{r.name}, your own home, has risen against you "
                               f"over {top_grievance(r)}.")
            events.append(world.game_over)
        elif r.controlled:
            r.controlled = False
            r.wariness = max(r.wariness, 70)
            r.unrest = 0
            apply_changes(r, {"pride": +8})          # they are their own masters again
            events.append(f"{r.name} has thrown off your rule after "
                          f"{BREAKAWAY_MONTHS} months of unrest.")
        elif r.unrest == BREAKAWAY_MONTHS:
            # A free region whose own rulers are failing looks for someone else.
            r.wariness = clamp(r.wariness - 10, 0, 100)
            events.append(f"The rulers of {r.name} are losing their grip. "
                          f"Its people are open to a new master.")
    return events


# ---------------------------------------------------------------- 3. Groq writes the mood

REPORT_SYSTEM = """You write the street-level mood for a political strategy game.
For each region, write ONE sentence of what ordinary people there are saying
this season, in their own voice. Base it on that region's numbers, its worst
driver, the era and recent history. Concrete and specific: prices, sons in the
army, the temple, the docks. No numbers, no game terms."""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "reports": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"region": {"type": "string"},
                               "line": {"type": "string"}},
                "required": ["region", "line"],
            },
        },
    },
    "required": ["reports"],
}


def mood_report(world: World, use_llm: bool | None = None) -> dict[str, str]:
    """One line per region on what people are saying. Changes nothing."""
    report = {r.name: f"{r.name} is {mood(r)}; the talk is of "
                      f"{DRIVER_MEANING[top_grievance(r)]}."
              for r in world.regions.values()}
    if llm_on(use_llm):
        try:
            raw = call_tool(REPORT_SYSTEM, describe_world(world), "report_street_mood",
                            "Report what people in each region are saying.",
                            REPORT_SCHEMA, max_tokens=800)
            for region, row in valid_rows(world, raw, "reports"):
                line = str(row.get("line", "")).strip()
                if line:
                    report[region.name] = line[:300]
        except Exception as err:
            note(f"happiness: mood report fell back to plain text ({err})")
    return report
