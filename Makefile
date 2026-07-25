# Makefile per la build e la pubblicazione dell'immagine Docker di llmproxy.
#
# La versione dell'immagine deriva dai TAG git:
#   - su un tag esatto (es. `git tag v1.2.3`) l'immagine e' :1.2.3 e viene
#     aggiornato anche :latest;
#   - fuori da un tag, la versione e' <ultimo-tag>-<n>-g<sha>[-dirty] (git
#     describe) e :latest NON viene toccato.
#
# Uso tipico:
#   git tag v1.2.3
#   make release            # build + push di :1.2.3 e :latest
#   make buildx-release     # come sopra, multi-arch (amd64+arm64) in un colpo
#
# Override delle variabili:
#   make build VERSION=1.4.0
#   make release IMAGE=lordraw/llmproxy PLATFORMS=linux/amd64

# ---------------------------------------------------------------------------
# Variabili
# ---------------------------------------------------------------------------

# Repository Docker Hub.
IMAGE     ?= lordraw/llmproxy

# Versione derivata dai tag git (rimuove l'eventuale prefisso "v").
# Fallback a "dev" se git non e' disponibile o non ci sono commit.
VERSION   ?= $(shell git describe --tags --always --dirty 2>/dev/null | sed 's/^v//' || echo dev)

# Non vuoto solo quando HEAD e' esattamente su un tag: abilita l'alias :latest.
GIT_TAG   := $(shell git describe --tags --exact-match 2>/dev/null)

# Piattaforme per le build multi-arch (target buildx-*).
PLATFORMS ?= linux/amd64

# Metadati OCI iniettati come label dell'immagine.
GIT_SHA   := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
VCS_URL   := https://github.com/lordraw77/llmproxy

# Tag calcolati.
IMAGE_VERSION := $(IMAGE):$(VERSION)
IMAGE_LATEST  := $(IMAGE):latest

# Argomenti comuni ai comandi docker build/buildx.
LABELS := \
	--label org.opencontainers.image.title=llmproxy \
	--label org.opencontainers.image.version=$(VERSION) \
	--label org.opencontainers.image.revision=$(GIT_SHA) \
	--label org.opencontainers.image.created=$(BUILD_DATE) \
	--label org.opencontainers.image.source=$(VCS_URL)

# Aggiunge -t :latest solo quando siamo su un tag esatto.
ifneq ($(GIT_TAG),)
LATEST_TAG_ARG := -t $(IMAGE_LATEST)
else
LATEST_TAG_ARG :=
endif

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Mostra questo aiuto
	@echo "llmproxy — build & publish Docker ($(IMAGE))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Versione corrente: $(VERSION)"
	@if [ -n "$(GIT_TAG)" ]; then echo "HEAD su tag: $(GIT_TAG) (verra' aggiornato anche :latest)"; \
		else echo "HEAD NON su un tag: :latest non verra' toccato"; fi

.PHONY: version
version: ## Stampa la versione/immagine calcolate dai tag git
	@echo "IMAGE   = $(IMAGE)"
	@echo "VERSION = $(VERSION)"
	@echo "TAGS    = $(IMAGE_VERSION)$(if $(GIT_TAG), + $(IMAGE_LATEST),)"

.PHONY: build
build: ## Build locale dell'immagine (:VERSION, + :latest se su tag)
	docker build $(LABELS) -t $(IMAGE_VERSION) $(LATEST_TAG_ARG) .
	@echo "Creata $(IMAGE_VERSION)$(if $(GIT_TAG), e $(IMAGE_LATEST),)"

.PHONY: login
login: ## Login a Docker Hub (usa DOCKER_USER/DOCKER_PASS se presenti)
	@if [ -n "$$DOCKER_USER" ] && [ -n "$$DOCKER_PASS" ]; then \
		echo "$$DOCKER_PASS" | docker login -u "$$DOCKER_USER" --password-stdin; \
	else \
		docker login; \
	fi

.PHONY: push
push: ## Push dei tag gia' buildati su Docker Hub
	docker push $(IMAGE_VERSION)
ifneq ($(GIT_TAG),)
	docker push $(IMAGE_LATEST)
endif

.PHONY: release
release: guard-tag build push ## Build + push (:VERSION e :latest); richiede un tag git
	@echo "Release $(IMAGE_VERSION) completata."

.PHONY: publish
publish: build push 
	@echo "Publish $(IMAGE_VERSION) completata."

.PHONY: buildx-release
buildx-release: guard-tag ## Build + push multi-arch ($(PLATFORMS)) in un unico passaggio
	docker buildx build --platform $(PLATFORMS) $(LABELS) \
		-t $(IMAGE_VERSION) $(LATEST_TAG_ARG) --push .
	@echo "Release multi-arch $(IMAGE_VERSION) completata."

.PHONY: migrate-config
migrate-config: ## Genera providers.toml dalle NVIDIA_* correnti (usa .env); OUT= per il path, FORCE=1 per sovrascrivere
	python -m llmproxy.scripts.env_to_toml $(OUT) $(if $(FORCE),--force,)

.PHONY: migrate-config-docker
migrate-config-docker: ## Come sopra ma dentro il container (usa .env), scrive ./providers.toml sull'host
	docker compose run --rm --no-TTY llmproxy python -m llmproxy.scripts.env_to_toml - > providers.toml
	@echo "Scritto ./providers.toml; abilita il volume/PROVIDERS_CONFIG in docker-compose.yml e riavvia."

.PHONY: run
run: ## Avvia l'immagine :VERSION in locale (usa .env, porta 11434)
	docker run --rm -it --env-file .env -p 11434:11434 $(IMAGE_VERSION)

.PHONY: clean
clean: ## Rimuove le immagini locali :VERSION e :latest
	-docker rmi $(IMAGE_VERSION) $(IMAGE_LATEST) 2>/dev/null || true

# Impedisce release accidentali fuori da un tag git.
.PHONY: guard-tag
guard-tag:
	@if [ -z "$(GIT_TAG)" ]; then \
		echo "ERRORE: HEAD non e' su un tag git."; \
		echo "        Crea un tag prima di rilasciare, es.: git tag v1.2.3"; \
		echo "        (oppure forza con: make build/push VERSION=x.y.z)"; \
		exit 1; \
	fi
