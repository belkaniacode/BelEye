"""Sofia/DVRIP password hashing.

The Xiongmai firmware does not transmit raw MD5. It takes the 16-byte
MD5 digest of the password, sums each consecutive pair of bytes modulo
62, and indexes into a 62-character alphabet. The result is the
8-character "encrypted password" expected by the LOGIN_REQ2 message.

Empty password → ``""`` (special case: many firmwares reject the hash
of an empty string and require it sent literally as ``""``).
"""

from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)

_ALPHABET = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
)
assert len(_ALPHABET) == 62


def sofia_hash(password: str) -> str:
    """Return the 8-character Sofia-encoded password hash.

    Empty password hashes to ``"tlJwpbo6"`` — the well-known constant
    used by Xiongmai firmwares when no password is set.
    """
    digest = hashlib.md5(password.encode("utf-8")).digest()
    out = []
    for i in range(0, 16, 2):
        s = (digest[i] + digest[i + 1]) % 62
        out.append(_ALPHABET[s])
    hashed = "".join(out)
    log.debug("[DVRIP] sofia_hash(len=%d) -> %s", len(password), hashed)
    return hashed
