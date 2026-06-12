from __future__ import annotations

import re
import socket
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
import http.client
import ssl
from typing import Any

try:
    from markdownify import markdownify
except ImportError:
    markdownify = None


class DNSRebindingSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, safety_checker):
        super().__init__()
        self.safety_checker = safety_checker

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        is_safe, ip, hostname = self.safety_checker(newurl)
        if not is_safe:
            raise urllib.error.HTTPError(newurl, code, "SSRF Redirect Blocked by Chassis Firewall", headers, fp)
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req:
            new_req._jack_resolved_ip = ip
            new_req._jack_resolved_hostname = hostname
        return new_req


class DNSRebindingSafeHTTPSHandler(urllib.request.HTTPSHandler):
    """Custom HTTPS Handler that binds the socket directly to a pre-verified IP.
    
    This prevents DNS Rebinding attacks by bypassing any subsequent DNS lookups
    at connection time, while preserving the original hostname for SSL SNI
    and Host header validation.
    """
    def __init__(self, ip_address: str, hostname: str, context: ssl.SSLContext | None = None):
        super().__init__(context=context)
        self.ip_address = ip_address
        self.hostname = hostname

    def https_open(self, req: urllib.request.Request) -> Any:
        ip = getattr(req, "_jack_resolved_ip", self.ip_address)
        hostname = getattr(req, "_jack_resolved_hostname", self.hostname)
        req.add_unredirected_header("Host", hostname)

        class BoundHTTPSConnection(http.client.HTTPSConnection):
            # FIX: Consume 'host' so it doesn't bleed into *args as the port
            def __init__(self, host: str, *args: Any, **kwargs: Any) -> None:
                super().__init__(ip, *args, **kwargs)
                self._server_hostname = hostname  # Enforce original hostname for SNI check

        req.host = hostname
        return self.do_open(BoundHTTPSConnection, req)


class DNSRebindingSafeHTTPHandler(urllib.request.HTTPHandler):
    """Custom HTTP Handler that binds the socket directly to a pre-verified IP.
    
    This prevents DNS Rebinding attacks by bypassing subsequent DNS lookup handshakes.
    """
    def __init__(self, ip_address: str, hostname: str):
        super().__init__()
        self.ip_address = ip_address
        self.hostname = hostname

    def http_open(self, req: urllib.request.Request) -> Any:
        ip = getattr(req, "_jack_resolved_ip", self.ip_address)
        hostname = getattr(req, "_jack_resolved_hostname", self.hostname)
        req.add_unredirected_header("Host", hostname)

        class BoundHTTPConnection(http.client.HTTPConnection):
            # FIX: Consume 'host'
            def __init__(self, host: str, *args: Any, **kwargs: Any) -> None:
                super().__init__(ip, *args, **kwargs)

        req.host = hostname
        return self.do_open(BoundHTTPConnection, req)


class WebNavigator:
    """Deterministic tool for fetching and converting web pages to Markdown."""

    def __init__(self, timeout: int = 15, *args: Any, **kwargs: Any) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": "Jack-Autonomous-Chassis/1.0"}

    def _is_safe_host(self, url: str) -> tuple[bool, str | None, str | None]:
        """Deterministically verify URL safety and resolve hostname to prevent DNS rebinding."""
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False, None, None
            
            # Resolve hostname exactly once
            ip_str = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(ip_str)
            
            # SSRF Protection: Check against forbidden local/private ranges
            if ip.is_loopback or ip.is_private or ip.is_multicast or ip.is_link_local or ip.is_reserved:
                return False, None, None
                
            # Explicitly block AWS metadata IP
            if str(ip) == "169.254.169.254":
                return False, None, None
                
            return True, ip_str, hostname
        except Exception:
            return False, None, None

    def navigate(self, url: str, *args: Any, **kwargs: Any) -> str:
        """Fetch a URL and return its content as clean Markdown using bound handlers."""
        if not url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://"
            
        is_safe, ip_address, hostname = self._is_safe_host(url)
        if not is_safe or not ip_address or not hostname:
            return "Error: SSRF Protection blocked access to local, private, or reserved network address."

        req = urllib.request.Request(url, headers=self.headers)
        
        # Enforce strict certificate verification
        ssl_context = ssl.create_default_context()
        
        # Build opener with custom bound handlers to prevent DNS Rebinding.
        # Register both safety handlers to completely close the cross-scheme redirect bypass.
        opener = urllib.request.build_opener(
            DNSRebindingSafeRedirectHandler(self._is_safe_host),
            DNSRebindingSafeHTTPSHandler(ip_address, hostname, context=ssl_context),
            DNSRebindingSafeHTTPHandler(ip_address, hostname)
        )

        try:
            with opener.open(req, timeout=self.timeout) as response:
                html_content = response.read().decode("utf-8", errors="ignore")

            if markdownify:
                md_content = markdownify(html_content, heading_style="ATX")
                return re.sub(r"\n{3,}", "\n\n", md_content).strip()
            else:
                text = re.sub(r"<[^>]+>", " ", html_content)
                return " ".join(text.split())

        except urllib.error.URLError as exc:
            return f"Failed to navigate to {url}: {exc}"
        except Exception as exc:
            return f"Unexpected error while navigating: {exc}"