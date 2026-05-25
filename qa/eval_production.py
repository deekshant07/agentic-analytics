# Backward-compat shim — code moved to qa/evals/shared/production.py
from qa.evals.shared.production import *  # noqa: F401, F403
from qa.evals.shared.production import (  # noqa: F401
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
)
