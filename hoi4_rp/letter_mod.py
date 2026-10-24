"""Mod files for diplomatic letters: a "Write Letter" diplomatic action, its writing area, an
"awaiting reply" notice and a reply window.

"Write Letter" is a scripted diplomatic action, so it sits in the vanilla action list. Its send popup
shows our writing area (send_scripted_gui); the game's own Send/Cancel buttons send or close it.

Typing works without a text box (script can't read one): the writing area holds one hidden button
per key, bound with `shortcut`. Each press appends that key's code to the player's array
airp_letter_text; scripted localisation turns the array back into text on screen. On Send, the
recipient goes into airp_outbox_to and Python (letters.py) reads both through the bridge.

Variables on the player's country (the save format lowercases names, so keep them lowercase):
  airp_letter_text     array of character codes (see KEYS)
  airp_outbox_to       set on Send; Python reads the letter, then clears this and the text
  airp_awaiting_from   set on Send; Python clears it when the reply is shown
  airp_reply_from      the reply window is open while it's set; text is the airp_reply_body key,
                       which letters.py sets with bridge.setloc
"""

from __future__ import annotations

import os
import string
from typing import Dict, List, Tuple

MAX_CHARS = 400

# (shortcut, character). Code = position + 1. Shifted punctuation follows the key names known to
# work in other mods; add keys at the END so existing codes don't shift.
KEYS: List[Tuple[str, str]] = (
    [(c, c) for c in string.ascii_lowercase]
    + [(f"shift+{c}", c.upper()) for c in string.ascii_lowercase]
    + [(d, d) for d in string.digits]
    + [("SPACE", " "), (".", "."), (",", ","), ("'", "'"), ("-", "-"),
       ("shift+/", "?"), ("shift+1", "!"), ("shift+2", '"'), ("shift+9", "("), ("shift+0", ")")]
)
CHARS = {i + 1: ch for i, (_, ch) in enumerate(KEYS)}


def decode_text(codes: List[float]) -> str:
    return "".join(CHARS.get(int(round(c)), "") for c in codes)


def _loc_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------- interface/airp_letters.gui

_HIDDEN_KEY = """
		buttonType = {{
			name = "{name}"
			position = {{ x = -1000 y = -1000 }}
			quadTextureSprite = "GFX_button_123x34"
			shortcut = "{shortcut}"
		}}"""

_GUI = """guiTypes = {
	# Writing area, shown inside the game's send popup for the airp_send_letter action.
	containerWindowType = {
		name = "airp_letter_compose_window"
		size = { width = 520 height = 380 }
		orientation = center
		clipping = no

		instantTextBoxType = {
			name = "airp_compose_body"
			position = { x = 25 y = 10 }
			font = "hoi_18mbs"
			format = left
			text = "AIRP_COMPOSE_BODY"
			maxWidth = 470
			maxHeight = 300
		}

		instantTextBoxType = {
			name = "airp_compose_hint"
			position = { x = 20 y = 350 }
			font = "hoi_16mbs"
			format = center
			text = "AIRP_COMPOSE_HINT"
			maxWidth = 480
			maxHeight = 20
			fixedsize = yes
		}

		buttonType = {
			name = "airp_backspace_button"
			position = { x = -1000 y = -1000 }
			quadTextureSprite = "GFX_button_123x34"
			shortcut = "BACKSPACE"
		}
@KEYS@
	}

	# Notice while the reply is being written.
	containerWindowType = {
		name = "airp_awaiting_window"
		position = { x = -200 y = 90 }
		size = { width = 400 height = 36 }
		orientation = center_up

		background = {
			name = "background"
			spriteType = "GFX_tiled_window_transparent"
		}

		instantTextBoxType = {
			name = "airp_awaiting_text"
			position = { x = 10 y = 9 }
			font = "hoi_16mbs"
			format = center
			text = "AIRP_AWAITING"
			maxWidth = 380
			maxHeight = 20
			fixedsize = yes
		}
	}

	# Reply window.
	containerWindowType = {
		name = "airp_letter_reply_window"
		position = { x = -260 y = -230 }
		size = { width = 520 height = 460 }
		orientation = center
		moveable = yes
		clipping = no

		background = {
			name = "background"
			spriteType = "GFX_tiled_window_2b_border"
		}

		instantTextBoxType = {
			name = "airp_reply_title"
			position = { x = 20 y = 18 }
			font = "hoi_20b"
			format = center
			text = "AIRP_REPLY_TITLE"
			maxWidth = 480
			maxHeight = 30
			fixedsize = yes
		}

		instantTextBoxType = {
			name = "airp_reply_body"
			position = { x = 25 y = 65 }
			font = "hoi_18mbs"
			format = left
			text = "AIRP_REPLY_BODY"
			maxWidth = 470
			maxHeight = 320
		}

		buttonType = {
			name = "airp_close_button"
			position = { x = 198 y = 405 }
			quadTextureSprite = "GFX_button_123x34"
			buttonFont = "hoi_16mbs"
			buttonText = "AIRP_CLOSE"
			clicksound = click_close
		}
	}
}
"""


def _gui() -> str:
    keys = "".join(_HIDDEN_KEY.format(name=f"airp_key_{i + 1}", shortcut=sc) for i, (sc, _) in enumerate(KEYS))
    return _GUI.replace("@KEYS@", keys)


# ---------------------------------------------------------------- common/scripted_diplomatic_actions

def _diplomatic_actions() -> str:
    # ROOT is the sender, THIS (PREV inside ROOT = { }) the receiver.
    return f"""# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.

scripted_diplomatic_actions = {{
	airp_send_letter = {{
		icon = 1
		visible = {{
			ROOT = {{ is_ai = no }}
		}}
		selectable = {{
			ROOT = {{
				NOT = {{ has_variable = airp_awaiting_from }}
				NOT = {{ has_variable = airp_reply_from }}
			}}
		}}
		requires_acceptance = no
		show_acceptance_on_action_button = no
		cost = 0

		send_scripted_gui = airp_letter_compose
		reset_send_effect = {{
			ROOT = {{ clear_array = airp_letter_text }}
		}}
		can_be_sent = {{
			ROOT = {{ check_variable = {{ airp_letter_text^num > 0 }} }}
		}}
		# Actions without acceptance may complete without running on_sent_effect, so both set the
		# same variables (running twice is harmless).
		on_sent_effect = {{
			ROOT = {{
				set_variable = {{ airp_outbox_to = PREV.id }}
				set_variable = {{ airp_awaiting_from = PREV.id }}
			}}
		}}
		complete_effect = {{
			ROOT = {{
				set_variable = {{ airp_outbox_to = PREV.id }}
				set_variable = {{ airp_awaiting_from = PREV.id }}
			}}
		}}

		ai_desire = {{ base = 0 }}
	}}
}}
"""


# ---------------------------------------------------------------- common/scripted_guis

def _scripted_guis() -> str:
    # In the diplomatic_action context ROOT is the sender (the player); write to ROOT explicitly.
    key_effects = "".join(
        f"\t\t\tairp_key_{i + 1}_click = {{ ROOT = {{ add_to_array = {{ airp_letter_text = {i + 1} }} }} }}\n"
        for i in range(len(KEYS)))
    key_triggers = "".join(
        f"\t\t\tairp_key_{i + 1}_click_enabled = {{ ROOT = {{ check_variable = {{ airp_letter_text^num < {MAX_CHARS} }} }} }}\n"
        for i in range(len(KEYS)))
    return f"""# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.

scripted_gui = {{
	airp_letter_compose = {{
		context_type = diplomatic_action
		window_name = "airp_letter_compose_window"

		visible = {{ always = yes }}
		effects = {{
			airp_backspace_button_click = {{
				ROOT = {{
					if = {{
						limit = {{ check_variable = {{ airp_letter_text^num > 0 }} }}
						set_temp_variable = {{ airp_last = airp_letter_text^num }}
						subtract_from_temp_variable = {{ airp_last = 1 }}
						remove_from_array = {{ array = airp_letter_text index = airp_last }}
					}}
				}}
			}}
{key_effects}		}}
		triggers = {{
{key_triggers}		}}
		ai_enabled = {{ always = no }}
	}}

	airp_letter_awaiting = {{
		context_type = player_context
		window_name = "airp_awaiting_window"
		visible = {{ has_variable = airp_awaiting_from }}
		ai_enabled = {{ always = no }}
	}}

	airp_letter_reply = {{
		context_type = player_context
		window_name = "airp_letter_reply_window"

		visible = {{ has_variable = airp_reply_from }}
		effects = {{
			airp_close_button_click = {{
				clear_variable = airp_reply_from
			}}
		}}
		ai_enabled = {{ always = no }}
	}}
}}
"""


# ---------------------------------------------------------------- common/scripted_localisation

def _scripted_loc() -> str:
    chars = "".join(
        f"\ttext = {{ trigger = {{ check_variable = {{ airp_c = {code} }} }} localization_key = airp_ch_{code} }}\n"
        for code in CHARS)
    return f"""# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.
# GetAirpTyped renders the airp_letter_text array as text, one character per recursion step.
# Call it on the player's country: [ROOT.GetAirpTyped].

defined_text = {{
	name = GetAirpTyped
	text = {{
		trigger = {{
			set_temp_variable = {{ airp_i = -1 }}
			always = yes
		}}
		localization_key = "[This.GetAirpNextChar]"
	}}
}}

defined_text = {{
	name = GetAirpNextChar
	text = {{
		trigger = {{
			add_to_temp_variable = {{ airp_i = 1 }}
			check_variable = {{ airp_letter_text^num > airp_i }}
			set_temp_variable = {{ airp_c = airp_letter_text^airp_i }}
		}}
		localization_key = "[This.GetAirpChar][This.GetAirpNextChar]"
	}}
	text = {{
		trigger = {{ always = yes }}
		localization_key = ""
	}}
}}

defined_text = {{
	name = GetAirpChar
{chars}	text = {{ trigger = {{ always = yes }} localization_key = "" }}
}}

# A plain loc key in a window is read once when the game loads; going through scripted
# localisation makes the reply window pick up the text letters.py sets at runtime.
defined_text = {{
	name = GetAirpReplyBody
	text = {{
		trigger = {{ always = yes }}
		localization_key = airp_reply_body
	}}
}}
"""


# ---------------------------------------------------------------- localisation

def _loc() -> str:
    lines = [
        "l_english:",
        # Diplomatic action: <KEY>_TITLE is the button, <KEY>_ACTION_DESC its tooltip. FROM is the receiver.
        ' AIRP_SEND_LETTER_TITLE:0 "Write Letter"',
        ' AIRP_SEND_LETTER_ACTION_DESC:0 "Write a personal letter to the government of this country."',
        ' AIRP_SEND_LETTER_SEND_DESC:0 "Letter to §Y[FROM.GetNameWithFlag]§!"',
        ' AIRP_COMPOSE_BODY:0 "[ROOT.GetAirpTyped]§Y_§!"',
        f' AIRP_COMPOSE_HINT:0 "§gType on your keyboard; Backspace deletes. [?ROOT.airp_letter_text^num]/{MAX_CHARS}§!"',
        ' AIRP_AWAITING:0 "Awaiting a reply from [?airp_awaiting_from.GetNameWithFlag]..."',
        ' AIRP_REPLY_TITLE:0 "Letter from [?airp_reply_from.GetNameWithFlag]"',
        ' AIRP_REPLY_BODY:0 "[This.GetAirpReplyBody]"',
        ' airp_reply_body:0 ""',
        ' AIRP_CLOSE:0 "Close"',
    ]
    lines += [f' airp_ch_{code}:0 "{_loc_escape(ch)}"' for code, ch in CHARS.items()]
    return "\n".join(lines) + "\n"


def files(mod_dir: str) -> Dict[str, Tuple[str, str]]:
    """path -> (text, encoding) for every letter file in the mod."""
    return {
        os.path.join(mod_dir, "interface", "airp_letters.gui"): (_gui(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_diplomatic_actions", "airp_letters.txt"):
            (_diplomatic_actions(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_guis", "airp_letters.txt"): (_scripted_guis(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_localisation", "airp_letters.txt"): (_scripted_loc(), "utf-8"),
        # HOI4 silently skips loc files without the UTF-8 BOM.
        os.path.join(mod_dir, "localisation", "english", "airp_letters_l_english.yml"): (_loc(), "utf-8-sig"),
    }
