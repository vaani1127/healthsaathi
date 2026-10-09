"""Named feature column sets shared by the separability audit, the baselines and E2D.

BEHAVIOUR_COLUMNS are the observable behaviour features the raw-feature baselines (B0 to B4) use
and the separability audit checks: the SPEC 5.4 behaviour features plus the access's local hour,
weekday and whether it was refused. Explanation strength and forgery flags are not in this set.
"""

from e2d_core.features.behaviour import FEATURES as BEHAVIOUR_FEATURES

CONTEXT_COLUMNS = ("hour_local", "weekday", "refused")
BEHAVIOUR_COLUMNS = (*BEHAVIOUR_FEATURES, *CONTEXT_COLUMNS)
