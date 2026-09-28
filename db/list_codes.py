"""Item-code generation for list membership (pure, no DB access).

Codes use an alphabet without visually-confusable characters (no 0/O, 1/I/L),
6 characters long — ~700 million combinations per list. Codes are stored and
compared upper-case; lookups normalize user input.
"""
import secrets

ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
LENGTH = 6


def generate_item_code() -> str:
    return ''.join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def normalize_item_code(code: str) -> str:
    return code.strip().upper()
