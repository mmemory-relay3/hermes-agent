"""Profile delete/rename: stop what a serve process still runs FOR the profile before its directory goes.

Kept out of ``profiles`` (over the line-count ratchet) and imported lazily by its delete/rename paths.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("hermes_cli.profiles")


def stop_bot_desktop(profile_dir: Path) -> None:
    """Stop the profile's Bot Desktop (Xvnc + Xfce launcher) before its directory is removed or renamed;
    gateway shutdown does not reach it (its own session, its own pid file). Scoped through the hermes-home
    override so the runtime reads THIS profile's bot-desktop/ state, whichever profile invoked the op.
    A failure here is logged, never fatal: the profile op is what the user asked for."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import runtime
    if not runtime.is_supported_host():
        return
    token = set_hermes_home_override(profile_dir)
    try:
        if runtime.stop():
            print("✓ Bot Desktop stopped")
            # The screen a human's exclusion protected is gone; on rename the directory (lease.json
            # included) moves with the profile, and a human lease for a dead viewer would fence the
            # agent out of the renamed profile's next screen until someone force-released it.
            from tools.bot_desktop import lease
            lease.release()
    except Exception as e:  # health: allow BLE001 -- moved verbatim from profiles; the profile op must never fail on the screen runtime's own errors
        logger.warning("Could not stop the Bot Desktop of %s: %s", profile_dir, e)
    finally:
        reset_hermes_home_override(token)


def close_live_sessions(profile_dir: Path) -> None:
    """Close the profile's live sessions in THIS process before its files go. A serve process hosting several
    profiles (Desktop's one local backend) still holds them: a turn must end through its normal finalize path,
    not write into a removed home. Other profiles keep running. ``tui_gateway.server`` is loaded only in a
    process that hosts chats, so from the CLI this is a no-op; a failure is logged, never fatal."""
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return
    try:
        closed = server.close_sessions_under(profile_dir)
    except (RuntimeError, OSError, ValueError, AttributeError) as e:
        logger.warning("Could not close live sessions of %s before delete: %s", profile_dir, e)
        return
    if closed:
        print(f"✓ Closed {closed} live session(s) of this profile")
