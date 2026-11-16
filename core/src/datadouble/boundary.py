# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The production boundary: the mark a machine needs to read production data.

Reading a spec's source and promoting an image happen only on machines whose
operators have marked them as inside the production boundary, by setting
``DATADOUBLE_BOUNDARY=inside`` in the runner's environment (decision 98). A
machine nobody marked, such as a CI vendor's hosted runner, is refused before
anything connects. A machine a CI/CD adapter recognizes as its vendor's
hosted runner is refused even when marked (decision 19).
"""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datadouble.registry import Registry

VARIABLE = "DATADOUBLE_BOUNDARY"
INSIDE = "inside"


class BoundaryError(Exception):
    """An operation that needs the production boundary, on a machine not marked inside it."""


def require_inside(action: str, registry: "Registry") -> None:
    """Fail unless this machine is marked as inside the production boundary.

    Every installed CI/CD adapter is asked whether the machine is its vendor's
    hosted runner, whichever system the request names.
    """
    if os.environ.get(VARIABLE) != INSIDE:
        message = (
            f"{action} runs only on machines marked as inside the production boundary; once"
            f" this one is, set {VARIABLE}={INSIDE} in its environment (decision 98)"
        )
        raise BoundaryError(message)
    for name in sorted(registry.ci):
        hosted = registry.ci_system(name).vendor_hosted(os.environ)
        if hosted is not None:
            message = (
                f"{action} never runs on a CI vendor's hosted runner, and this is {hosted};"
                " run it on a runner inside the production boundary (decision 19)"
            )
            raise BoundaryError(message)
