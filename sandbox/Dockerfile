# Coder-agent sandbox image (Stage 13D — HLD §18.6).
#
# Scripts written by the Coder agent run in a container built from this
# image — a SIBLING of the target workload that joins its network
# namespace (Podman) or attaches as an ephemeral debug container (K8s),
# but never shares the target's filesystem.
#
# Build:  podman build -t poddebugger-coder-sandbox sandbox/
# Override the image the engine uses with PODDEBUGGER_CODER_IMAGE.
FROM docker.io/library/alpine:3.20

RUN apk add --no-cache \
        bash \
        python3 \
        py3-requests \
        curl \
        wget \
        jq \
        bind-tools \
        netcat-openbsd \
        postgresql-client \
        mariadb-client \
        redis

# No entrypoint: the engine passes the interpreter + script explicitly
# (`sh -c …` / `python3 -c …`). Non-root by default.
RUN adduser -D -u 10001 coder
USER coder
