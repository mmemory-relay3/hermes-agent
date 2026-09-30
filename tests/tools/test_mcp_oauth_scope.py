"""Explicit OAuth permissions must survive the SDK's scope discovery (SDK issue #2317)."""

from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

from tools.mcp_dashboard_oauth import DashboardOAuthFlow, dashboard_oauth_flow
from tools.mcp_oauth import build_oauth_auth, force_interactive_oauth
from tools.mcp_oauth_manager import MCPOAuthManager
from tools.mcp_tool import sdk_httpx


class _AuthorizationCaptured(Exception):
    pass


async def _discovered_authorization(tmp_path, scope, managed):
    httpx = sdk_httpx()
    resource = "https://resource.example/mcp"
    issuer = "https://idp.example/"
    advertised = "files.read files.create files.delete"
    prm_url = "https://resource.example/.well-known/oauth-protected-resource"
    cfg = {"client_id": "pre-registered-client"}
    if scope is not None:
        cfg["scope"] = scope
    flow = DashboardOAuthFlow(flow_id="scope-test", server_name="files", profile=None,
        hermes_home=str(tmp_path), redirect_uri="https://dashboard.example/callback/files")
    with force_interactive_oauth(), dashboard_oauth_flow(flow):
        provider = (MCPOAuthManager().get_or_build_provider("files", resource, cfg)
            if managed else build_oauth_auth("files", resource, cfg))

    captured = []

    async def redirect(url):
        captured.append(url)
        raise _AuthorizationCaptured

    provider.context.redirect_handler = redirect

    def respond(request):
        if str(request.url) == resource:
            return httpx.Response(401, request=request, headers={"WWW-Authenticate":
                f'Bearer resource_metadata="{prm_url}", scope="{advertised}"'})
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(200, request=request, json={"resource": resource,
                "authorization_servers": [issuer], "scopes_supported": advertised.split()})
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(200, request=request, json={"issuer": issuer,
                "authorization_endpoint": issuer + "authorize", "token_endpoint": issuer + "token",
                "scopes_supported": advertised.split(), "code_challenge_methods_supported": ["S256"]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(_AuthorizationCaptured):
            await client.get(resource)
    assert len(captured) == 1
    return parse_qs(urlsplit(captured[0]).query), provider.context.client_metadata, advertised


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False], ids=["manager", "legacy"])
async def test_configured_scope_survives_sdk_discovery(tmp_path, monkeypatch, managed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    configured = "files.read files.create"
    query, metadata, _ = await _discovered_authorization(tmp_path, configured, managed)
    assert query["scope"] == [configured]
    assert metadata.scope == configured
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"]


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False], ids=["manager", "legacy"])
async def test_unconfigured_scope_retains_sdk_negotiation(tmp_path, monkeypatch, managed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    query, _, advertised = await _discovered_authorization(tmp_path, None, managed)
    assert set(query["scope"][0].split()) == set(advertised.split())
