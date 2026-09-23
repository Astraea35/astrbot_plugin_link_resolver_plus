"""Generate the WebSign parameters required by Douyin detail requests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from time import time
from urllib.parse import quote, unquote

WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
WEB_SIGNATURE_PARAM = "x-secsdk-web-signature"


@dataclass(frozen=True, slots=True)
class SignedQuery:
    query: str
    signature: str
    timestamp: str


def encode_pairs(pairs: list[tuple[str, str]]) -> str:
    return "&".join(
        f"{quote(name, safe='*-._')}={quote(value, safe='*-._')}"
        for name, value in pairs
    )


def sign(query: str, uifid: str, *, timestamp: int | None = None) -> SignedQuery:
    stamp = str(int(time() if timestamp is None else timestamp))
    pairs = []
    for part in query.split("&"):
        if part:
            name, _, value = part.partition("=")
            pairs.append((unquote(name), unquote(value)))
    if not any(name == "uifid" for name, _ in pairs):
        pairs.append(("uifid", uifid))
    pairs.append(("timestamp", stamp))
    covered_query = encode_pairs(pairs)
    signature = hashlib.md5(
        f"{uifid}_{stamp}_{WEB_SIGN_SALT}_{covered_query}".encode()
    ).hexdigest()
    return SignedQuery(
        query=f"{covered_query}&{WEB_SIGNATURE_PARAM}={signature}",
        signature=signature,
        timestamp=stamp,
    )
