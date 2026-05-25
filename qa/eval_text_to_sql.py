# Backward-compat shim — code moved to qa/evals/sql/run.py
from qa.evals.sql.run import *  # noqa: F401, F403
from qa.evals.sql.run import TextToSQLCase, compile_canonical, main  # noqa: F401

if __name__ == "__main__":
    main()
