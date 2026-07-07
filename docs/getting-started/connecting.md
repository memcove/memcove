# Connect an MCP client

Memcove serves MCP over **Streamable HTTP**. Any MCP-capable client (an agent
framework, an IDE assistant, your own code) connects to one endpoint and calls the
[tools](../tools/index.md).

## Endpoint

```text
http://localhost:8090/mcp
```

The host and port come from `MEMCOVE_HOST` / `MEMCOVE_PORT` (default `0.0.0.0:8090`).

## Identifying the tenant

Every request is scoped to a **tenant** — the isolation boundary. In the default
configuration Memcove reads the tenant from a request header:

```text
x-memcove-tenant: acme
```

That resolves to the private namespace `t_acme`. If the header is absent, the request
falls back to `MEMCOVE_DEFAULT_TENANT` (`default` → `t_default`). The header name is
configurable via `MEMCOVE_TENANT_HEADER`.

In production this header is set by an authenticating proxy, not the client. For how
identity works end to end — including the fail-closed provisioning map that maps a
verified OIDC subject to an internal tenant id — see
[Authentication & tenancy](../configuration/auth.md). For local work without any proxy,
see [Local development (no proxy)](../configuration/local-dev.md).

!!! warning
    In default header mode, anything that can reach the port can set `x-memcove-tenant`
    to any value and read that tenant's data. That is fine on localhost; it is why
    production puts an authenticating proxy in front and restricts the network. See
    [Security & trust boundary](../concepts/security.md).

## Connect Claude (native OAuth)

To let Claude — or any OAuth-capable MCP client — connect **directly**, without a proxy,
enable Memcove's native OAuth resource server. Memcove then validates bearer tokens
itself and the tenant comes from the verified token instead of a header.

1. **Enable OAuth** on the server, pointed at your IdP (Keycloak, Auth0, Okta, Google, …):

    ```bash
    MEMCOVE_OAUTH_ENABLED=true
    MEMCOVE_OAUTH_ISSUER=https://keycloak.example.com/realms/memcove
    MEMCOVE_OAUTH_AUDIENCE=memcove
    MEMCOVE_OAUTH_REQUIRED_SCOPES=["memcove.use"]
    MEMCOVE_PUBLIC_URL=https://memcove.example.com
    ```

    Full setup, claim→tenant mapping, and a runnable Keycloak example are in
    [Authentication & tenancy](../configuration/auth.md#native-oauth-resource-server).

2. **Add the server in Claude** — in Claude's connectors/MCP settings, add a custom
   connector with Memcove's URL (`https://memcove.example.com/mcp`). Claude reads the
   `/.well-known/oauth-protected-resource` metadata, discovers your authorization server,
   and runs the login flow against your IdP. On success it calls the tools as the
   authenticated user.

Unauthenticated requests get a `401` with a `WWW-Authenticate` challenge — that's the
resource server telling the client where to authenticate, not an error.

!!! note "Prefer the proxy instead?"
    If you terminate OIDC at the edge, leave OAuth disabled and keep the header model —
    the proxy authenticates and sets `x-memcove-tenant`. Both paths funnel through the
    same tenant seam. See the [proxy recipe](../configuration/auth.md).

## Minimal client (Python)

Using the official MCP SDK's Streamable HTTP client, passing the tenant header:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

async def main():
    headers = {"x-memcove-tenant": "acme"}
    async with streamablehttp_client("http://localhost:8090/mcp", headers=headers) as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            result = await session.call_tool(
                "remember_dataset",
                {
                    "name": "signups",
                    "source": {
                        "kind": "inline",
                        "format": "json_records",
                        "records": [{"day": "mon", "n": 12}, {"day": "tue", "n": 9}],
                    },
                },
            )
            print(result)
```

## Runnable demos

The repo ships two scripts that hand the real Memcove tools to a local LLM (via
[LM Studio](https://lmstudio.ai/) on `:1234`, OpenAI-compatible):

```bash
uv run python scripts/agent_demo.py      # autonomous: the model builds a mini warehouse
uv run python scripts/pipeline_demo.py   # guided: deterministic lifecycle, always completes
```

Both accept env overrides — `MEMCOVE_MCP_URL` (default `http://localhost:8090/mcp`),
`MEMCOVE_TENANT`, and the `LMSTUDIO_*` connection settings. They are the fastest way to
see an agent use every tool end to end.

!!! tip "Use a cloud model instead of LM Studio"
    The `LMSTUDIO_*` variables are just an OpenAI-compatible endpoint — point them at any
    provider. For OpenAI:

    ```bash
    export LMSTUDIO_BASE_URL=https://api.openai.com/v1
    export LMSTUDIO_API_KEY=$OPENAI_API_KEY
    export LMSTUDIO_MODEL=gpt-4o
    uv run python scripts/agent_demo.py
    ```

    No local model server required.

## Next

Walk through the core tools by hand in the [walkthrough](walkthrough.md), or jump to the
[tool reference](../tools/index.md).
