"""The bounds on how much of a filesystem the probe will look at.

Read through the module -- ``probe_limits.PROBE_MAX_VISITS`` rather than a
from-import -- so lowering one in a test or at runtime takes effect. Binding the
value at import time leaves each reader holding whatever it read first, which is
the trap the client-side caps module documents as well.
"""

from __future__ import annotations

# How far under a search root the probe will look, how many hits it will
# report, and how much of any one directory it will list. All three are bounds
# on a diagnostic that runs against a network volume of unknown size while a
# caller waits for the response.
PROBE_MAX_DEPTH = 4


PROBE_MAX_MATCHES = 20


PROBE_MAX_ENTRIES = 50


# Directory entries the model search will look at before giving up. This is
# the bound that survives a volume with no models in it at all.
PROBE_MAX_VISITS = 2000


PROBE_MAX_SNAPSHOTS = 5
