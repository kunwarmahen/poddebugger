# Operator image — the Go control plane + the Python "brain" + kubectl.
# Build from the REPO ROOT (the build context needs both controller/ and agent/):
#
#   docker build -f deploy/Dockerfile -t poddebugger-operator:latest .
#   podman build -f deploy/Dockerfile -t poddebugger-operator:latest .

# --- stage 1: build the Go controller --------------------------------------
FROM golang:1.23 AS build
WORKDIR /src
COPY controller/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/poddebugger-watch .

# --- stage 2: runtime -------------------------------------------------------
FROM registry.access.redhat.com/ubi9/python-311:latest
ARG KUBECTL_VERSION=v1.31.0

USER 0
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl \
 && chmod 0755 /usr/local/bin/kubectl

# Install the poddebugger CLI (the brain) — the operator execs it.
COPY agent/ /opt/poddebugger-agent/
RUN pip install --no-cache-dir "/opt/poddebugger-agent[anthropic,openai]"

COPY --from=build /out/poddebugger-watch /usr/local/bin/poddebugger-watch

USER 1001
ENTRYPOINT ["poddebugger-watch"]
CMD ["--operator"]
