# Backward-compat shim — code moved to qa/evals/business/run.py
from qa.evals.business.run import *  # noqa: F401, F403
from qa.evals.business.run import main  # noqa: F401

if __name__ == "__main__":
    main()
