# MCP Orchestrator

A lightweight Home Assistant add-on that bridges HA Assist to Azure OpenAI via MCP (Model Context Protocol). It connects to multiple Home Assistant MCP servers across sites and exposes an Ollama-compatible API. The local HA MCP connection is auto-configured via the Supervisor API.

## Architecture

The orchestrator acts as a central hub:

- **Incoming**: Each HA site uses the built-in **Ollama** integration to send requests to the orchestrator's `/api/chat` endpoint.
- **Outgoing**: The orchestrator connects to each site's MCP server, gathers available tools, namespaces them by site, and uses Azure OpenAI to orchestrate tool-calling across all sites.

## Configuration

| Option | Type | Description |
|---|---|---|
| `azure_openai_endpoint` | url | Azure OpenAI resource endpoint |
| `azure_openai_api_key` | password | Azure OpenAI API key |
| `azure_openai_deployment` | str | Deployment/model name (e.g., `gpt-4o`) |
| `azure_openai_api_version` | str | API version (default: `2024-10-21`) |
| `azure_openai_extra` | str | Extra kwargs passed to chat completion calls as a JSON string (e.g., `{"reasoning_effort": "low"}`) |
| `local_site_name` | str | Name for the local HA MCP connection (default: `local`) |
| `local_site_keywords` | str | Comma-separated keywords that identify the local site in user messages |
| `remote_mcp_servers` | list | Remote MCP server connections (name, url, token, keywords) |
| `system_prompt` | str | Master system prompt prepended to every request |
| `global_keywords` | str | Comma-separated keywords that trigger sending all sites' tools |
| `max_tool_iterations` | int | Max tool-calling loop iterations (1–50, default: 10) |

### Tool Filtering

To reduce prompt size and latency, the orchestrator selectively sends only relevant tools to Azure OpenAI:

1. **Global keywords** — if the user message contains any global keyword (e.g., "everywhere", "all sites"), all tools are sent.
2. **Site keywords** — if the user message contains a site-specific keyword (e.g., "brno", "cabin"), only that site's tools are sent.
3. **Origin detection** — if no keywords match, the orchestrator detects the origin site from the Ollama integration's Instructions (e.g., "This request originates from Home.") and sends only that site's tools.
4. **Fallback** — if nothing matches, all tools are sent.

### Example Configuration

```yaml
azure_openai_endpoint: "https://my-resource.openai.azure.com/"
azure_openai_api_key: "sk-..."
azure_openai_deployment: "gpt-4o"
azure_openai_api_version: "2024-10-21"
azure_openai_extra: '{"reasoning_effort": "low"}'
local_site_name: "home"
local_site_keywords: "home,house,main"
remote_mcp_servers:
  - name: "office"
    url: "http://office.local:8123/api/mcp"
    token: "eyJ0eXAi..."
    keywords: "office,work"
  - name: "cabin"
    url: "http://cabin.local:8123/api/mcp"
    token: "eyJ0eXAi..."
    keywords: "cabin,cottage"
system_prompt: >-
  You are a smart home assistant managing three sites: Home, Office, and Cabin.
  Each site has its own set of MCP tools prefixed with its name.
global_keywords: "everywhere,all sites,all homes"
max_tool_iterations: 10
```

The local site (where the add-on runs) connects automatically via the Supervisor API — no URL or token needed.

## HA-Side Prerequisites

Each HA site needs:

1. **MCP Server integration** enabled (Settings → Devices & Services → Add Integration → "Model Context Protocol Server")
2. Desired entities **exposed** via Settings → Voice Assistants → Expose tab
3. **Long-Lived Access Token** created for remote sites — the local site uses the Supervisor token automatically

## Client Setup (Ollama Integration)

The orchestrator exposes an Ollama-compatible API so each HA site can use the built-in **Ollama** integration — no HACS add-ons needed.

On each HA site:

1. Add the **Ollama** integration (Settings → Devices & Services → Add Integration → "Ollama")
2. Set the **URL** to `http://<orchestrator-ip>:11434`
3. Select model **ha-orchestrator:latest**
4. Configure the conversation agent — uncheck the **Assist** checkbox (the orchestrator handles tool calling internally via MCP)
5. Set per-site **Instructions** in the conversation agent config to identify the origin, e.g.: "This request originates from Office." — this is used for default tool filtering when no site keywords match the user message.
6. Assign as conversation agent in Settings → Voice Assistants

## API Endpoints

- `GET /api/tags` — Lists available models (`ha-orchestrator:latest`)
- `POST /api/chat` — Ollama-compatible chat (supports streaming via NDJSON)
