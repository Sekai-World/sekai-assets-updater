FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        procps \
        systemd \
        systemd-sysv \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pm2@7.0.4

COPY pm2-fixture.service /etc/systemd/system/pm2-fixture.service
COPY ecosystem.config.cjs webhook.js worker.js updater.js mock-logrotate.js run-inside.sh /opt/handover-test/

RUN chmod 0755 /opt/handover-test/run-inside.sh \
    && ln -s /opt/handover-test/run-inside.sh /usr/local/sbin/run-handover-test

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
