"""
Ora Worker Agents — operational staff layer.

Each worker inherits from BaseWorkerAgent and handles one
specific job autonomously, reporting to a C-suite agent
and teaching Ora what it learns.
"""

from .base import BaseWorkerAgent

__all__ = ["BaseWorkerAgent"]
