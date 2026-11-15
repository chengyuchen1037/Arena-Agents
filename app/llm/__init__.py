"""Multi-protocol LLM layer (OpenAI Chat / OpenAI Responses / Anthropic Messages).

``app.engine.llm.LLMClient`` is the entry point for the engine; ``discovery``
backs the "fetch models" API. Importing this package stays cheap: adapters are
loaded from their own modules.
"""
