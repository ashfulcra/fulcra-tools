# LiteLLM proxy for Fulcra — front for OpenRouter, mints per-sandbox virtual
# keys with budgets + TTLs. Deployed to Railway from this Dockerfile.
#
# Image: official LiteLLM non-root stable. Bake config.yaml in; runtime env supplies
# OPENROUTER_API_KEY, LITELLM_MASTER_KEY, and DATABASE_URL (Railway-managed Postgres).
FROM ghcr.io/berriai/litellm-non_root:main-stable
COPY config.yaml /app/config.yaml
# Railway provides $PORT; LiteLLM defaults to 4000 but we honor PORT when set.
CMD ["--config", "/app/config.yaml", "--port", "4000"]
