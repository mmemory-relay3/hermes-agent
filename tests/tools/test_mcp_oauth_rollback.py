"""A partial client registration is not a completed grant and must not suppress rollback."""

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
from tools.mcp_oauth import HermesTokenStorage


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_committed", [False, True], ids=["registration-only", "new-grant"])
async def test_rollback_restores_old_grant_unless_a_new_token_was_committed(
    tmp_path, monkeypatch, grant_committed
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("files")

    async def write_registration(label):
        await storage.set_client_info(OAuthClientInformationFull(client_id=f"client-{label}",
            redirect_uris=["http://127.0.0.1:12345/callback"]))
        storage.save_oauth_metadata(OAuthMetadata(issuer=f"https://{label}.example/",
            authorization_endpoint=f"https://{label}.example/authorize",
            token_endpoint=f"https://{label}.example/token"))

    await write_registration("old")
    await storage.set_tokens(OAuthToken(access_token="old-access", token_type="Bearer",
        refresh_token="old-refresh", expires_in=3600, scope="files.read"))
    original = storage.snapshot()
    storage.remove()

    await write_registration("new")
    if grant_committed:
        await storage.set_tokens(OAuthToken(access_token="new-access", token_type="Bearer",
            refresh_token="new-refresh", expires_in=3600, scope="files.read"))
    newer = storage.snapshot()

    storage.restore(original, only_if_absent=True)

    assert storage.snapshot() == (newer if grant_committed else original)
