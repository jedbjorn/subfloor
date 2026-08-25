"""Test-only signer for the DSH preparation provenance contract.

This deterministic private key is executable fixture material, not product
authority.  Production issuance, key custody, rotation, and domain lifecycle
belong to task 649 and must never import this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ISSUER_CONTRACT = "sc-dsh-prototype-issuer-key-v1"
ALGORITHM = "rsa-pkcs1v15-sha256"
PUBLIC_EXPONENT = 65_537
MODULUS = int(
    "eb4a33473bff4220b26dbc18d6de30294113f30f7f2da4d478e4f56045b0be38"
    "92cf37b4c2c2640a3790baa0bdf205fbe99283aafde010dfc75f629aefbeab67"
    "8dd46cbb08f463e6e21c6b83018fe9837d59351e2640a45466a992f5688febc9"
    "e1719a2c5712bc872ac049b97b6841bfc7054503e54bc61f60d2c1bc4efe6f85"
    "1ba2eaba3928e186ea5b30a1ee93c2a688880b0c540f901cea976d47cb78f564"
    "e3f75cb68178be0c17d4d0033394dbf453fed62e7d02d1f559f15509d8053abd"
    "9d7316252dc95b6519bd2aa97eb6b0798f1f94dd1685ebe1b89f5a1909c00c69"
    "84fc670613d9891eba87c1cfd7726c13a7bab857123735e471a332525352660d",
    16,
)
PRIVATE_EXPONENT = int(
    "1903bcfae44a8985bf62823e63cda0722a5c8c19482c9a9b0a35514f0869b7"
    "777d48318472b646fb7d17d277976a1d2fc08fd696bdc1ee1954717422c3bd"
    "522cc2bbe4496834cf503316d1694ea7b5ac488dcce3652eb729cff6544ce9e"
    "e6f2379e7e17bb8502221feae0dc87df1c217b8f97af26494cf3df3c5c45a"
    "184ddd9e25726c158f671f0f394ed566f516611feb6ba3b3659d863beb4a2b"
    "3c69cc5d5143083d3ade06ac0c6eac0e3b5761e1e900f94cf2bbf97a37b65"
    "362b896a74ad02fd6cb966aff2147e68821be64fcf3bf4c18a317e2e3a37d"
    "037e621defff72b4daef947c18213c0edc04ca05241f95d0860719f1ecba4e"
    "0ea7489947cf33b023",
    16,
)
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _key_bytes() -> bytes:
    return MODULUS.to_bytes((MODULUS.bit_length() + 7) // 8, "big")


KEY_ID = hashlib.sha256(
    _key_bytes() + PUBLIC_EXPONENT.to_bytes(4, "big")
).hexdigest()


def public_key_record() -> dict[str, object]:
    return {
        "contract": ISSUER_CONTRACT,
        "key_id": KEY_ID,
        "algorithm": ALGORITHM,
        "modulus_hex": format(MODULUS, "x"),
        "public_exponent": PUBLIC_EXPONENT,
    }


def write_public_key(path: Path) -> None:
    path.write_text(json.dumps(public_key_record(), sort_keys=True))
    path.chmod(0o600)


def _encoded_digest(payload: bytes) -> bytes:
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    width = len(_key_bytes())
    return b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info


def sign_descriptor(fields: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    encoded = _encoded_digest(payload)
    signature = pow(
        int.from_bytes(encoded, "big"),
        PRIVATE_EXPONENT,
        MODULUS,
    ).to_bytes(len(_key_bytes()), "big")
    return {**fields, "signature": signature.hex()}
