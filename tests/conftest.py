import os
from collections.abc import Iterator

import pytest
from botocore.session import Session


@pytest.fixture(scope="session")
def aws_session() -> Iterator[Session]:
    """A botocore session blind to the developer's own AWS configuration."""
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("AWS_")]:
            patch.delenv(name)
        patch.setenv("AWS_CONFIG_FILE", os.devnull)
        patch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
        session = Session()
        session.set_credentials("AKIDFAKES3", "fakes3-secret")
        yield session
