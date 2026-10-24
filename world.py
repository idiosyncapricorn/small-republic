"""
world.py - shared game state, plus the one function that talks to Groq.

ripple.py and happiness.py both import from here, so the model name, the
regions and the calendar are defined in exactly one place.

Setup:  pip install groq   and set GROQ_API_KEY
"""

from __future__ import annotations

import json
import os
import random
import sys
from dataclasses import dataclass, field

MODEL = "llama-3.3-70b-versatile"   # any Groq model that supports tool use

# The five things people judge their rulers on. Every one is 0-100, higher is better.
DRIVERS = ("affordability", "jobs", "safety", "freedom", "pride")
DRIVER_MEANING = {
    "affordability": "food, fuel and housing people can pay for",
    "jobs": "work, wages and trade",
    "safety": "protection from war, crime and disease",
    "freedom": "rights, worship, a free press and a say in how they are ruled",
    "pride": "identity, sovereignty and standing among the regions",
}

# How much each kind of region cares about each driver (each row adds up to 1).
TRAIT_WEIGHTS = {
    "farming":   {"affordability": .30, "jobs": .20, "safety": .20, "freedom": .10, "pride": .20},
    "trade":     {"affordability": .25, "jobs": .35, "safety": .10, "freedom": .20, "pride": .10},
    "military":  {"affordability": .15, "jobs": .15, "safety": .35, "freedom": .05, "pride": .30},
    "religious": {"affordability": .20, "jobs": .15, "safety": .20, "freedom": .10, "pride": .35},
    "energy":    {"affordability": .20, "jobs": .30, "safety": .15, "freedom": .10, "pride": .25},
}
EVEN_WEIGHTS = {d: 1 / len(DRIVERS) for d in DRIVERS}

ERAS = ("peace", "boom", "famine", "plague", "war scare", "revival")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def to_int(value) -> int:
    """Models sometimes send '5', 5.0 or nonsense. Anything unreadable counts as 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- state

@dataclass
class Region:
    name: str
    trait: str
    drivers: dict[str, int] = field(default_factory=dict)
    baseline: dict[str, int] = field(default_factory=dict)  # where drivers drift back to
    wariness: int = 30               # 0-100, how much its rulers fear the player
    controlled: bool = False         # True once the player holds it
    is_home: bool = False            # the player's starting region
    rivals: list[str] = field(default_factory=list)
    allies: list[str] = field(default_factory=list)
    unrest: int = 0                  # months in a row spent below the unrest line

    def __post_init__(self):
        self.drivers = {d: clamp(to_int(self.drivers.get(d, 50)), 0, 100) for d in DRIVERS}
        self.baseline = {d: self.baseline.get(d, self.drivers[d]) for d in DRIVERS}

    @property
    def weights(self) -> dict[str, float]:
        return TRAIT_WEIGHTS.get(self.trait, EVEN_WEIGHTS)

    @property
    def happiness(self) -> int:
        """
        0-100. Mostly a weighted average of the drivers, but 30% of it is the
        single worst driver: people who cannot afford bread are not consoled
        by a strong army. One acute grievance can sink a region.
        """
        average = sum(self.drivers[d] * self.weights[d] for d in DRIVERS)
        return round(0.7 * average + 0.3 * min(self.drivers.values()))


@dataclass
class World:
    year: int = 1
    month: int = 3                   # 1-12
    era: str = "peace"
    player_title: str = "the Upstart"
    regions: dict[str, Region] = field(default_factory=dict)
    chronicle: list[str] = field(default_factory=list)
    questions: list = field(default_factory=list)       # pending, see ripple.py
    recent_issues: list[str] = field(default_factory=list)
    game_over: str | None = None     # set when the home region rises against you
    rng: random.Random = field(default_factory=random.Random)

    @property
    def season(self) -> str:
        return ("winter", "winter", "spring", "spring", "spring", "summer",
                "summer", "summer", "autumn", "autumn", "autumn", "winter")[self.month - 1]

    @property
    def date(self) -> str:
        return f"{self.season}, month {self.month} of year {self.year}"

    def controlled(self) -> list[Region]:
        return [r for r in self.regions.values() if r.controlled]

    def independent(self) -> list[Region]:
        return [r for r in self.regions.values() if not r.controlled]


def describe_world(world: World) -> str:
    """Turn the game state into the text the model sees."""
    held = len(world.controlled())
    lines = [
        f"Date: {world.date}. Era: {world.era}.",
        f"The player is known as {world.player_title} and holds {held} of "
        f"{len(world.regions)} regions.",
        "Drivers (0-100, higher is better): "
        + "; ".join(f"{d} = {m}" for d, m in DRIVER_MEANING.items()) + ".",
        "Regions:",
    ]
    for r in world.regions.values():
        status = "controlled by the player" if r.controlled else "independent"
        cares = max(DRIVERS, key=lambda d: r.weights[d])
        scores = ", ".join(f"{d} {r.drivers[d]}" for d in DRIVERS)
        lines.append(
            f"- {r.name} ({r.trait}, {status}): happiness {r.happiness}, {scores}, "
            f"wariness of the player {r.wariness}. Cares most about {cares}. "
            f"Rivals: {', '.join(r.rivals) or 'none'}. Allies: {', '.join(r.allies) or 'none'}."
        )
    if world.chronicle:
        lines.append("Recent history: " + " ".join(world.chronicle[-5:]))
    return "\n".join(lines)


def valid_rows(world: World, raw: dict, key: str = "reactions"):
    """Yield (region, row) for each usable row; skip invented or repeated regions."""
    rows = raw.get(key, []) if isinstance(raw, dict) else []
    seen = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        region = world.regions.get(str(row.get("region", "")))
        if region is None or region.name in seen:
            continue
        seen.add(region.name)
        yield region, row


# ---------------------------------------------------------------- Groq

def llm_on(use_llm: bool | None = None) -> bool:
    """True/False forces it. None means: use Groq if a key is set."""
    if use_llm is None:
        return bool(os.environ.get("GROQ_API_KEY"))
    return use_llm


def note(message: str) -> None:
    print(f"[{message}]", file=sys.stderr)


def call_tool(system: str, user: str, name: str, description: str,
              schema: dict, max_tokens: int = 1500) -> dict:
    """
    Ask the model on Groq to fill in `schema` and return the result as a dict.
    Raises on any failure; every caller has a fallback, so the game never stops.
    """
    from groq import Groq

    client = Groq()                  # reads GROQ_API_KEY
    last_error: Exception | None = None
    for _ in range(2):               # models occasionally fumble a tool call; try twice
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                temperature=0.7,
                tools=[{"type": "function",
                        "function": {"name": name, "description": description,
                                     "parameters": schema}}],
                tool_choice={"type": "function", "function": {"name": name}},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            calls = response.choices[0].message.tool_calls
            if not calls:
                raise ValueError("model returned no tool call")
            result = json.loads(calls[0].function.arguments)
            if not isinstance(result, dict):
                raise ValueError("model returned something other than an object")
            return result
        except Exception as err:
            last_error = err
    raise last_error


# ---------------------------------------------------------------- demo world

def demo_world(seed: int | None = None) -> World:
    def d(affordability, jobs, safety, freedom, pride):
        return dict(zip(DRIVERS, (affordability, jobs, safety, freedom, pride)))

    regions = [
        Region("Aldermere", "farming", d(62, 55, 58, 55, 60), controlled=True,
               is_home=True, wariness=0, rivals=["Korvath"], allies=["Sella"]),
        Region("Sella", "trade", d(55, 66, 48, 62, 50),
               rivals=["Korvath"], allies=["Aldermere"]),
        Region("Korvath", "military", d(45, 48, 68, 35, 66), wariness=45,
               rivals=["Aldermere", "Sella"], allies=["Dunmar"]),
        Region("Ystrad", "religious", d(50, 45, 55, 40, 70),
               rivals=["Dunmar"]),
        Region("Dunmar", "energy", d(52, 60, 50, 42, 55), wariness=35,
               rivals=["Ystrad"], allies=["Korvath"]),
    ]
    return World(year=3, month=3, era="peace",
                 regions={r.name: r for r in regions},
                 rng=random.Random(seed))
