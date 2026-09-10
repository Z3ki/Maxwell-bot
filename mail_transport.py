"""TLS and connection handling shared by mail tools and polling."""

import contextlib
import imaplib
import ipaddress
import ssl


def mail_ssl_context(host: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    hostname = str(host).strip().lower().rstrip(".").strip("[]")
    local = hostname in {"localhost", "host.docker.internal"}
    try:
        local = local or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    # The bundled local MTA uses a self-signed certificate. Remote endpoints
    # must still authenticate their server before receiving mailbox credentials.
    if local:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def connect_imap(host: str, port: int, user: str, password: str):
    connection = imaplib.IMAP4_SSL(
        host, port, ssl_context=mail_ssl_context(host), timeout=60
    )
    try:
        connection.login(user, password)
    except BaseException:
        with contextlib.suppress(Exception):
            connection.shutdown()
        raise
    return connection
