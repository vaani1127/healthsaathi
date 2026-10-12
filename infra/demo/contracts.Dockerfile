# Builds the contracts once; at run time deploys them to the demo's anvil chain if needed.
FROM ghcr.io/foundry-rs/foundry:v1.8.5
USER root
WORKDIR /contracts
COPY contracts/ ./
RUN mkdir -p deployments && forge build
COPY infra/demo/deploy-contracts.sh /usr/local/bin/deploy-contracts
RUN chmod +x /usr/local/bin/deploy-contracts
ENTRYPOINT ["/bin/sh", "/usr/local/bin/deploy-contracts"]
