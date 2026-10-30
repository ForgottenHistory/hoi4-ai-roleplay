"""Mod files for diplomatic letters: a "Write Letter" diplomatic action, its writing area, an
"awaiting reply" notice and a reply window.

"Write Letter" is a scripted diplomatic action, so it sits in the vanilla action list. Its send popup
shows our writing area (send_scripted_gui); the game's own Send/Cancel buttons send or close it.

The writing area is a one-line text box (EDITBOX_NAME) under a "page" (PAGE_NAME). The engine's text
box only ever draws the line with the cursor, so the bridge copies its whole text onto the page, a
normal wrapping text field, as you type (`bridge.editbox.mirror`); `bridge.editbox.multiline` makes
Enter start a new line. Script can't read text boxes, so Python reads the letter through the bridge
(`bridge.editbox NAME`). letters.py registers both bridge settings while it runs.

Variables on the player's country (the save format lowercases names, so keep them lowercase):
  airp_outbox_to       set on Send; Python reads the text box, then clears this and the box
  airp_awaiting_from   set on Send; Python clears it when the reply is shown
  airp_reply_from      the reply window is open while it's set; text is the airp_reply_body key,
                       which letters.py sets with bridge.setloc
"""

from __future__ import annotations

import os
from typing import Dict, Tuple

EDITBOX_NAME = "airp_letter_editbox"
PAGE_NAME = "airp_letter_page"     # instant text box showing the whole letter (bridge.editbox.mirror)

# ---------------------------------------------------------------- interface/airp_letters.gui

_GUI = """guiTypes = {
	# Writing area, shown inside the game's send popup for the airp_send_letter action.
	containerWindowType = {
		name = "airp_letter_compose_window"
		size = { width = 520 height = 380 }
		orientation = center
		clipping = no

		# The page: the whole letter, wrapped. The bridge copies the input line's text here as you type
		# (bridge.editbox.mirror); until then it shows the hint.
		instantTextBoxType = {
			name = "@PAGE@"
			position = { x = 25 y = 10 }
			font = "hoi_18mbs"
			format = left
			text = "AIRP_LETTER_PAGE_HINT"
			maxWidth = 470
			maxHeight = 285
		}

		# The input line: a text box draws only the line with the cursor, so it's one line tall.
		# The box sits inside its background window: a sibling window would cover it and take the clicks.
		containerWindowType = {
			name = "airp_letter_input"
			position = { x = 97 y = 305 }
			size = { width = 325 height = 27 }
			iconType = {
				name = "airp_letter_input_bg"
				spriteType = "GFX_generic_text_bg_325"
				position = { x = 0 y = 0 }
				alwaystransparent = yes
			}
			editBoxType = {
				name = "@EDITBOX@"
				position = { x = 0 y = 0 }
				size = { x = 325 y = 27 }
				font = "hoi_18mbs"
				borderSize = { x = 8 y = 4 }
				text = ""
				orientation = "UPPER_LEFT"
			}
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

	# Reputation: a small box on the top bar showing the player's standing, the record in its tooltip. Its own window,
	# attached to the top bar by the scripted gui (parent_window_token), so no game window is changed. Drag it anywhere.
	# The full-screen outer window gives the box room to be moved (inside the top bar alone it could hardly move).
	containerWindowType = {
		name = "airp_rep_container"
		position = { x = 0 y = 0 }
		size = { width = 102%% height = 107%% }

		containerWindowType = {
			name = "airp_rep_box"
			position = { x = 1125 y = 4 }
			size = { width = 180 height = 26 }
			moveable = yes

			background = {
				name = "background"
				spriteType = "GFX_generic_box_small"
				alwaystransparent = no
			}

			# Left-aligned: centring text that changes at runtime put it right of the middle, past the box.
			instantTextBoxType = {
				name = "airp_rep_value"
				position = { x = 8 y = 4 }
				font = "hoi_18mbs"
				text = "AIRP_REP_VALUE"
				format = left
				maxWidth = 164
				maxHeight = 23
				fixedsize = yes
				pdx_tooltip = "AIRP_REP_TIP"
			}
		}
	}

	# Treaties by letter with the country in the diplomacy view: a box at the foot of its relations column (the one
	# listing guarantees and pacts). Attached by the scripted gui, so the game's window is untouched. Positions are
	# in the diplomacy view's country_info window; that column ends 50 px above its foot, its last 55 px or so
	# under the view's bottom bar.
	containerWindowType = {
		name = "airp_treaty_box"
		position = { x = 19 y = -250 }
		size = { width = 252 height = 140 }
		orientation = LOWER_LEFT
		clipping = no

		background = {
			name = "background"
			spriteType = "GFX_tiled_window_1b_thin_border"
		}

		iconType = {
			name = "airp_treaty_header"
			position = { x = 2 y = 4 }
			spriteType = "GFX_win_header_short"
		}
		instantTextBoxType = {
			name = "airp_treaty_title"
			position = { x = 8 y = 7 }
			font = "hoi_18mbs"
			text = "AIRP_TREATY_TITLE"
			maxWidth = 236
			maxHeight = 22
			fixedsize = yes
			pdx_tooltip = "AIRP_TREATY_TIP"
		}
		instantTextBoxType = {
			name = "airp_treaty_text"
			position = { x = 10 y = 34 }
			font = "hoi_16mbs"
			text = "AIRP_TREATY_TEXT"
			format = left
			maxWidth = 232
			maxHeight = 100
			fixedsize = yes
			pdx_tooltip = "AIRP_TREATY_TIP"
		}
	}

@REPLY_WINDOWS@
}
"""


# The reply window comes in several heights (of its text area); letters.py picks the smallest that fits
# the letter by setting airp_reply_size. Script can't resize a window, so each size is its own window.
# Text taller than the biggest size scrolls.
REPLY_TEXT_HEIGHTS = [200, 320, 460, 620]
REPLY_TEXT_WIDTH = 470


def reply_window_name(size: int) -> str:
    return f"airp_letter_reply_window_{size}"


_REPLY_WINDOW = """	# Reply window, size @SIZE@: @TEXT_H@ px of text.
	containerWindowType = {
		name = "@NAME@"
		position = { x = -260 y = -@HALF_H@ }
		size = { width = 520 height = @WIN_H@ }
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
			maxWidth = @TEXT_W@
			maxHeight = @TEXT_H@
			scrollbartype = "standardtext_slider"
		}

		buttonType = {
			name = "airp_close_button"
			position = { x = 198 y = @BUTTON_Y@ }
			quadTextureSprite = "GFX_button_123x34"
			buttonFont = "hoi_16mbs"
			buttonText = "AIRP_CLOSE"
			clicksound = click_close
		}

		# With an offer attached (airp_offer set), Accept/Decline replace Close.
		buttonType = {
			name = "airp_accept_button"
			position = { x = 80 y = @BUTTON_Y@ }
			quadTextureSprite = "GFX_button_123x34"
			buttonFont = "hoi_16mbs"
			buttonText = "AIRP_ACCEPT"
			clicksound = click_action
		}
		buttonType = {
			name = "airp_decline_button"
			position = { x = 317 y = @BUTTON_Y@ }
			quadTextureSprite = "GFX_button_123x34"
			buttonFont = "hoi_16mbs"
			buttonText = "AIRP_DECLINE"
			clicksound = click_close
		}
	}
"""


def _reply_window(size: int, text_h: int) -> str:
    win_h = text_h + 140
    out = _REPLY_WINDOW
    for k, v in {"@SIZE@": size, "@TEXT_H@": text_h, "@NAME@": reply_window_name(size), "@HALF_H@": win_h // 2,
                 "@WIN_H@": win_h, "@TEXT_W@": REPLY_TEXT_WIDTH, "@BUTTON_Y@": 65 + text_h + 20}.items():
        out = out.replace(k, str(v))
    return out


def _gui() -> str:
    windows = "\n".join(_reply_window(i + 1, h) for i, h in enumerate(REPLY_TEXT_HEIGHTS))
    return (_GUI.replace("@EDITBOX@", EDITBOX_NAME).replace("@PAGE@", PAGE_NAME)
            .replace("@REPLY_WINDOWS@", windows))


def reply_size_for(text: str) -> int:
    """Smallest reply window size (1-based) whose text area fits `text`. hoi_18mbs averages about 7.5 px
    per character and 19 px per line; long words wrap early, so this errs on the tall side."""
    chars_per_line = int(REPLY_TEXT_WIDTH / 7.5)
    lines = sum(max(1, -(-len(para) // chars_per_line)) for para in text.split("\n"))
    height = lines * 19 + 10
    for i, h in enumerate(REPLY_TEXT_HEIGHTS):
        if height <= h:
            return i + 1
    return len(REPLY_TEXT_HEIGHTS)


# ---------------------------------------------------------------- common/scripted_diplomatic_actions

def _diplomatic_actions() -> str:
    # ROOT is the sender, THIS (PREV inside ROOT = { }) the receiver.
    return """# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.

scripted_diplomatic_actions = {
	airp_send_letter = {
		icon = 1
		visible = {
			ROOT = { is_ai = no }
		}
		selectable = {
			ROOT = {
				NOT = { has_variable = airp_awaiting_from }
				NOT = { has_variable = airp_reply_from }
			}
		}
		requires_acceptance = no
		show_acceptance_on_action_button = no
		cost = 0

		send_scripted_gui = airp_letter_compose

		# Actions without acceptance may complete without running on_sent_effect, so both set the
		# same variables (running twice is harmless).
		on_sent_effect = {
			ROOT = {
				set_variable = { airp_outbox_to = PREV.id }
				set_variable = { airp_awaiting_from = PREV.id }
				add_to_variable = { airp_awaiting_refresh = 1 }
				add_to_variable = { airp_reply_refresh = 1 }
			}
		}
		complete_effect = {
			ROOT = {
				set_variable = { airp_outbox_to = PREV.id }
				set_variable = { airp_awaiting_from = PREV.id }
				add_to_variable = { airp_awaiting_refresh = 1 }
				add_to_variable = { airp_reply_refresh = 1 }
			}
		}

		ai_desire = { base = 0 }
	}
}
"""


# ---------------------------------------------------------------- common/scripted_guis

_SCRIPTED_GUIS = """# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.

scripted_gui = {
	airp_letter_compose = {
		context_type = diplomatic_action
		window_name = "airp_letter_compose_window"
		visible = { always = yes }
		ai_enabled = { always = no }
	}

	airp_letter_awaiting = {
		context_type = player_context
		window_name = "airp_awaiting_window"
		visible = { has_variable = airp_awaiting_from }
		dirty = airp_awaiting_refresh        # a paused game has no ticks: update the notice the moment a letter is sent
		ai_enabled = { always = no }
	}

@REPLY_GUIS@

	# The reputation box on the top bar; letters.py sets its text and tooltip (airp_rep_value, airp_rep_tip).
	airp_reputation = {
		context_type = player_context
		parent_window_token = top_bar
		window_name = "airp_rep_container"
		visible = { always = yes }
		dirty = airp_rep_refresh             # a paused game has no ticks: letters.py bumps this when the text changes
		ai_enabled = { always = no }
	}

	# The treaty box in the diplomacy view. THIS is the country shown, ROOT the player; letters.py gives each country
	# with treaties a slot (airp_treaty_slot) whose loc keys hold the text.
	airp_treaties = {
		context_type = diplomacy_target_context
		parent_window_token = selected_country_view_diplomacy
		window_name = "airp_treaty_box"
		visible = {
			has_variable = airp_treaty_slot
			ROOT = { is_ai = no }
		}
		ai_enabled = { always = no }
	}
}
"""


_REPLY_GUI = """	airp_letter_reply_@SIZE@ = {
		context_type = player_context
		window_name = "@NAME@"

		visible = {
			has_variable = airp_reply_from
			NOT = { has_variable = airp_awaiting_from }     # a letter sent over an open reply: the notice shows
			check_variable = { airp_reply_size = @SIZE@ }
		}
		# Scripted guis update every tick, and a paused game has none: a reply opened while paused showed the
		# buttons of the state before (Close instead of Accept/Decline). Bumping this variable updates it at once.
		dirty = airp_reply_refresh
		effects = {
			airp_close_button_click = {
				clear_variable = airp_reply_from
				add_to_variable = { airp_reply_refresh = 1 }
			}
			# letters.py reads the answer (1 accept, 2 decline), applies it and clears airp_offer.
			airp_accept_button_click = {
				set_variable = { airp_offer_answer = 1 }
				clear_variable = airp_reply_from
				add_to_variable = { airp_reply_refresh = 1 }
			}
			airp_decline_button_click = {
				set_variable = { airp_offer_answer = 2 }
				clear_variable = airp_reply_from
				add_to_variable = { airp_reply_refresh = 1 }
			}
		}
		triggers = {
			airp_close_button_visible = { NOT = { has_variable = airp_offer } }
			airp_accept_button_visible = { has_variable = airp_offer }
			airp_decline_button_visible = { has_variable = airp_offer }
		}
		ai_enabled = { always = no }
	}
"""


def _scripted_guis() -> str:
    guis = "\n".join(_REPLY_GUI.replace("@SIZE@", str(i + 1)).replace("@NAME@", reply_window_name(i + 1))
                     for i in range(len(REPLY_TEXT_HEIGHTS)))
    return _SCRIPTED_GUIS.replace("@REPLY_GUIS@", guis)


# ---------------------------------------------------------------- common/scripted_localisation

def _treaty_loc(name: str, suffix: str) -> str:
    """Scripted loc for the treaty box: the text of the shown country's slot (treaties.PANEL_SLOTS of them)."""
    from .treaties import PANEL_SLOTS
    entries = "".join(f"\ttext = {{\n\t\ttrigger = {{ check_variable = {{ airp_treaty_slot = {n} }} }}\n"
                      f"\t\tlocalization_key = airp_treaty_{n}{suffix}\n\t}}\n" for n in range(1, PANEL_SLOTS + 1))
    return f"defined_text = {{\n\tname = {name}\n{entries}\ttext = {{\n\t\tlocalization_key = airp_treaty_none\n\t}}\n}}\n"


def _scripted_loc() -> str:
    return """# GENERATED by hoi4_rp/letter_mod.py. Don't edit by hand.

# A plain loc key in a window is read once when the game loads; going through scripted
# localisation makes the reply window pick up the text letters.py sets at runtime.
defined_text = {
	name = GetAirpReplyBody
	text = {
		trigger = { always = yes }
		localization_key = airp_reply_body
	}
}
defined_text = {
	name = GetAirpRepValue
	text = {
		trigger = { always = yes }
		localization_key = airp_rep_value
	}
}
defined_text = {
	name = GetAirpRepTip
	text = {
		trigger = { always = yes }
		localization_key = airp_rep_tip
	}
}
""" + _treaty_loc("GetAirpTreaty", "") + _treaty_loc("GetAirpTreatyTip", "_tip")


# ---------------------------------------------------------------- localisation

def _loc() -> str:
    lines = [
        "l_english:",
        # Diplomatic action: <KEY>_TITLE is the button, <KEY>_ACTION_DESC its tooltip. FROM is the receiver.
        ' AIRP_SEND_LETTER_TITLE:0 "Write Letter"',
        ' AIRP_SEND_LETTER_ACTION_DESC:0 "Write a personal letter to the government of this country."',
        ' AIRP_SEND_LETTER_SEND_DESC:0 "Letter to §Y[FROM.GetNameWithFlag]§!"',
        ' AIRP_COMPOSE_HINT:0 "§gEnter starts a new line.§!"',
        ' AIRP_LETTER_PAGE_HINT:0 "§gClick the field below and start writing. Your letter appears here.§!"',
        ' AIRP_AWAITING:0 "Awaiting a reply from [?airp_awaiting_from.GetNameWithFlag]..."',
        ' AIRP_REPLY_TITLE:0 "Letter from [?airp_reply_from.GetNameWithFlag]"',
        ' AIRP_REPLY_BODY:0 "[This.GetAirpReplyBody]"',
        ' airp_reply_body:0 ""',
        ' AIRP_REP_VALUE:0 "[This.GetAirpRepValue]"',
        ' AIRP_REP_TIP:0 "[This.GetAirpRepTip]"',
        ' airp_rep_value:0 "Reputation"',
        ' airp_rep_tip:0 "Your reputation for keeping your word. It shows once the AI Roleplay script is running."',
        ' AIRP_TREATY_TITLE:0 "Treaties by letter"',
        ' AIRP_TREATY_TEXT:0 "[This.GetAirpTreaty]"',
        ' AIRP_TREATY_TIP:0 "[This.GetAirpTreatyTip]"',
        ' airp_treaty_none:0 ""',
    ]
    from .treaties import PANEL_SLOTS
    for n in range(1, PANEL_SLOTS + 1):
        lines += [f' airp_treaty_{n}:0 ""', f' airp_treaty_{n}_tip:0 ""']
    lines += [
        ' AIRP_CLOSE:0 "Close"',
        ' AIRP_ACCEPT:0 "Accept"',
        ' AIRP_DECLINE:0 "Decline"',
    ]
    from .actions import opinion_loc
    lines += opinion_loc()
    return "\n".join(lines) + "\n"


def files(mod_dir: str) -> Dict[str, Tuple[str, str]]:
    """path -> (text, encoding) for every letter file in the mod."""
    from .actions import opinion_modifiers_file
    return {
        os.path.join(mod_dir, "common", "opinion_modifiers", "airp_opinion_modifiers.txt"):
            (opinion_modifiers_file(), "utf-8"),
        os.path.join(mod_dir, "interface", "airp_letters.gui"): (_gui(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_diplomatic_actions", "airp_letters.txt"):
            (_diplomatic_actions(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_guis", "airp_letters.txt"): (_scripted_guis(), "utf-8"),
        os.path.join(mod_dir, "common", "scripted_localisation", "airp_letters.txt"): (_scripted_loc(), "utf-8"),
        # HOI4 silently skips loc files without the UTF-8 BOM.
        os.path.join(mod_dir, "localisation", "english", "airp_letters_l_english.yml"): (_loc(), "utf-8-sig"),
    }
