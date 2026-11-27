FROM debian:bookworm-slim@sha256:e67eeead736ff7bbe258ef088bff683635c561b1fcfefad0806c1df4ab49ce0e
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 stunnel4 tini tinyproxy-bin \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 outbound \
    && useradd --uid 10001 --gid outbound --no-create-home outbound \
    && useradd --uid 10002 --user-group --no-create-home inventory \
    && install -d -m 0700 -o outbound -g outbound /var/lib/outbound-proxy
WORKDIR /opt/outbound-proxy
COPY scripts/minipc_proxy_apply.py ./
COPY scripts/outbound_proxy ./outbound_proxy/
COPY --chmod=755 scripts/container-cli /usr/local/bin/outbound-proxy
ENV PYTHONPATH=/opt/outbound-proxy PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "outbound_proxy.container"]
