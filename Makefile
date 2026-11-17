.PHONY: build deploy

CONFIG_DIR ?= $(HOME)/.config/agora/agora-hub
# Address clients use for the gateway: Tailscale 100.64.0.0/10, else LAN, else any non-loopback IPv4.
PUBLIC_HOST ?= $(shell ips=$$( { ip -4 -o addr show 2>/dev/null || ifconfig; } | sed -nE 's/.*inet (addr:)?([0-9.]+).*/\2/p' | grep -v '^127\.'); \
  for re in '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.' '^192\.168\.' '^10\.' '^172\.(1[6-9]|2[0-9]|3[01])\.' '.'; do \
  echo "$$ips" | grep -m1 -E "$$re" && break; done)

build:
	docker build -t agora-hub:local .

# Creates CONFIG_DIR (and its parents) and only the config files that are missing,
# then (re)starts the hub container in the background with an automatic restart policy.
deploy: build
	@set -e; umask 077; dir="$(CONFIG_DIR)"; \
	mkdir -p "$$dir"; chmod 700 "$$dir"; \
	[ -e "$$dir/hub.yaml" ] || sed -e 's/^  machines: .*/  machines: []/' \
	  -e 's/^  startup_deployments: .*/  startup_deployments: []/' examples/hub.yaml > "$$dir/hub.yaml"; \
	[ -e "$$dir/machines.yaml" ] || echo 'machines: {}' > "$$dir/machines.yaml"; \
	[ -e "$$dir/deployments.yaml" ] || echo 'deployments: {}' > "$$dir/deployments.yaml"; \
	[ -e "$$dir/known_hosts" ] || : > "$$dir/known_hosts"; \
	echo "agora-hub config: $$dir"
	@echo "agora-hub public host: $(or $(PUBLIC_HOST),none)"
	@docker rm -f agora-hub >/dev/null 2>&1 || true
	docker run -d --restart unless-stopped --name agora-hub --publish 3333:3333 --publish 8888:8888 \
	  --mount type=bind,src="$(CONFIG_DIR)",dst=/config agora-hub:local$(if $(PUBLIC_HOST), /app/.venv/bin/python -m agora_hub \
	  --config-dir /config --host 0.0.0.0 --port 3333 --gateway-port 8888 --public-host $(PUBLIC_HOST))
	@echo "agora-hub deployed on :3333 and :8888 (logs: docker logs -f agora-hub)"
