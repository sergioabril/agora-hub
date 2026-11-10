.PHONY: build deploy

CONFIG_DIR ?= $(HOME)/.config/agora/agora-hub

build:
	docker build -t agora-hub:local .

# Creates CONFIG_DIR (and its parents) and only the config files that are missing,
# then (re)starts the hub container in the background with an automatic restart policy.
deploy: build
	@set -e; umask 077; dir="$(CONFIG_DIR)"; \
	mkdir -p "$$dir"; chmod 700 "$$dir"; \
	[ -e "$$dir/hub.yaml" ] || sed -e 's/^  machines: .*/  machines: []/' \
	  -e 's/^  startup_deployment: .*/  startup_deployment: null/' examples/hub.yaml > "$$dir/hub.yaml"; \
	[ -e "$$dir/machines.yaml" ] || echo 'machines: {}' > "$$dir/machines.yaml"; \
	[ -e "$$dir/deployments.yaml" ] || echo 'deployments: {}' > "$$dir/deployments.yaml"; \
	[ -e "$$dir/known_hosts" ] || : > "$$dir/known_hosts"; \
	echo "agora-hub config: $$dir"
	@docker rm -f agora-hub >/dev/null 2>&1 || true
	docker run -d --restart unless-stopped --name agora-hub --publish 3333:3333 \
	  --mount type=bind,src="$(CONFIG_DIR)",dst=/config agora-hub:local
	@echo "agora-hub deployed on :3333 (logs: docker logs -f agora-hub)"
