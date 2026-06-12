"""Centralized constants for Jack's deterministic gates."""

COGNITIVE_MIRROR_THRESHOLD_BITS = 4.0
COGNITIVE_MIRROR_GRACE_TOKENS = 5
COGNITIVE_MIRROR_MASS_FLOOR = 0.5

STREAMING_IRQ_WINDOW_SIZE = 256
GHOST_LEDGER_CHUNK_SIZE = 8192
GHOST_LEDGER_OVERLAP = 256

import re
ARITHMETIC_PATTERN = re.compile(r'(?:calculate|compute|evaluate|what\s+is|solve)\s+(?P<expr>[0-9\s+\-*/().%^]+)', re.IGNORECASE)
