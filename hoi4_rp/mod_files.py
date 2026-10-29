"""Generate the AI Roleplay mod: descriptor, blank template events and their localisation.

Events can't be created at runtime, so the mod holds blank ones the LLM fills in. Each "slot" is one
event's worth of text keys; several slots let a second popup open without overwriting the first.
Pictures can't change at runtime either, so each slot has one template per picture:
event id = slot * 100 + picture number (airp.101 = slot 1, first picture).
"""

from __future__ import annotations

import os

NAMESPACE = "airp"
SLOTS = 3
MAX_OPTIONS = 4
MOD_NAME = "AI Roleplay"
MOD_FOLDER = "airoleplay"
MOD_ROOT = os.path.join(os.path.expanduser("~"), "Documents", "Paradox Interactive",
                        "Hearts of Iron IV", "mod")

# (key the LLM picks, vanilla sprite, what it shows). After editing this list: `python -m hoi4_rp write-mod`, then restart HOI4.
PICTURES = [
    ("army", "GFX_report_event_generic_army", "soldiers, troops, conscription"),
    ("battle", "GFX_report_event_generic_battle", "fighting, the front line"),
    ("tanks", "GFX_report_event_generic_panzer_attack", "tanks, an armoured offensive"),
    ("aircraft", "GFX_report_event_generic_bombers", "aircraft, bombers, air war"),
    ("parade", "GFX_report_event_generic_military_parade", "a military parade, a show of strength"),
    ("factory", "GFX_report_event_generic_factory", "industry, production, the economy"),
    ("lab", "GFX_report_event_generic_research_lab", "science, technology, invention"),
    ("parliament", "GFX_report_event_generic_parliament", "politics, government, legislature"),
    ("conference", "GFX_report_event_generic_conference", "ministers, a cabinet meeting, negotiations"),
    ("handshake", "GFX_report_event_generic_handshake", "diplomacy, a meeting of leaders"),
    ("treaty", "GFX_report_event_generic_sign_treaty1", "a treaty, an agreement being signed"),
    ("rally", "GFX_report_event_generic_rally", "a political rally, crowds, speeches"),
    ("riot", "GFX_report_event_generic_riot", "unrest, a riot, protest turning violent"),
    ("funeral", "GFX_report_event_generic_funeral", "death, mourning, a state funeral"),
    ("ruins", "GFX_report_event_generic_ruins", "destruction, disaster, aftermath"),
    ("letter", "GFX_report_event_generic_read_write", "a letter, a report, a secret memo"),
    ("money", "GFX_report_event_generic_stock_traders", "finance, markets, money"),
]
PICTURE_KEYS = [p[0] for p in PICTURES]

# News events (hoi4_rp/news.py): the newspaper-styled popup with one button, pure flavour. Same idea as the
# templates above: one per slot and picture, text set at runtime. Their ids start at 1000.
NEWS_SLOTS = 3
NEWS_PICTURES = [
    ("army", "GFX_news_event_generic_army", "soldiers, troops, conscription, a parade of infantry"),
    ("tanks", "GFX_news_event_generic_panzer_attack", "tanks, an armoured offensive, fighting at the front"),
    ("aircraft", "GFX_news_event_generic_bombers", "aircraft, air force, aviation, an air raid"),
    ("navy", "GFX_news_event_generic_south_american_dreadnought_race", "warships, the navy, ports, the sea"),
    ("ruins", "GFX_news_event_generic_ruins", "destruction, disaster, fire, the aftermath of something"),
    ("factory", "GFX_news_event_generic_factory", "industry, factories, the economy, production"),
    ("lab", "GFX_news_event_generic_research_lab", "science, invention, medicine, technology"),
    ("parliament", "GFX_news_event_generic_parliament", "politics, government, laws, elections, scandal in office"),
    ("rally", "GFX_news_event_generic_rally", "crowds, celebrations, speeches, festivals"),
    ("riot", "GFX_news_event_generic_riot", "unrest, violent protest, police"),
    ("strike", "GFX_news_event_generic_strike", "a labour strike, workers' demands"),
    ("handshake", "GFX_news_event_generic_handshake", "diplomacy, leaders meeting, a visit abroad"),
    ("treaty", "GFX_news_event_generic_sign_treaty1", "a treaty or agreement being signed, a deal"),
    ("funeral", "GFX_news_event_generic_funeral", "a death, mourning, a memorial"),
    ("press", "GFX_news_event_generic_read_write", "newspapers, writing, culture, everyday oddities"),
]
NEWS_PICTURE_KEYS = [p[0] for p in NEWS_PICTURES]


def news_event_id(slot: int, picture: str) -> str:
    return f"{NAMESPACE}.{1000 + slot * 100 + NEWS_PICTURE_KEYS.index(picture) + 1}"


def news_loc_key(slot: int, part: str) -> str:
    """part: 't' headline, 'd' report, 'a' the button."""
    return f"{NAMESPACE}.n{slot}.{part}"


def event_id(slot: int, picture: str) -> str:
    return f"{NAMESPACE}.{slot * 100 + PICTURE_KEYS.index(picture) + 1}"


def loc_key(slot: int, part: str) -> str:
    """part: 't' title, 'd' desc, 'o<N>' option text, 'o<N>.tt' option tooltip."""
    return f"{NAMESPACE}.s{slot}.{part}"


def pick_var(slot: int) -> str:
    return f"{NAMESPACE}_s{slot}_pick"


def option_flag(slot: int, n: int) -> str:
    return f"{NAMESPACE}_s{slot}_o{n}"


def _event_lines(slot: int, key: str, sprite: str) -> list[str]:
    out = [
        "country_event = {",
        f"\tid = {event_id(slot, key)}\t# slot {slot}, {key}",
        f"\ttitle = {loc_key(slot, 't')}",
        f"\tdesc = {loc_key(slot, 'd')}",
        f"\tpicture = {sprite}",
        "\tis_triggered_only = yes",
    ]
    for n in range(1, MAX_OPTIONS + 1):
        # Option 1 is always shown so the event can never end up with no options.
        trigger = "" if n == 1 else f"\t\ttrigger = {{ has_country_flag = {option_flag(slot, n)} }}\n"
        out.append(
            "\toption = {\n"
            f"\t\tname = {loc_key(slot, f'o{n}')}\n"
            f"{trigger}"
            f"\t\tset_variable = {{ {pick_var(slot)} = {n} }}\n"
            f"\t\tcustom_effect_tooltip = {loc_key(slot, f'o{n}.tt')}\n"
            "\t}"
        )
    out += ["}", ""]
    return out


def write_mod(mod_root: str = MOD_ROOT) -> None:
    mod_dir = os.path.join(mod_root, MOD_FOLDER)
    descriptor = "\n".join([
        f'name="{MOD_NAME}"',
        'version="0.1.0"',
        'tags={\n\t"Gameplay"\n\t"Events"\n}',
        'supported_version="1.19.*"',
    ])
    ev = [
        "# GENERATED by D:/Stuff/Coding/hoi4-ai-roleplay (python -m hoi4_rp write-mod).",
        "# Don't edit by hand. Blank events the LLM fills in at runtime; see hoi4_rp/__init__.py.",
        "",
        f"add_namespace = {NAMESPACE}",
        "",
    ]
    loc = ["l_english:"]
    for slot in range(1, SLOTS + 1):
        for key, sprite, _ in PICTURES:
            ev += _event_lines(slot, key, sprite)
        loc.append(f' {loc_key(slot, "t")}:0 "Unwritten"')
        loc.append(f' {loc_key(slot, "d")}:0 "The game master has not written this yet."')
        for n in range(1, MAX_OPTIONS + 1):
            loc.append(f' {loc_key(slot, f"o{n}")}:0 "Continue."')
            loc.append(f' {loc_key(slot, f"o{n}.tt")}:0 ""')

    for slot in range(1, NEWS_SLOTS + 1):
        for key, sprite, _ in NEWS_PICTURES:
            # a country_event with the news picture: HOI4 (as of the 2026 update) shows no news_event fired from the console
            ev += ["country_event = {",
                   f"\tid = {news_event_id(slot, key)}\t# news, slot {slot}, {key}",
                   f"\ttitle = {news_loc_key(slot, 't')}",
                   f"\tdesc = {news_loc_key(slot, 'd')}",
                   f"\tpicture = {sprite}",
                   "\tis_triggered_only = yes",
                   f"\toption = {{ name = {news_loc_key(slot, 'a')} }}",
                   "}", ""]
        loc += [f' {news_loc_key(slot, "t")}:0 "News"',
                f' {news_loc_key(slot, "d")}:0 "This story has not been written yet."',
                f' {news_loc_key(slot, "a")}:0 "Interesting."']

    files = {
        os.path.join(mod_root, f"{MOD_FOLDER}.mod"): (descriptor + f'\npath="mod/{MOD_FOLDER}"\n', "utf-8"),
        os.path.join(mod_dir, "descriptor.mod"): (descriptor + "\n", "utf-8"),
        os.path.join(mod_dir, "events", "airp_templates.txt"): ("\n".join(ev), "utf-8"),
        # HOI4 silently skips loc files without the UTF-8 BOM.
        os.path.join(mod_dir, "localisation", "english", "airp_templates_l_english.yml"):
            ("\n".join(loc) + "\n", "utf-8-sig"),
    }
    from . import letter_mod
    files.update(letter_mod.files(mod_dir))
    from . import guarantees, peace
    files.update(peace.files(mod_dir))
    files.update(guarantees.files(mod_dir))
    for path, (text, enc) in files.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding=enc, newline="\n") as f:
            f.write(text)
        print(f"wrote {path}")
    print(f"{SLOTS} slots x {len(PICTURES)} pictures = {SLOTS * len(PICTURES)} template events, "
          f"{NEWS_SLOTS} x {len(NEWS_PICTURES)} news events")
