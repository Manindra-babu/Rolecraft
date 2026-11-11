import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "trk",
    "trkInfo",
    "ref",
    "referrer",
    "source",
}

BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}


class SSRFError(ValueError):
    """Raised when a URL fails SSRF validation."""


def normalize_job_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    kept_params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k not in TRACKING_QUERY_PARAMS and not k.startswith("utm_")
    ]
    cleaned_query = urlencode(sorted(kept_params))
    cleaned_path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, cleaned_path, "", cleaned_query, ""))


def _is_ip_blocked(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def validate_public_url(url: str) -> str:
    """Validate scheme, hostname, and resolved IP addresses to prevent SSRF."""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SSRFError("Only http and https URLs are allowed.")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise SSRFError("URL must include a valid hostname.")
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal", ".localhost")):
        raise SSRFError("Internal or local network addresses are not allowed.")

    # Direct IP literal check
    try:
        ipaddress.ip_address(hostname)
        if _is_ip_blocked(hostname):
            raise SSRFError("Private or non-routable IP addresses are not allowed.")
        return cleaned
    except ValueError:
        pass

    # DNS resolution check
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFError(f"Could not resolve hostname: {hostname}") from exc

    resolved_ips = {info[4][0] for info in addr_info if info and info[4]}
    if not resolved_ips:
        raise SSRFError(f"No IP addresses resolved for {hostname}")
    for ip_str in resolved_ips:
        if _is_ip_blocked(ip_str):
            raise SSRFError("URL resolves to a private or restricted IP range.")

    return cleaned


def safe_fetch_url(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    max_bytes: int = 2 * 1024 * 1024,
    max_redirects: int = 5,
    headers: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    """
    Fetch a URL safely with SSRF validation on the initial target and every redirect hop,
    strict timeouts, and a response byte cap.
    Returns (final_url, response_text, status_code).
    """
    current_url = validate_public_url(url)
    req_headers = {
        "User-Agent": "ResumeIQBot/1.0 (+https://resumeiq.local/bot)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    if headers:
        req_headers.update(headers)

    with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            validate_public_url(current_url)
            with client.stream("GET", current_url, headers=req_headers) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        raise SSRFError("Redirect missing Location header.")
                    current_url = validate_public_url(urljoin(current_url, location))
                    continue

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Response exceeded maximum allowed size (2 MB).")
                    chunks.append(chunk)
                raw_bytes = b"".join(chunks)
                encoding = resp.encoding or "utf-8"
                return current_url, raw_bytes.decode(encoding, errors="replace"), resp.status_code

    raise SSRFError("Too many redirects.")
