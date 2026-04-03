"""Pydoll HTTP serve module — exposes Pydoll as a remote execution engine."""

from pydoll.serve.server import create_app, run_server

__all__ = ['create_app', 'run_server']
