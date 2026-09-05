# Azure OpenAI MCP Orchestrator

A lightweight Home Assistant add-on that bridges HA Assist to Azure OpenAI via MCP (Model Context Protocol). It connects to multiple Home Assistant MCP servers across sites and exposes an Ollama-compatible API. The local HA MCP connection is auto-configured via the Supervisor API.

## Architecture

The orchestrator acts as a central hub:

- **Incoming**: Each HA site uses the built-in **Ollama** integration to send requests to the orchestrator's `/api/chat` endpoint.
- **Outgoing**: The orchestrator connects to each site's MCP server, gathers available tools, namespaces them by site, and uses Azure OpenAI to orchestrate tool-calling across all sites.
- **Targeted entity context**: The orchestrator parses `GetLiveContext` internally, injects only entities relevant to the current utterance, and exposes bounded search/state tools. The complete home snapshot is never sent to Azure OpenAI.
- **Catalog recovery**: If a connected Home Assistant changes or namespaces an MCP tool while the orchestrator is running, a tool-not-found response triggers catalog re-discovery and one automatic retry.

## Configuration

| Option | Type | Description |
|---|---|---|
| `azure_openai_endpoint` | url | Azure OpenAI resource endpoint |
| `azure_openai_api_key` | password | Azure OpenAI API key |
| `azure_openai_deployment` | str | Azure OpenAI deployment name |
| `azure_openai_extra` | str | Native Azure OpenAI request options as JSON (default: `{"reasoning": {"effort": "low"}}`) |
| `local_site_name` | str | Name for the local HA MCP connection (default: `local`) |
| `local_site_keywords` | str | Comma-separated keywords that identify the local site in user messages |
| `remote_sites` | list | Remote site connections (name, url, token, keywords) |
| `system_prompt` | str | Master system prompt prepended to every request |
| `global_keywords` | str | Comma-separated keywords that trigger sending all sites' tools |
| `max_tool_iterations` | int | Max tool-calling loop iterations (1–50, default: 10) |
| `entity_context_max_results` | int | Maximum relevant entities preselected per routed site (1–10, default: 5) |
| `remote_logging_connection_string` | password | Azure Blob Storage connection string or container SAS URL for remote interaction logging. Leave empty to disable (default). |
| `remote_logging_mode` | list | `missed` keeps the compact, missed-intent-only records from earlier releases (backward-compatible default); `all` logs every completed interaction and its full trace. |
| `api_key` | password | Optional API key to protect the Ollama-compatible endpoint. When set, remote clients must send `Authorization: Bearer <key>`. Requests from the local HA (Supervisor network) are always allowed without a key. |

### Tool Filtering

To reduce prompt size and latency, the orchestrator selectively sends only relevant tools to Azure OpenAI:

1. **Global keywords** — if the user message contains any global keyword (e.g., "everywhere", "all sites"), all tools are sent.
2. **Site keywords** — if the user message contains a site-specific keyword (e.g., "brno", "cabin"), only that site's tools are sent.
3. **Follow-up context** — short referential follow-ups such as “then try again” inherit the previous request's routing. Inheritance continues across a follow-up chain but never skips over a newer standalone request.
4. **Origin detection** — if no keywords or follow-up context match, the orchestrator detects the origin site from the Ollama integration's Instructions (e.g., "This request originates from Home.") and sends only that site's tools.
5. **Fallback** — if nothing matches, all tools are sent.

### Azure OpenAI

The orchestrator uses Azure's OpenAI-compatible `/openai/v1/` endpoint.
Requests remain stateless with `store=false`; encrypted reasoning state is
carried only within the current request's tool loop.

Values in `azure_openai_extra` are passed directly to the Azure OpenAI request.

### Entity Context

After connecting to each site's MCP server, the orchestrator calls `GetLiveContext` internally and parses every exposed entity into a structured cache. The raw zero-argument tool is removed. For compatibility with existing prompts, the model sees a replacement `GetLiveContext(query, domain?, area?, limit?)` façade that always requires a query and returns at most 10 matching states, never the complete snapshot.

For each request, entity names and areas are matched against the user utterance with accent-insensitive, typo-tolerant matching plus the prompt's common Hungarian→English smart-home vocabulary. Only the top `entity_context_max_results` candidates per routed site are injected **inline** through `{entities}` in `SITE: name [type], ...` format:

```
HOME: Living Room Lamp [light], Living Room Temperature [sensor]
```

This list is deliberately non-exhaustive. When the correct entity is not preselected, the model uses two virtual tools namespaced to the routed site:

- `SearchEntities(query, domain?, area?, limit?)` refreshes the internal snapshot and returns at most 10 matching names without state data.
- `GetEntityState(name, domain?, area?)` refreshes the internal snapshot and returns current data for at most 10 exact name matches. Domain and area can disambiguate repeated names. If the name is not exact, it returns a bounded candidate list instead of the full home.
- `GetLiveContext(query, domain?, area?, limit?)` is a bounded compatibility shortcut for older prompts. New prompts should prefer the two explicit tools above.

Area names participate in matching but are omitted from inline candidates to prevent the model from prepending areas to entity names in control calls. If no candidate matches, `{entities}` tells the model to use `SearchEntities`. If `{entities}` is absent from the system prompt, preselection is disabled but both virtual tools remain available.

### Tool-call Hardening

Before calling Home Assistant, the orchestrator:

- Removes empty optional arguments such as `""`, `null`, `[]`, and `{}`.
- Validates exact `name` values against the exposed entity cache.
- When a unique exact name is present, removes alternative target selectors (`area`, `floor`, `domain`, and `device_class`) while preserving action-specific values. Duplicate names retain only the area or domain needed to disambiguate them.
- Keeps `area` for operations where it is action data, such as `HassVacuumCleanArea`.
- Promotes MCP error responses to tool failures, so the model receives an explicit error and remote logs use the `tool_error` outcome.

### Example Configuration

```yaml
azure_openai_endpoint: "https://my-resource.openai.azure.com/"
azure_openai_api_key: "sk-..."
azure_openai_deployment: "my-deployment"
azure_openai_extra: '{"reasoning": {"effort": "low"}}'
local_site_name: "home"
local_site_keywords: "home,house,main"
remote_sites:
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
  Tool names include the site prefix (e.g. site__tool). Results from a site's tools always belong to that site.
  Entities preselected as relevant to this request (this is not a complete list):
  {entities}
  Use only the name part (before the brackets) in the "name" field of tool calls.
  If the exact entity is not listed, use that site's SearchEntities tool with an English query that omits the site name, then copy its exact name into the control tool.
  With a unique exact entity name, send only name and action-specific values. Preserve action data such as a vacuum cleaning area; for duplicate names include only the area or domain needed to disambiguate.
  For current sensor or device state, use GetEntityState with an exact discovered name.
  Never invent entity names, and only report an entity as missing after SearchEntities returns no match.
  Omit the site name when responding about the request's origin site. Mention sites only for cross-site or multi-site results.
global_keywords: "everywhere,all sites,all homes"
max_tool_iterations: 10
entity_context_max_results: 5
remote_logging_mode: "all"
```

The local site (where the add-on runs) connects automatically via the Supervisor API — no URL or token needed.

## HA-Side Prerequisites

Each HA site needs:

1. **MCP Server integration** enabled (Settings → Devices & Services → Add Integration → "Model Context Protocol Server")
2. Desired entities **exposed** via Settings → Voice Assistants → Expose tab
3. **Long-Lived Access Token** created for remote sites — the local site uses the Supervisor token automatically

## Client Setup (Ollama Integration)

The orchestrator exposes an Ollama-compatible API so each HA site can use the built-in **Ollama** integration — no HACS add-ons needed.

On the local HA site:
1. Add the **Ollama** integration (Settings → Devices & Services → Add Integration → "Ollama")
2. Set the **URL** to `http://d7336e3b-mcp-orchestrator:11434`
3. Select model **ha-orchestrator:latest**
4. Configure the conversation agent — uncheck the **Assist** checkbox (the orchestrator handles tool calling internally via MCP)
5. Set per-site **Instructions** in the conversation agent config to identify the origin, e.g.: "This request originates from Home." — this is used for default tool filtering when no site keywords match the user message.
6. Assign as conversation agent in Settings → Voice Assistants

On each remote HA site:

1. Add the **Ollama** integration (Settings → Devices & Services → Add Integration → "Ollama")
2. Set the **URL** to `http://<orchestrator-ip>:11434`
3. If `api_key` is set on the orchestrator, enter it in the **API Key** field (requires HA 2026.4+)
4. Select model **ha-orchestrator:latest**
5. Configure the conversation agent — uncheck the **Assist** checkbox (the orchestrator handles tool calling internally via MCP)
6. Set per-site **Instructions** in the conversation agent config to identify the origin, e.g.: "This request originates from Office." — this is used for default tool filtering when no site keywords match the user message.
7. Assign as conversation agent in Settings → Voice Assistants

## API Endpoints

- `GET /api/tags` — Lists available models (`ha-orchestrator:latest`)
- `POST /api/chat` — Ollama-compatible chat (supports streaming via NDJSON)

## Remote Logging

With `remote_logging_mode: all`, the orchestrator logs **every completed interaction**, including requests that used tools, requests that did not use tools, tool failures, and requests that reached the tool-iteration limit. The complete execution trace helps identify prompt-routing issues, incorrect arguments, unexpected tool results, entity-exposure gaps, and successful patterns worth preserving.

**Privacy note:** In `all` mode this feature stores the effective system prompts (including injected entity names), conversation messages, user commands, model responses, available tool names, tool arguments, and full tool results in Azure Blob Storage. This may contain sensitive household and conversation data. Logging is opt-in and disabled by default; use a private container with narrowly scoped access and an appropriate retention policy.

### Setup

1. Create the `mcp-orchestrator-logs` private blob container in an Azure Storage Account.
2. Generate a container-scoped SAS URL with **Read**, **Add**, **Create**, **Write**, and **List** permissions.
3. Set the complete container SAS URL as `remote_logging_connection_string` in the add-on config.
4. Set `remote_logging_mode` to `all`. The default `missed` mode preserves the previous behavior.

An account connection string is also supported and will create the container automatically if needed. With a container SAS URL, the container must already exist. Daily append blobs are stored as `logs/YYYY-MM-DD.jsonl`.

### Event Schema

In `all` mode, each version 2 JSONL record contains:

| Field | Description |
|---|---|
| `ts` | UTC timestamp |
| `schema_version` | Event schema version (`2` for complete interaction records) |
| `event_type` | Event type (`interaction`) |
| `deployment` | Azure OpenAI deployment name |
| `outcome` | `tools_used`, `no_tool_calls`, `tool_error`, `no_tools_available`, `model_error`, or `max_iterations` |
| `origin` | Detected origin site (or `null`) |
| `routed_sites` | Sites whose tools were sent |
| `user_message` | The voice command text |
| `request_messages` | Effective request messages, including system prompts and entity injection |
| `assistant_response` | Model's text reply |
| `available_tools` | Names of tools sent to the model |
| `tools_available` | Number of tools sent |
| `tool_calls` | Ordered trace with iteration, name, arguments, result, and error |
| `tool_calls_made` | Number of tool calls |
| `iterations` | Number of Azure OpenAI calls made |
| `duration_ms` | End-to-end model/tool-loop duration |
| `prompt_tokens` | Total input tokens across all iterations |
| `completion_tokens` | Total output tokens across all iterations |
| `total_tokens` | Combined token usage across all iterations |

### Analyzing Logs

Use the dependency-free `fetch-logs` tool in the prompt-refinement MCP server to retrieve and review interactions. It accepts the same container SAS URL through `REMOTE_LOGGING_CONNECTION_STRING`:

```
fetch-logs(date="2026-04-17")
fetch-logs(date_from="2026-04-14", date_to="2026-04-17", outcome="no_tool_calls")
fetch-logs(date="2026-04-17", limit=20, include_details=true)
fetch-logs(date="2026-04-17", raw=true)
```

Version 1 missed-intent records already stored in the container remain readable and are reported as `outcome=no_tool_calls`.

## Diagnostics

The add-on automatically creates diagnostic sensor entities in HA after the first chat request:

| Sensor | Tracks |
|---|---|
| `sensor.mcp_orchestrator_requests` | Total chat requests |
| `sensor.mcp_orchestrator_prompt_tokens` | Input tokens sent to Azure OpenAI |
| `sensor.mcp_orchestrator_completion_tokens` | Output tokens from Azure OpenAI |
| `sensor.mcp_orchestrator_total_tokens` | Combined token usage |
| `sensor.mcp_orchestrator_tool_calls` | MCP tool invocations |

All sensors use `state_class: total_increasing` so HA tracks rates and handles add-on restarts.

Each sensor carries per-site breakdowns as attributes (e.g., `site_Home`, `site_Office`). These are visible in the entity's "More info" dialog or via templates:

```jinja
{{ state_attr('sensor.mcp_orchestrator_total_tokens', 'site_Home') }}
```
