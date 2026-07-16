FROM alpine:3.24.1

RUN apk add --no-cache openssh-server tini \
    && addgroup -g 10001 relay \
    && adduser -D -u 10001 -G relay -s /bin/sh relay \
    && passwd -d relay \
    && install -d -m 0755 /run/sshd /var/empty

COPY ops/home-relay/sshd_config /etc/ssh/sshd_config
COPY ops/home-relay/entrypoint.sh /usr/local/bin/home-relay-entrypoint

RUN chmod 0755 /usr/local/bin/home-relay-entrypoint

EXPOSE 2222

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/home-relay-entrypoint"]
