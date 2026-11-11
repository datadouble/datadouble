# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Parsing adapter settings from the spec.

The spec passes adapter-specific settings (``source.connection``, each entry of
``store``) through untouched; the adapter parses them here, at its boundary,
into its own strict model.
"""

import json
from collections.abc import Mapping

from pydantic import BaseModel, JsonValue, ValidationError


class SettingsError(Exception):
    """Adapter settings that do not validate."""


def parse_settings[M: BaseModel](model: type[M], settings: Mapping[str, JsonValue], what: str) -> M:
    """Validate ``settings`` against ``model``; ``what`` names them in errors."""
    try:
        return model.model_validate_json(json.dumps(dict(settings)))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in problem['loc']) or '(settings)'}: {problem['msg']}"
            for problem in error.errors(include_url=False)
        )
        message = f"{what} settings are invalid: {problems}"
        raise SettingsError(message) from None
