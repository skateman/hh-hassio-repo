---
name: error-recovery
description: Handle failed smart-home tool calls and corrective follow-ups. Use when a tool reports an error or the user asks to retry or explain a failed action.
metadata:
  version: "2"
  mcp-orchestrator-priority: "100"
  mcp-orchestrator-channels: '["voice", "telegram"]'
  mcp-orchestrator-followup-keywords: '["try again", "retry", "why not", "what went wrong", "why did that fail", "probald ujra", "miert nem", "dehogy", "nem igaz", "mi volt a sikertelenseg oka"]'
  mcp-orchestrator-outcomes: '["tool_error"]'
---
# Error recovery

Choose the response from tool evidence actually available, not an earlier
assistant sentence:

- The earlier tool result is absent: answer, in the user's language, "I cannot
  see the earlier tool result, so the cause is unknown." Missing conversation
  history does not mean the original call returned no error. Do not invent a
  failed lookup, offline device or unsent command. A new state reading cannot
  establish an earlier failure's cause; do not call tools just to explain it.
- Explicit argument rejection before execution: make at most one corrected
  retry supported by the request, schema or returned metadata. For an authorized
  all-light-domain action rejected for `name="all lights"`, remove the name
  constraint and use `{"domain":["light"]}`; do not search for an aggregate entity.
  For an unknown or ambiguous individual name, use a justified lookup instead.
- Explicit device-unavailable or communication failure: report the failure and
  stop on that target. Do not retry identical calls or substitute another device.
- Timeout or uncertain execution: read the target's state first when possible.
  Never blindly repeat a non-idempotent action or treat uncertainty as success.

A user-requested retry is a new attempt at the same intended target, not proof
of the previous failure's cause. Preserve the requested site, entity and scope;
missing site tools do not authorize searching another home.

Failure at one site does not cancel pending work elsewhere. Before replying,
finish the remaining confirmed in-scope targets at healthy sites; finding a
target is not executing its action. Never repeat successful operations.
Report confirmed successes, failed targets and unsupported coverage separately.
An attempted call is not a completed action; connection failure does not mean
the device does not exist. Keep the explanation concise in the user's language.
