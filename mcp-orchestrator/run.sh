#!/usr/bin/with-contenv bashio

export AZURE_OPENAI_ENDPOINT="$(bashio::config 'azure_openai_endpoint')"
export AZURE_OPENAI_API_KEY="$(bashio::config 'azure_openai_api_key')"
export AZURE_OPENAI_DEPLOYMENT="$(bashio::config 'azure_openai_deployment')"
export AZURE_OPENAI_EXTRA="$(bashio::config 'azure_openai_extra')"
export LOCAL_SITE_NAME="$(bashio::config 'local_site_name')"
export LOCAL_SITE_KEYWORDS="$(bashio::config 'local_site_keywords')"
export REMOTE_SITES="$(bashio::config 'remote_sites')"
export SYSTEM_PROMPT="$(bashio::config 'system_prompt')"
export GLOBAL_KEYWORDS="$(bashio::config 'global_keywords')"
export MAX_TOOL_ITERATIONS="$(bashio::config 'max_tool_iterations')"
export ENTITY_CONTEXT_MAX_RESULTS="$(bashio::config 'entity_context_max_results')"
export REMOTE_LOGGING_CONNECTION_STRING="$(bashio::config 'remote_logging_connection_string')"
export REMOTE_LOGGING_MODE="$(bashio::config 'remote_logging_mode')"
export API_KEY="$(bashio::config 'api_key')"
export LOG_LEVEL="$(bashio::config 'log_level')"

# Map log level to uvicorn's expected values
case "${LOG_LEVEL}" in
  debug)   UVICORN_LOG_LEVEL="debug" ;;
  warning) UVICORN_LOG_LEVEL="warning" ;;
  error)   UVICORN_LOG_LEVEL="error" ;;
  *)       UVICORN_LOG_LEVEL="info" ;;
esac

exec python3 -m uvicorn app.server:app --host 0.0.0.0 --port 11434 --log-level "${UVICORN_LOG_LEVEL}"
