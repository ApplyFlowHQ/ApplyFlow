"""Keep SMTP and IMAP logical hostnames for TLS while dialing validated IPs."""

import imaplib
import smtplib
import socket

from app.core.ssrf import validate_mail_host


def _connect(host, port, timeout):
    addresses = validate_mail_host(host, port)
    for index, address in enumerate(addresses):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((address, port))
            return sock
        except OSError:
            sock.close()
            if index + 1 == len(addresses):
                raise


class PublicSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _connect(host, port, timeout)


def _tls_socket(host, port, timeout, context):
    if not context.check_hostname:
        raise ValueError("Mail TLS requires hostname verification")
    sock = _connect(host, port, timeout)
    try:
        return context.wrap_socket(sock, server_hostname=host)
    except BaseException:
        sock.close()
        raise


class PublicSMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        return _tls_socket(host, port, timeout, self.context)


class PublicIMAP(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return _connect(self.host, self.port, timeout)


class PublicIMAPSSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        return _tls_socket(self.host, self.port, timeout, self.ssl_context)
