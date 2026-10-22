"""Versioned pHash distance policy; thresholds retain gate-specific meaning."""

PHASH_POLICY_VERSION = "visual-identity-v1"

# Conservatively confirms a recompressed/resized rendition of the same frame.
PHASH_NEAR_EXACT_MAX_DISTANCE = 2

# Broader candidate-duplicate rule; consumers still apply provenance/context gates.
PHASH_DUPLICATE_MAX_DISTANCE = 6
