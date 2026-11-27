"""The model gateway, cache, cassettes, scripted models, meters, and the four roles.

Three implementations of the ``Models`` port live here:

- ``Gateway`` (``pmfa.models.gateway``): live calls, cached, metered, recorded;
- ``CassettePlayer`` (``pmfa.models.cassette``): offline replay of recorded calls;
- ``ScriptedModels`` (``pmfa.models.scripted``): fixed answers for tests.

Importing this package loads neither LangChain nor CrewAI; they are imported
when a live call is first made.
"""

from pmfa.models.backend import AnthropicBackend, Backend, Completion, MissingApiKey
from pmfa.models.cassette import CassettePlayer, CassetteRecorder, load_cassettes
from pmfa.models.gateway import Gateway
from pmfa.models.pricing import assert_window_after_cutoffs, cost
from pmfa.models.scripted import ScriptedModels, ScriptExhausted

__all__ = [
    "AnthropicBackend",
    "Backend",
    "CassettePlayer",
    "CassetteRecorder",
    "Completion",
    "Gateway",
    "MissingApiKey",
    "ScriptExhausted",
    "ScriptedModels",
    "assert_window_after_cutoffs",
    "cost",
    "load_cassettes",
]
