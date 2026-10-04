"""File access. Depends on envguard.core, never on envguard.cli."""

from envguard.io.reader import EnvFileError, load_env, read_text

__all__ = ["EnvFileError", "load_env", "read_text"]
