"""Loads the console bridge client (hoi4-bridge/client/hoi4bridge.py).

The client lives in the hoi4-bridge repo, expected next to this one. Set HOI4_BRIDGE_CLIENT to
point elsewhere.
"""

import os
import sys

_REPOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DIR = os.environ.get("HOI4_BRIDGE_CLIENT") or os.path.join(_REPOS, "hoi4-bridge", "client")
if not os.path.isfile(os.path.join(_DIR, "hoi4bridge.py")):
    raise ImportError(f"console bridge client not found in {_DIR} (set HOI4_BRIDGE_CLIENT)")
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from hoi4bridge import (BridgeError, BridgeUnavailable, country_block, decode_country, editbox,  # noqa: E402,F401
                        editbox_set, loc, player_tag, query, send, setloc, surrender, trigger)
