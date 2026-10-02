import logging

import pytest


@pytest.fixture
def caplog(caplog):
    """Our "incident" logger doesn't propagate to root, so attach pytest's handler directly."""
    logger = logging.getLogger("incident")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)
