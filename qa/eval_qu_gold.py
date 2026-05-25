# Backward-compat shim — code moved to qa/evals/orchestrator/run.py
from qa.evals.orchestrator.run import *  # noqa: F401, F403
from qa.evals.orchestrator.run import run_qu_gold_eval, main  # noqa: F401

if __name__ == "__main__":
    main()
