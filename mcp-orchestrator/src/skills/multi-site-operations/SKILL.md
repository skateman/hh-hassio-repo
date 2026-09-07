---
name: multi-site-operations
description: Plan smart-home requests that explicitly span multiple sites or everywhere. Use for multi-site control and comparisons while preserving scope and reporting partial results.
metadata:
  version: "2"
  mcp-orchestrator-priority: "60"
  mcp-orchestrator-channels: '["voice", "telegram"]'
  mcp-orchestrator-signals: '["multi_site_request"]'
---
# Multi-site operations

"Everywhere" or "mindenhol" explicitly selects every configured site; do not ask
which one. Otherwise keep the requested sites and areas. Several available
toolsets alone never authorize operating everywhere.

For bulk lighting, follow this workflow:

1. Call each requested site's advertised on/off action directly with
   `{"domain":["light"]}`. Add an exact `area` only for a room-scoped request.
   Selectors narrow targets: leave unused `name`, `floor` and `device_class`
   absent or empty. Filling `device_class` with every enum value is not a
   wildcard. Never invent `name="all lights"`, `"lights"` or `"all"`.
   Do not enumerate lights before a supported scoped action.
2. Natural-language "all lights" can also include lighting exposed as switches.
   Unless the user explicitly excludes switches or requests only the light
   domain, search each site's switches with
   `{"query":"lights","domain":"switch","limit":10}`. Scoped light actions and
   these lookups may run together. Restrict room-scoped lookups to that room.
3. From the switch results, keep only names or aliases clearly identifying
   lighting. A switch domain, room match or score alone is insufficient.
   Remove every name already present in that site's successful light-action
   results. A different domain or entity ID does NOT justify controlling that
   name again. Unless the user explicitly requested a distinct switch, skip
   ambiguous same-name entries rather than replaying them through another domain.
4. Execute the remaining confirmed lighting switches by exact name and necessary
   disambiguation BEFORE replying. Search results are not completed actions.
   Never target all switches, plugs, doors or appliances.

Search results are bounded, not exhaustive. If full lighting coverage cannot be
established, summarize "the lights and identified lighting switches", not "every
light everywhere". If scoped targeting is
unsupported, control only confirmed in-scope targets and disclose partial
coverage; do not equate the first search page with the whole site.

Keep targets paired with their site's advertised tool. Independent sites may run
together. Preserve successes; never repeat them when another site fails.
Summarize failures separately.

For comparisons, read the requested sensor at each site. Keep units and
unavailable readings distinct; never substitute another sensor or treat missing
data as zero.
