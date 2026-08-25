# Brainite plugin — build and test.
BINARY := bin/brainite-hook
PKG    := ./cmd/brainite-hook
GO     ?= go

# Static, no cgo: the binary is copied onto machines we do not control.
GOFLAGS := CGO_ENABLED=0

.PHONY: build test golden latency install-local clean fmt vet dist

build: ## Build the hook binary for this machine
	@mkdir -p bin
	$(GOFLAGS) $(GO) build -ldflags="-s -w" -o $(BINARY) $(PKG)

test: build ## Unit tests, including the exit-code fuzz
	$(GO) test ./... -count=1

golden: build ## Replay every captured hook payload through the binary
	$(GO) test ./internal/handlers -run TestGolden -count=1 -v

latency: build ## Assert the hot-path budgets
	$(GO) test ./internal/handlers -run TestLatency -count=1 -v

fmt:
	$(GO) fmt ./...

vet:
	$(GO) vet ./...

install-local: build ## Symlink this plugin into ~/.claude/plugins for dogfooding
	@mkdir -p $(HOME)/.claude/plugins
	@rm -f $(HOME)/.claude/plugins/brainite
	@ln -s $(CURDIR) $(HOME)/.claude/plugins/brainite
	@echo "linked $(HOME)/.claude/plugins/brainite -> $(CURDIR)"

# Cross-compile for distribution. The hook runs on whatever the developer has.
dist: ## Build darwin/linux x amd64/arm64
	@mkdir -p bin/dist
	@for os in darwin linux; do for arch in amd64 arm64; do \
		echo "  $$os/$$arch"; \
		$(GOFLAGS) GOOS=$$os GOARCH=$$arch $(GO) build -ldflags="-s -w" \
			-o bin/dist/brainite-hook-$$os-$$arch $(PKG) || exit 1; \
	done; done

clean:
	rm -rf bin

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
