"""Safe provider errors intended for terminal-facing output."""


class ProviderRequestError(RuntimeError):
    """A provider failed without exposing request bodies, headers, or credentials."""

