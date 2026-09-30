"""Rules shared across Environment Studio: the id form and the geometric
tolerances. They depend on one another, so they live together and are
checked when imported.

  TOLERANCE_M         an overlap up to this (1 mm) counts as touching (validation)
  EPSILON_M           floating-point slack when comparing against a tolerance
  SURFACE_GAP_M       what stands on a road floats this far above it: exactly
                      touching meshes make MuJoCo report a deep sideways overlap
  HFIELD_CLEARANCE_M  what stands on a height field keeps this above the sampled
                      ground (bilinear sampling can sit a few mm under MuJoCo's
                      triangles)
  CIRCLE_SEGMENTS     sides of the polygon standing for a circle (browser too)
"""

import re

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ID_FORM = "lower case letters, digits, - and _ (up to 64, starting with a letter or digit)"

TOLERANCE_M = 0.001
EPSILON_M = 1e-9
SURFACE_GAP_M = 0.0005
HFIELD_CLEARANCE_M = 0.005
CIRCLE_SEGMENTS = 32

# A gap is floating, never an overlap the checks would report; the clearance
# must cover more than the tolerance to hide sampling differences.
assert 0 < SURFACE_GAP_M < TOLERANCE_M, "objects on roads would count as overlapping"
assert HFIELD_CLEARANCE_M > TOLERANCE_M, "sampling differences would show as reaching into the ground"
assert EPSILON_M < TOLERANCE_M / 1000
