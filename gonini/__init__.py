"""gonini — an order exception reconciliation agent for Fulfilment-as-a-Service.

A deterministic rules engine does all diffing and flagging; a fenced LLM layer
only classifies ambiguous prose, infers root causes, and drafts escalations —
which are written to /outbox for human approval, never sent.
"""

__version__ = "0.1.0"
