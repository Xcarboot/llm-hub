import hashlib
import hmac
import secrets

API_KEY_PREFIX = "gw_"


def hash_api_key(raw_key: str, secret: str) -> str:
    return hmac.new(secret.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def generate_api_key(secret: str) -> tuple[str, str, str]:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    prefix = raw[:12]
    return raw, prefix, hash_api_key(raw, secret)


def verify_api_key(raw_key: str, expected_hash: str, secret: str) -> bool:
    if not raw_key.startswith(API_KEY_PREFIX):
        return False
    return hmac.compare_digest(hash_api_key(raw_key, secret), expected_hash)


def verify_admin_token(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def payload_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
