"""Durable local project worker; load the optional SDK worker only on demand."""
__all__ = ['LocalTaskRunner']

def __getattr__(name):
    if name == 'LocalTaskRunner':
        from .runner import LocalTaskRunner
        return LocalTaskRunner
    raise AttributeError(name)
