"""llm-message-gateway: an LLM classifies inbound messages; Python enforces every rule.

The package is deliberately import-light: nothing here reads the environment,
opens a network connection or loads the prompt at import time. Runtime wiring
lives in `message_gateway.app`.
"""

__version__ = "0.1.0"
