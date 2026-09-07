---
name: entity-discovery
description: Resolve uncertain smart-home entities using names, aliases, areas, and types. Use when no confident candidate is available, after an entity lookup, or when the user corrects a target.
metadata:
  version: "2"
  mcp-orchestrator-priority: "80"
  mcp-orchestrator-channels: '["voice", "telegram"]'
  mcp-orchestrator-keywords: '["find the device", "find an entity", "search for a device", "which device", "keress entitast", "keresd meg az eszkozt", "melyik eszkoz"]'
  mcp-orchestrator-followup-keywords: '["i meant", "not that one", "dehogy", "nem az", "nem arra"]'
  mcp-orchestrator-signals: '["entity_unresolved", "entity_lookup"]'
---
# Entity discovery

Use a suitable exact candidate directly. This skill being active never requires
another lookup. Explicit whole-domain or area-wide requests use supported scoped
actions, not this individual-entity search workflow; follow the bulk-operation
guidance when present.

For an unknown individual target, search the intended site's SearchEntities
once with a short translated entity phrase. Keep site names out of arguments.
Use domain or area filters only when the request or returned metadata supports
them.

After a correctly translated search without unjustified filters returns
`{"count":0,"matches":[]}`, STOP searching and report no suitable exposed entity.
Do not rephrase that valid zero-result query or add related words. A second
query is allowed only to fix a demonstrably wrong translation/filter or use an
explicit alternative name already supplied by the user or tool metadata; allow
at most one such correction. No result means no suitable exposed entity was
found, not that the device does not exist. Clarify only a concrete ambiguity.

Scores rank candidates; they do not establish identity. A room or device-type
match alone is insufficient. Check names, domains and areas, including repeated
names. Do not substitute humidity for temperature or a similar device for the
requested one.

Copy an exact returned name or alias into the action or GetEntityState call.
Keep only necessary disambiguating selectors. Search results have no fresh
measurements: read GetEntityState before answering a state question. If an exact
state result already answers the request, use it without another search or read.

All candidate lists and searches are bounded. Do not claim full coverage from
them. An unfamiliar place name or recognition error requires clarification, not
substitution of the local site.
