#!/usr/bin/env python3
"""Generate a TOTP code from a base32 secret. Stdlib only."""
import base64
import hashlib
import hmac
import struct
import sys
import time


def totp(secret: str, digits: int = 6, period: int = 30, algo: str = "sha1") -> str:
    key = base64.b32decode(secret.replace(" ", "").upper(), casefold=True)
    counter = struct.pack(">Q", int(time.time() // period))
    digest = hmac.new(key, counter, getattr(hashlib, algo)).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


if __name__ == "__main__":
    secret = sys.stdin.read().strip() if len(sys.argv) < 2 else sys.argv[1]
    if not secret:
        sys.exit("error: empty TOTP secret")
    print(totp(secret))
