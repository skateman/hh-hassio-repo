#!/usr/bin/with-contenv bashio

export AZURE_OPENAI_ENDPOINT="$(bashio::config 'azure_openai_endpoint')"
export AZURE_OPENAI_API_KEY="$(bashio::config 'azure_openai_api_key')"
export AZURE_OPENAI_DEPLOYMENT="$(bashio::config 'azure_openai_deployment')"
export AZURE_OPENAI_API_VERSION="$(bashio::config 'azure_openai_api_version')"
export AZURE_OPENAI_EXTRA="$(bashio::config 'azure_openai_extra')"
export LOCAL_SITE_NAME="$(bashio::config 'local_site_name')"
export LOCAL_SITE_KEYWORDS="$(bashio::config 'local_site_keywords')"
export REMOTE_MCP_SERVERS="$(bashio::config 'remote_mcp_servers')"
export SYSTEM_PROMPT="$(bashio::config 'system_prompt')"
export GLOBAL_KEYWORDS="$(bashio::config 'global_keywords')"
export MAX_TOOL_ITERATIONS="$(bashio::config 'max_tool_iterations')"

exec python3 -m uvicorn app.server:app --host 0.0.0.0 --port 11434
