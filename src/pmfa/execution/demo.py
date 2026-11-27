"""The demo executor: real orders on Kalshi's demo exchange, mock funds only.

It lives with the other Kalshi adapters because it speaks the exchange's wire
format; it is re-exported here because it is one of the two executors.
"""

from pmfa.world.kalshi.demo import DemoExecutor

__all__ = ["DemoExecutor"]
