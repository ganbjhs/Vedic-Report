"""Keep the scraper API key out of the database in plain text.

`seal(secret, text)` → "v1:<nonce>:<ciphertext>:<tag>", all base64;
`open_(secret, sealed)` → text, or raises ValueError when the secret is wrong
or the value was tampered with.

Standard library only (hashlib / hmac / secrets), so neither app grows a
dependency for one field. The construction is textbook and boring on purpose:
a per-value random nonce, a keystream derived with HMAC-SHA256 as the PRF
(counter mode), the plaintext XORed with it, and an HMAC-SHA256 tag over
nonce + ciphertext (encrypt-then-MAC) with a separately derived key. If the
project ever adopts `cryptography`, swapping this for `Fernet` is a one-file
change — the stored format is versioned (`v1:`) for exactly that.

The secret is `PORTAL_KEY_SECRET` in `.env`, shared by the internal app (which
seals, in Admin → Clients) and the portal (which opens, to call the scraper).
"""
import base64
import hashlib
import hmac
import secrets

_VERSION = "v1"


def _keys(secret: str):
    if not secret:
        raise ValueError("PORTAL_KEY_SECRET is not set")
    root = hashlib.sha256(secret.encode("utf-8")).digest()
    enc = hmac.new(root, b"portal-secretbox-enc", hashlib.sha256).digest()
    mac = hmac.new(root, b"portal-secretbox-mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def seal(secret: str, text: str) -> str:
    enc_key, mac_key = _keys(secret)
    nonce = secrets.token_bytes(16)
    data = (text or "").encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return f"{_VERSION}:{_b64(nonce)}:{_b64(ct)}:{_b64(tag)}"


def open_(secret: str, sealed: str) -> str:
    if not sealed:
        return ""
    try:
        version, n, c, t = sealed.split(":", 3)
    except ValueError:
        raise ValueError("not a sealed value")
    if version != _VERSION:
        raise ValueError(f"unknown secretbox version {version!r}")
    enc_key, mac_key = _keys(secret)
    nonce, ct, tag = _unb64(n), _unb64(c), _unb64(t)
    expect = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, tag):
        raise ValueError("wrong PORTAL_KEY_SECRET, or the value was altered")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode("utf-8")


def is_sealed(value: str) -> bool:
    return bool(value) and value.startswith(_VERSION + ":") and value.count(":") == 3
