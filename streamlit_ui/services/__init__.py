"""
Streamlit UI Services Package

Provides the PenTestAgentService wrapper that enables dependency injection
for testing with mock objects while using real agent in production.
"""

from .agent_service import PenTestAgentService

__all__ = ['PenTestAgentService']
