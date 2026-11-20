"""The v0.14 benchmark (docs/v0.14-plan.md H47-H51, §6.5; contract §13.11): what we measure about a bot's play, in the repository.

``bench.loaders``  reads a saved game (``game.json``) and its trace (``trace-NNNN.jsonl``);
``bench.metrics``  pure functions over speeches / ledgers / traces: R (runtime health), S1 (form), S3 (homogeneity);
``bench.__main__`` the command line (``python -m bench --help``).

Nothing here calls a model.  The suites that need one (the situations of L1-L4, S2, E2 ...) are driven by the runner of the
``bench`` stream; a judge model is a DSH sub-agent (``bench/judges``), never the player model.  ``bench/fixtures`` must stay out
of reach of every learning tool (H50): nothing under ``app/`` reads this directory.
"""
