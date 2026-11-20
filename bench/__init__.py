"""The v0.14 benchmark (docs/v0.14-plan.md H47-H51, §6.5; contract §13.11): what we measure about a bot's play, in the repository.

Measuring (no model):

``bench.loaders``    reads a saved game (``game.json``) and its trace (``trace-NNNN.jsonl``);
``bench.metrics``    pure functions over speeches / ledgers / traces: R (runtime health), S1 (form), S3 (homogeneity);
``bench.summary``    the same numbers over the rows of a ``results.jsonl``, per bot and for the whole run.

Situations and the runner (the real engine and the real agent loop; the model is the only thing that can be fake):

``bench.situation``  the YAML situation scripts: a world built with the real engine, stopped at a decision point;
``bench.play``       plays a script on ``World`` (steps = the intents a model's tool calls would be), finds what wakes the seat under test;
``bench.bots``       the bots to try: the v0.13.1 player (``LegacyBrain``), v0.14 bots (``BotBrain``) from the library / a directory / a snapshot, ablations;
``bench.runner``     bot × situation × sample -> the real ``Agent`` loop until the bot itself moves -> ``results.jsonl`` and ``judge_items.jsonl``;
``bench.fakes``      the scripted model of the smoke tests (CI): no network, no key;
``bench.realllm``    the real-model mode: off unless ``--real`` and ``--max-calls``; the app's provider table is never opened;
``bench.safety``     the real ``data/`` is never read or written by a run; a key never shows;
``bench.replay``     saved game -> situation script: the feasibility check of H47 and ``python -m bench replay --emit``;
``bench.cli`` / ``bench.__main__``  the command line (``python -m bench --help``).

A judge model is a DSH sub-agent (``bench/judges``), never the player model.  ``bench/fixtures`` must stay out of reach of every learning tool (H50):
nothing under ``app/`` reads or names this directory.
"""
