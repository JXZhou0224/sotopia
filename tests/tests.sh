docker compose -f .devcontainer/docker-compose.yml  run --rm -u root -v $(pwd):/workspaces/sotopia devcontainer /bin/sh -c "export UV_PROJECT_ENVIRONMENT=/workspaces/.venv; cd /workspaces/sotopia; uv run --extra test --extra chat pytest tests/experimental"
