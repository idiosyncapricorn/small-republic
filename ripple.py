"""
ripple.py - decisions, the questions that force them, and how they spread.

Three jobs:
  1. resolve_decision()  - any decision ripples across the map. Groq judges how
                           the other GOVERNMENTS react (wariness); happiness.py
                           judges how the PEOPLE react.
  2. questions           - real-world dilemmas (refugees, embargoes, tariffs,
                           water, energy, borders...) appear at random and must
                           be answered before their deadline.
  3. advance_month()     - moves the clock: mood tick, deadlines, new questions.

The code picks which dilemma appears and who is involved. Groq writes it up
for the current world and judges the answer. Every preset option also carries
known effects, so the game plays the same way with no key or a failed call.

Setup:   pip install groq   and set GROQ_API_KEY
Watch:   python ripple.py           (plays a year by itself)
Play:    python ripple.py --play    (you answer: 1, 2, 3 or your own words)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import happiness
from world import (ERAS, World, call_tool, clamp, demo_world, describe_world,
                   llm_on, note, to_int, valid_rows)

MAX_WARINESS_DELTA = 15   # biggest change one decision can cause to wariness
QUESTION_CHANCE = 0.35    # chance of a new question each month...
CHANCE_PER_REGION = 0.05  # ...plus this for every extra region you hold
MAX_PENDING = 3           # questions that can wait on your desk at once
ERA_CHANGE_CHANCE = 0.45  # chance the era changes at each new year
ERA_ODDS = {"peace": 3, "boom": 2, "famine": 2, "plague": 1, "war scare": 2, "revival": 1}


# ---------------------------------------------------------------- 1. the ripple

GOV_SYSTEM = """You are the world simulator for a political strategy game.
The player rules one or more regions and is trying to bring the rest under
their control. When the player makes a decision, you judge how the
GOVERNMENTS of the independent regions react. Their people are judged elsewhere.

Wariness is how much a region's rulers fear and distrust the player (0-100).
Reason the way real governments do:
- Alliances and rivalries: helping a region alarms its rivals and reassures its allies.
- Precedent: rulers ask what the act says about how the player would treat them.
- Balance of power: the more regions the player holds, the more any forceful
  act frightens the rest, and the more they lean on each other.
- Credibility: kept commitments and restraint lower wariness; broken deals,
  secrecy and coercion raise it everywhere, not only where they happened.
- Interest: a region with nothing at stake barely reacts.

Rules:
- Report every independent region. Use 0 when it would not care.
- wariness_delta is an integer from -15 to 15. Most decisions cause small
  ripples. Save large numbers for dramatic acts.
- Weigh the season and the era.
- reason is one short sentence in the voice of a court chronicler.
- chronicle_line is one sentence recording the decision for the history of the reign."""

GOV_SCHEMA = {
    "type": "object",
    "properties": {
        "reactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "wariness_delta": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["region", "wariness_delta", "reason"],
            },
        },
        "chronicle_line": {"type": "string"},
    },
    "required": ["reactions", "chronicle_line"],
}


def judge_governments(world: World, decision: str, use_llm: bool | None = None,
                      fallback: dict | None = None) -> tuple[list[dict], str]:
    """
    Work out how each independent region's rulers react, and apply it.
    fallback: {region name: wariness change}, used if Groq is off or fails.
    Returns (changes, chronicle line). The line is "" if the model gave none.
    """
    raw = None
    if llm_on(use_llm):
        try:
            raw = call_tool(GOV_SYSTEM,
                            f"{describe_world(world)}\n\nThe player's decision: {decision}",
                            "report_ripple",
                            "Report how each independent region's rulers react.",
                            GOV_SCHEMA)
        except Exception as err:
            note(f"ripple: Groq call failed, using fallback ({err})")

    if raw is None and fallback is not None:
        raw = {"reactions": [{"region": name, "wariness_delta": delta, "reason": ""}
                             for name, delta in fallback.items() if name]}
    if raw is None and not llm_on(use_llm):      # typed-in decision, no key: rough guess
        text = decision.lower()
        raw = {"reactions": [
            {"region": r.name, "reason": "(offline guess)",
             "wariness_delta": (-world.rng.randint(1, 4) if r.name.lower() in text
                                else world.rng.randint(2, 5)
                                if any(n.lower() in text for n in r.rivals)
                                else world.rng.randint(-1, 1))}
            for r in world.independent()]}
    if raw is None:
        return [], ""

    changes = []
    for region, row in valid_rows(world, raw):
        if region.controlled:
            continue                 # your own regions answer through happiness
        delta = clamp(to_int(row.get("wariness_delta", 0)),
                      -MAX_WARINESS_DELTA, MAX_WARINESS_DELTA)
        before = region.wariness
        region.wariness = clamp(before + delta, 0, 100)
        if region.wariness != before:
            changes.append({"region": region.name,
                            "wariness_delta": region.wariness - before,
                            "wariness": region.wariness,
                            "reason": str(row.get("reason", ""))})
    return changes, str(raw.get("chronicle_line", "")).strip()[:300]


def resolve_decision(world: World, decision: str, use_llm: bool | None = None,
                     fallback: dict | None = None, headline: str = "") -> dict:
    """
    Apply any player decision to the whole world. Call this for every decision,
    not only answers to questions.

    fallback: {"people": {...}, "wary": {...}} known effects (see judge_* above).
    headline: short text for the chronicle if the model does not write one.

    Returns {"people": [...], "governments": [...], "chronicle_line": str}.
    """
    fallback = fallback or {}
    people = happiness.judge_impact(world, decision, use_llm, fallback.get("people"))
    governments, line = judge_governments(world, decision, use_llm, fallback.get("wary"))
    line = line or f"Year {world.year}, {world.season}: {headline or decision[:140]}"
    world.chronicle.append(line)
    return {"people": people, "governments": governments, "chronicle_line": line}


# ---------------------------------------------------------------- 2. the questions

def opt(label, text, home=None, a=None, b=None, wary=None) -> dict:
    """One answer. home/a/b = driver changes for those peoples; wary = for rulers."""
    return {"label": label, "text": text,
            "people": {"home": home or {}, "a": a or {}, "b": b or {}},
            "wary": wary or {}}


# In the text: {home} is one of your regions, {a} is an independent region, and
# {b} is a second independent region (a rival of {a} where one exists).
# "eras" makes an issue more likely in that era (2 = twice as likely).
ISSUES = [
    dict(key="refugees", title="Refugees at the border", eras={"famine": 3, "plague": 2, "war scare": 2},
         situation="Thousands are fleeing hardship in {a} and gathering at {home}'s border, with more on the road behind them.",
         options=[
             opt("Open the border", "Let them all in and house them.",
                 home={"affordability": -4, "jobs": -2, "freedom": +2}, a={"safety": +2},
                 wary={"a": +1, "others": -2}),
             opt("Admit a fixed number", "Take a set quota and turn the rest back.",
                 home={"affordability": -2}, wary={"others": -1}),
             opt("Close the border", "Seal the crossings and post guards.",
                 home={"safety": +2, "pride": +2, "freedom": -2}, a={"affordability": -2, "safety": -2},
                 wary={"a": +3, "others": +2}),
         ]),
    dict(key="tariffs", title="Cheap imports", eras={"boom": 2},
         situation="Cheaper goods from {a} are undercutting the workshops of {home}, and the guilds want protection.",
         options=[
             opt("Raise tariffs", "Tax goods from {a} at the border.",
                 home={"jobs": +4, "affordability": -4}, a={"jobs": -4}, wary={"a": +4, "others": +1}),
             opt("Subsidise our own", "Leave trade alone and pay {home}'s producers from the treasury.",
                 home={"jobs": +2, "affordability": -1}, a={"jobs": -1}, wary={"a": +1}),
             opt("Keep trade open", "Do nothing; let buyers choose.",
                 home={"jobs": -3, "affordability": +3}, a={"jobs": +3}, wary={"a": -3}),
         ]),
    dict(key="embargo", title="A call for an embargo", needs_b=True, eras={"war scare": 2},
         situation="{a} asks {home} to join its embargo on {b}: no trade, no credit, no ships in port.",
         options=[
             opt("Join the embargo", "Cut all trade with {b}.",
                 home={"jobs": -3, "affordability": -2, "pride": +2}, a={"pride": +2},
                 b={"jobs": -4, "affordability": -3}, wary={"a": -4, "b": +6}),
             opt("Stay neutral", "Decline politely and trade with both.",
                 wary={"a": +2, "b": -1}),
             opt("Trade with {b} in secret", "Agree in public, keep the goods moving in private.",
                 home={"jobs": +3}, b={"jobs": +2}, wary={"a": +5, "b": -3, "others": +1}),
         ]),
    dict(key="water", title="The dam on the shared river",
         situation="{home} can dam the river it shares with {a}, securing its own water and mills. Downstream, {a}'s fields depend on that flow.",
         options=[
             opt("Build the dam", "Build it and control the flow.",
                 home={"affordability": +4, "jobs": +3, "pride": +2}, a={"affordability": -5, "safety": -2},
                 wary={"a": +6, "others": +2}),
             opt("Sign a water treaty", "Build a smaller dam and guarantee {a} a fixed share.",
                 home={"affordability": +2}, a={"affordability": -1, "pride": +1}, wary={"a": -2}),
             opt("Abandon the plan", "Leave the river as it is.",
                 home={"pride": -3, "jobs": -1}, a={"affordability": +1}, wary={"a": -3}),
         ]),
    dict(key="energy", title="A cheap fuel offer", needs_b=True, eras={"boom": 2},
         situation="{a} offers {home} fuel far below the market price on a ten-year contract. {b} warns that it is a leash, to be pulled when {a} wants something.",
         options=[
             opt("Sign the contract", "Take the cheap fuel and the dependence that comes with it.",
                 home={"affordability": +5, "pride": -2, "safety": -2}, a={"jobs": +3},
                 wary={"a": -4, "b": +3}),
             opt("Buy some, keep other suppliers", "Take a small share and stay diversified.",
                 home={"affordability": +2}, a={"jobs": +1}, wary={"a": -1, "b": +1}),
             opt("Refuse and build our own", "Pay more now for independence later.",
                 home={"affordability": -3, "jobs": +2, "pride": +3}, a={"jobs": -2},
                 wary={"a": +3, "b": -2}),
         ]),
    dict(key="border", title="Shots at the border", eras={"war scare": 3},
         situation="Patrols from {home} and {a} have clashed in the disputed hills. Men are dead on both sides and each blames the other.",
         options=[
             opt("Send troops", "Reinforce the line and hold the hills.",
                 home={"pride": +4, "safety": -3, "affordability": -1}, a={"safety": -4, "pride": +2},
                 wary={"a": +7, "others": +3}),
             opt("Propose a joint inquiry", "Offer to investigate together and pay the families.",
                 home={"pride": -1, "safety": +2}, a={"safety": +2}, wary={"a": -2}),
             opt("Pull back and apologise", "Withdraw from the hills.",
                 home={"pride": -5, "safety": +1}, a={"pride": +3}, wary={"a": -4, "others": -1}),
         ]),
    dict(key="garrison", title="A request for a garrison", needs_b=True, eras={"war scare": 3},
         situation="{a} asks to station its soldiers on {home}'s soil, as protection for you both against {b}.",
         options=[
             opt("Accept the garrison", "Foreign soldiers, foreign flag, inside {home}.",
                 home={"safety": +4, "pride": -4, "freedom": -1, "jobs": +1}, wary={"a": -5, "b": +6}),
             opt("Port and supply rights only", "No garrison, but their ships and wagons may use {home}.",
                 home={"safety": +2, "pride": -1}, wary={"a": -2, "b": +3}),
             opt("Refuse", "{home} defends itself.",
                 home={"pride": +3, "safety": -2}, wary={"a": +3, "b": -2}),
         ]),
    dict(key="arms", title="An arms order", needs_b=True, eras={"war scare": 3},
         situation="{a} wants to buy weapons from {home}'s forges, and will pay well. {b}, its rival, is watching what you do.",
         options=[
             opt("Sell to {a}", "Take the order.",
                 home={"jobs": +4}, a={"safety": +3, "pride": +2}, b={"safety": -4},
                 wary={"a": -4, "b": +6}),
             opt("Sell to both sides", "Offer {b} the same terms.",
                 home={"jobs": +5, "pride": -2}, a={"safety": -1}, b={"safety": -1},
                 wary={"a": +2, "b": +2, "others": +2}),
             opt("Refuse the order", "{home} arms no one.",
                 home={"jobs": -2}, a={"pride": -1}, wary={"a": +3, "b": -2}),
         ]),
    dict(key="grain", title="Grain for sale, or for home", eras={"famine": 4},
         situation="Stores are thin. Merchants in {home} are under contract to ship grain to {a}, where it is needed just as badly.",
         options=[
             opt("Ban exports", "Keep every sack at home.",
                 home={"affordability": +4, "jobs": -2}, a={"affordability": -5},
                 wary={"a": +5, "others": +2}),
             opt("Honour the contracts", "Ship it all, as agreed.",
                 home={"affordability": -4, "pride": -1, "jobs": +2}, a={"affordability": +3},
                 wary={"a": -4, "others": -1}),
             opt("Ship half", "Split the shortage between you.",
                 home={"affordability": -1}, a={"affordability": -2}, wary={"a": +1}),
         ]),
    dict(key="debt", title="A debtor asks for mercy", eras={"boom": 2, "famine": 2},
         situation="{a} cannot repay what it owes {home}, and asks for the debt to be forgiven.",
         options=[
             opt("Forgive the debt", "Write it off.",
                 home={"affordability": -2, "pride": +1}, a={"affordability": +4, "jobs": +2},
                 wary={"a": -6, "others": -2}),
             opt("Take assets instead", "Trade the debt for {a}'s port dues and mining rights.",
                 home={"jobs": +3}, a={"pride": -5, "affordability": +1}, wary={"a": +3, "others": +3}),
             opt("Demand payment", "Every coin, on time.",
                 home={"affordability": +2}, a={"affordability": -4, "jobs": -3},
                 wary={"a": +5, "others": +1}),
         ]),
    dict(key="vote", title="A vote you could tip",
         situation="{a}'s council votes soon. Your agents say that coin in the right hands would seat friendlier members.",
         options=[
             opt("Fund them in secret", "Pay for friendly candidates and deny everything.",
                 a={"freedom": -3, "pride": -2}, wary={"a": -4, "others": +3}),
             opt("Back them openly", "Say in public whom {home} would like to see win.",
                 a={"pride": -2}, wary={"a": +2, "others": +1}),
             opt("Stay out of it", "Their vote, their business.",
                 home={"pride": +1}, wary={"a": -1, "others": -1}),
         ]),
    dict(key="plague_road", title="Sickness on the trade road", eras={"plague": 5},
         situation="Fever has broken out in {a}. Its caravans and ships are due in {home} within the week.",
         options=[
             opt("Close the border", "Turn every caravan and ship back.",
                 home={"safety": +4, "jobs": -4, "affordability": -2}, a={"jobs": -4}, wary={"a": +3}),
             opt("Inspect and quarantine", "Let them in after a wait outside the walls.",
                 home={"safety": +1, "jobs": -1}, a={"jobs": -1}, wary={"a": -1}),
             opt("Keep trade moving", "Business as usual.",
                 home={"safety": -5, "jobs": +2}, a={"jobs": +2}, wary={"a": -2}),
         ]),
    dict(key="spy", title="A spy in chains",
         situation="{a} has caught one of your agents and is parading him through its capital.",
         options=[
             opt("Deny everything", "He is no man of ours.",
                 home={"pride": -1}, a={"pride": +2}, wary={"a": +3, "others": +1}),
             opt("Admit it and bargain", "Own it, and trade for his release.",
                 home={"pride": -3}, a={"pride": +3}, wary={"a": -2}),
             opt("Expel their envoys", "Answer the insult with one of your own.",
                 home={"pride": +3, "jobs": -1}, a={"jobs": -1}, wary={"a": +6, "others": +2}),
         ]),
    dict(key="kin", title="Kin across the border", eras={"revival": 3, "war scare": 2},
         situation="People who share {home}'s faith and tongue say they are being persecuted in {a}, and are asking you for help.",
         options=[
             opt("Threaten {a}", "Warn that {home} will protect its own, by force if needed.",
                 home={"pride": +4, "safety": -2}, a={"safety": -3, "pride": +1},
                 wary={"a": +6, "others": +3}),
             opt("Offer them a home", "Invite them to resettle in {home}.",
                 home={"pride": +2, "affordability": -2}, wary={"a": +1, "others": -1}),
             opt("Stay silent", "It is {a}'s affair.",
                 home={"pride": -4}, wary={"a": -2}),
         ]),
    # Internal issues need no other region, so they still appear late in the game.
    dict(key="separatists", title="A province wants out", internal=True,
         situation="A border province of {home} is demanding self-rule, and marchers fill its streets.",
         options=[
             opt("Grant autonomy", "Its own council and taxes, under your flag.",
                 home={"freedom": +4, "pride": -3, "safety": +1}, wary={"others": -2}),
             opt("Hold a referendum", "Let the province vote, and abide by the result.",
                 home={"freedom": +5, "pride": -2, "safety": -1}, wary={"others": -1}),
             opt("Send in troops", "Clear the streets and arrest the leaders.",
                 home={"freedom": -6, "safety": -2, "pride": +2}, wary={"others": +4}),
         ]),
    dict(key="press", title="Pamphlets against you", internal=True, eras={"revival": 2},
         situation="Printers in {home} are circulating attacks on your rule. Some of what they say is true.",
         options=[
             opt("Shut the presses", "Seize the type and jail the printers.",
                 home={"freedom": -6, "safety": +1}, wary={"others": +3}),
             opt("Answer in public", "Reply to the charges and correct what you can.",
                 home={"freedom": +3, "pride": +1}, wary={"others": -1}),
             opt("Ignore them", "Let it blow over.",
                 home={"freedom": +1, "pride": -2}),
         ]),
    dict(key="conscription", title="The army needs men", internal=True, eras={"war scare": 3},
         situation="Your commanders say the army is too small to hold what you now rule.",
         options=[
             opt("Conscript", "Every household gives a son.",
                 home={"safety": +4, "freedom": -4, "jobs": -2}, wary={"others": +3}),
             opt("Pay for volunteers", "Raise taxes and raise wages.",
                 home={"safety": +2, "affordability": -3}, wary={"others": +1}),
             opt("Leave the army as it is", "Trust in what you have.",
                 home={"safety": -3}, wary={"others": -1}),
         ]),
]


@dataclass
class Question:
    key: str
    title: str
    situation: str
    options: list[dict]              # as built by opt(), with the names filled in
    home: str
    a: str | None
    b: str | None
    due_in: int                      # months left to answer


def _cast(world: World, issue: dict):
    """Choose who is involved. Returns (home, a, b) names, or None if it cannot happen."""
    held, free = world.controlled(), world.independent()
    if not held:
        return None
    home = world.rng.choice(held)
    if issue.get("internal"):
        return home.name, None, None
    if not free:
        return None
    a = world.rng.choice(free)
    if not issue.get("needs_b"):
        return home.name, a.name, None
    others = [r for r in free if r is not a]
    rivals = [r for r in others if r.name in a.rivals]
    if not others:
        return None
    return home.name, a.name, world.rng.choice(rivals or others).name


SITUATION_SYSTEM = """You write briefing notes for the ruler in a political strategy game.
You are given a situation and the choices the ruler will be offered. Rewrite
the situation in two or three sentences so it belongs to this world right now:
the season, the era, recent history and the regions' current state. Keep the
same regions and the same dilemma, so the choices still fit. Concrete and
plain. Do not list or hint at the choices, and do not recommend one."""

SITUATION_SCHEMA = {
    "type": "object",
    "properties": {"situation": {"type": "string"}},
    "required": ["situation"],
}


def maybe_raise_question(world: World, use_llm: bool | None = None,
                         force: bool = False) -> Question | None:
    """Roll for a new question this month. Returns it, or None."""
    if len(world.questions) >= MAX_PENDING:
        return None
    extra = max(0, len(world.controlled()) - 1)
    chance = min(0.8, QUESTION_CHANCE + CHANCE_PER_REGION * extra)
    if not force and world.rng.random() >= chance:
        return None

    pending = {q.key for q in world.questions}
    choices = []
    for issue in ISSUES:
        if issue["key"] in pending or issue["key"] in world.recent_issues:
            continue
        cast = _cast(world, issue)
        if cast:
            choices.append((issue, cast, issue.get("eras", {}).get(world.era, 1)))
    if not choices:
        return None
    issue, (home, a, b), _ = world.rng.choices(choices, weights=[c[2] for c in choices])[0]

    names = {"home": home, "a": a or "", "b": b or ""}
    question = Question(
        key=issue["key"], title=issue["title"],
        situation=issue["situation"].format(**names),
        options=[{**o, "label": o["label"].format(**names), "text": o["text"].format(**names)}
                 for o in issue["options"]],
        home=home, a=a, b=b, due_in=world.rng.randint(2, 3))

    if llm_on(use_llm):
        try:
            choices_text = "; ".join(o["label"] for o in question.options)
            raw = call_tool(SITUATION_SYSTEM,
                            f"{describe_world(world)}\n\nSituation: {question.situation}\n"
                            f"Choices the ruler will be offered: {choices_text}",
                            "write_situation", "Write the briefing note.",
                            SITUATION_SCHEMA, max_tokens=400)
            text = str(raw.get("situation", "")).strip()
            if text:
                question.situation = text[:700]
        except Exception as err:
            note(f"ripple: kept the stock wording for this question ({err})")

    world.questions.append(question)
    world.recent_issues = (world.recent_issues + [question.key])[-6:]
    return question


def _known_effects(world: World, q: Question, option: dict) -> dict:
    """Turn an option's home/a/b effects into real region names."""
    role = {"home": q.home, "a": q.a, "b": q.b}
    people = {role[k]: v for k, v in option["people"].items() if role[k] and v}
    wary = {role[k]: v for k, v in option["wary"].items() if k != "others" and role[k]}
    for r in world.independent():
        if r.name not in (q.a, q.b) and option["wary"].get("others"):
            wary[r.name] = option["wary"]["others"]
    return {"people": people, "wary": wary}


def answer_question(world: World, question: Question, answer,
                    use_llm: bool | None = None) -> dict:
    """
    Answer a pending question and ripple the result across the world.
    answer: 1, 2 or 3 to pick an option, or any text for the player's own idea.
    Typed answers are judged by Groq alone, so with no key they are a rough guess.
    """
    choice = str(answer).strip()
    if choice.isdigit() and 1 <= int(choice) <= len(question.options):
        option = question.options[int(choice) - 1]
        said, label = f"{option['label']}. {option['text']}", option["label"]
        fallback = _known_effects(world, question, option)
    else:
        said, label, fallback = choice, choice[:80], None
    if question in world.questions:
        world.questions.remove(question)
    decision = (f"{question.title}. {question.situation} "
                f"The ruler of {question.home} decided: {said}")
    return resolve_decision(world, decision, use_llm, fallback,
                            headline=f"{question.title}: {label}.")


def _expire(world: World, question: Question, use_llm: bool | None) -> dict:
    """An unanswered question settles itself, badly, and you look weak at home."""
    world.questions.remove(question)
    happiness.apply_changes(world.regions[question.home], {"pride": -4, "safety": -1})
    decision = (f"{question.title}. {question.situation} The ruler of {question.home} "
                f"gave no answer, and the matter was settled without them on poor terms.")
    fallback = {"people": {}, "wary": {question.a: +2} if question.a else {}}
    return resolve_decision(world, decision, use_llm, fallback,
                            headline=f"{question.title}: left unanswered.")


# ---------------------------------------------------------------- 3. the clock

def advance_month(world: World, use_llm: bool | None = None) -> dict:
    """
    Move the game forward one month.
    Returns {"events": [str], "expired": [(Question, result)], "new_question": Question|None}.
    """
    events = []
    world.month += 1
    if world.month > 12:
        world.month, world.year = 1, world.year + 1
        if world.rng.random() < ERA_CHANGE_CHANCE:
            eras = [e for e in ERAS if e != world.era]
            world.era = world.rng.choices(eras, weights=[ERA_ODDS[e] for e in eras])[0]
            events.append(f"A new era begins: {world.era}.")

    events += happiness.monthly_tick(world)

    expired = []
    for q in list(world.questions):
        q.due_in -= 1
        if q.due_in <= 0:
            expired.append((q, _expire(world, q, use_llm)))

    new = None if world.game_over else maybe_raise_question(world, use_llm)
    return {"events": events, "expired": expired, "new_question": new}


# ---------------------------------------------------------------- demo

def show_result(result: dict) -> None:
    for p in result["people"]:
        moved = ", ".join(f"{d} {n:+}" for d, n in p["changes"].items())
        print(f"    people of {p['region']:10} {p['before']} -> {p['after']} ({p['mood']}): "
              f"{moved}  {p['reason']}")
    for g in result["governments"]:
        print(f"    rulers of {g['region']:10} wariness {g['wariness_delta']:+} "
              f"(now {g['wariness']})  {g['reason']}")
    print(f"    Chronicle: {result['chronicle_line']}")


def show_table(world: World) -> None:
    for r in world.regions.values():
        who = "yours" if r.controlled else f"wary {r.wariness}"
        print(f"    {r.name:10} {r.happiness:3} {happiness.mood(r):10} ({who})")


if __name__ == "__main__":
    play = "--play" in sys.argv
    world = demo_world()
    print("Using:", "Groq" if llm_on() else "no GROQ_API_KEY, so built-in effects only")
    show_table(world)

    for _ in range(12):
        report = advance_month(world)
        print(f"\n== {world.date}, era: {world.era} ==")
        for event in report["events"]:
            print(f"  * {event}")
        for q, result in report["expired"]:
            print(f"  ! Unanswered: {q.title}")
            show_result(result)
        if world.game_over:
            print("\nGAME OVER:", world.game_over)
            break
        for q in list(world.questions):
            if not play and q.due_in > 1 and world.rng.random() < 0.3:
                continue             # the autoplayer sometimes sits on a question
            print(f"  ? {q.title} (answer within {q.due_in} months)\n    {q.situation}")
            for i, o in enumerate(q.options, 1):
                print(f"      {i}. {o['label']}: {o['text']}")
            answer = (input("    Your answer (1-3, or your own words): ") if play
                      else world.rng.randint(1, len(q.options)))
            if not play:
                print(f"    -> chose {answer}")
            show_result(answer_question(world, q, answer or 2))
        if world.month % 3 == 0:
            print("  Word on the street:")
            for line in happiness.mood_report(world).values():
                print(f"    {line}")
            show_table(world)
