"""An interactive onboarding assistant. The engine is generic; a playbook describes one team.

A playbook (onboarding/playbooks/<name>/) is a YAML checklist plus an optional `pack.py` with the
probes (what is already done?) and actions (do it) that the checklist refers to. The engine decides
what is ready, asks for what is missing, and runs only registered actions, each after the operator's
yes. The model, when there is one, only steers the conversation.
"""
