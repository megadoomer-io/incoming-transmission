.PHONY: help diagrams test

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test: ## Run the dev test suite (shell syntax + pytest)
	@bash -n runtime/telegram-spawn.sh runtime/telegram-send.sh runtime/telegram-inbox.sh runtime/poll-render.sh
	@if command -v uv >/dev/null 2>&1; then uv run --group dev pytest; else pytest; fi

diagrams: ## Render dot diagrams to SVG (requires graphviz)
	@find docs/diagrams -name '*.dot' -exec sh -c 'dot -Tsvg "$$1" -o "$${1%.dot}.svg"' _ {} \;
	@echo "Rendered $$(find docs/diagrams -name '*.svg' | wc -l | tr -d ' ') diagrams"
