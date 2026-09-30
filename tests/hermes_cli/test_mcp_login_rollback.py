"""CLI reauthentication must preserve a working login when browser consent fails."""

import asyncio
import contextlib
import io

import pytest

pytest.importorskip("mcp.client.auth.oauth2")


@pytest.mark.parametrize("peer_committed", [False, True], ids=["cancelled", "peer-grant"])
def test_failed_cli_login_restores_old_state_without_overwriting_a_peer(
    tmp_path, monkeypatch, peer_committed
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.mcp_config as mc
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import get_manager, reset_manager_for_tests

    reset_manager_for_tests()
    storage = HermesTokenStorage("files")
    cfg = {"url": "https://resource.example/mcp", "auth": "oauth"}

    async def write_state(label, token):
        await storage.set_client_info(OAuthClientInformationFull(client_id=f"client-{label}",
            redirect_uris=["http://127.0.0.1:12345/callback"]))
        storage.save_oauth_metadata(OAuthMetadata(issuer=f"https://{label}.example/",
            authorization_endpoint=f"https://{label}.example/authorize",
            token_endpoint=f"https://{label}.example/token"))
        if token:
            await storage.set_tokens(OAuthToken(access_token=f"{label}-access", token_type="Bearer",
                refresh_token=f"{label}-refresh", expires_in=3600, scope="files.read"))

    asyncio.run(write_state("old", True))
    get_manager().get_or_build_provider("files", cfg["url"], None)
    original = storage.snapshot()
    newer = None

    def failed_probe(name, config, connect_timeout=None):
        nonlocal newer
        asyncio.run(write_state("new", peer_committed))
        newer = storage.snapshot()
        raise RuntimeError("consent cancelled")

    monkeypatch.setattr(mc, "_probe_single_server", failed_probe)
    with contextlib.redirect_stdout(io.StringIO()):
        assert mc._reauth_oauth_server("files", cfg) is False

    assert storage.snapshot() == (newer if peer_committed else original)
