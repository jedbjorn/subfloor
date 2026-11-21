"""Password verifier for the standalone manager's single operator account."""
import base64
import binascii
import hashlib
import hmac
import secrets
import threading

# As in the host sign-in service, bound concurrent scrypt working memory.
_hash_lock = threading.Lock()


def _derive(password, salt):
    with _hash_lock:
        return hashlib.scrypt(password, salt=salt, n=2**15, r=8, p=1, dklen=32,
                              maxmem=64 * 1024 * 1024)


def make_verifier(password):
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "hash": _derive(password.encode("utf-8"), salt).hex()}


def matches(authorization, verifier):
    if not verifier or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True)
        username, separator, password = decoded.partition(b":")
        if username != b"operator" or not separator or not 1 <= len(password) <= 1024:
            return False
        actual = _derive(password, bytes.fromhex(verifier["salt"]))
        return hmac.compare_digest(actual, bytes.fromhex(verifier["hash"]))
    except (KeyError, TypeError, ValueError, binascii.Error):
        return False
