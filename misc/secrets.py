"""
Single source of truth for credentials.

Historically API keys and broker passwords were pasted directly into a dozen
modules. They now live in one place -- common_files/parameters.json, which is
gitignored -- and every consumer reads them through the accessors below.

Resolution order for each secret:

    1. Environment variable  (lets you override without editing config,
                              and is how CI / containers should supply it)
    2. parameters.json       (the normal case for this desktop app)
    3. ""                    (never raises; callers get an empty string and
                              the underlying library reports the auth error)

Returning "" rather than raising is deliberate: importing this module must
never be able to take the Dash app down at startup.
"""

import os

try:
    from misc.load_parameters import parameters
except Exception:  # pragma: no cover - only hit if run outside the project
    try:
        from load_parameters import parameters
    except Exception:
        parameters = None


def _from_params(*path):
    """Walk parameters.json by key path, returning '' if anything is missing."""
    if parameters is None:
        return ""
    node = parameters.get_parameters() or {}
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
        if node is None:
            return ""
    return str(node) if node is not None else ""


def _resolve(env_var, *param_path):
    value = os.environ.get(env_var)
    if value:
        return value.strip()
    return _from_params(*param_path).strip()


def openai_api_key():
    """OpenAI key used by the LLM report, rolling verdict and online scan."""
    return _resolve("OPENAI_API_KEY", "openai", "api_key")


def hf_token():
    """HuggingFace token used by pyannote speaker diarization."""
    return _resolve("HF_TOKEN", "diarization", "hf_token")


def mqtt_password():
    """Password for the MQTT broker (local and PTT brokers share it)."""
    return _resolve("MQTT_PASSWORD", "mqtt", "password")


def mqtt_username():
    """Broker username. Not secret, but kept here so both live together."""
    return _resolve("MQTT_USERNAME", "mqtt", "username")
