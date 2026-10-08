"""Profile delete on a serve process hosting several profiles (Desktop's one local backend behind a remote
primary): the deleted profile's live sessions are closed through the normal teardown before its files go,
every client is told, and other profiles' sessions keep running. Before, nothing stopped them: the turn
kept running against a home rmtree had removed."""

from __future__ import annotations

from types import SimpleNamespace

import tui_gateway.server as server


def _session(home, sid):
    return {"profile_home": str(home), "session_key": f"stored-{sid}", "agent": SimpleNamespace(), "running": False}


def test_closes_only_the_deleted_profiles_sessions_and_announces_them(tmp_path, monkeypatch):
    doomed, kept = tmp_path / "profiles" / "reviewer", tmp_path / "profiles" / "calendar-demo"
    doomed.mkdir(parents=True)
    kept.mkdir(parents=True)
    monkeypatch.setitem(server._sessions, "r1", _session(doomed, "r1"))
    monkeypatch.setitem(server._sessions, "r2", _session(doomed, "r2"))
    monkeypatch.setitem(server._sessions, "c1", _session(kept, "c1"))
    torn, events = [], []
    monkeypatch.setattr(server, "_teardown_popped_session",
                        lambda s, end_reason: torn.append((s["session_key"], end_reason)) or True)
    monkeypatch.setattr(server, "_broadcast_global_event", lambda kind, payload: events.append((kind, payload)))

    assert server.close_sessions_under(doomed) == 2

    assert sorted(torn) == [("stored-r1", "profile_deleted"), ("stored-r2", "profile_deleted")]
    assert sorted((p["session_id"], p["reason"]) for k, p in events if k == "session.reclaimed") == [
        ("r1", "profile_deleted"), ("r2", "profile_deleted")]
    assert "c1" in server._sessions and "r1" not in server._sessions and "r2" not in server._sessions


def test_delete_profile_closes_live_sessions_in_a_chat_hosting_process(tmp_path, monkeypatch):
    from hermes_cli import profiles_live_teardown

    closed = []
    monkeypatch.setattr(server, "close_sessions_under", lambda d: closed.append(d) or 1)
    profiles_live_teardown.close_live_sessions(tmp_path / "profiles" / "reviewer")
    assert closed == [tmp_path / "profiles" / "reviewer"]
