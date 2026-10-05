"""Live test against the real Renater partage server. Set RENATER_LIVE_TEST=1."""
import os
from pathlib import Path

import pytest

from src.config import load_config
from src.connector import MailConnector


@pytest.mark.skipif(
    os.environ.get("RENATER_LIVE_TEST") != "1",
    reason="set RENATER_LIVE_TEST=1 to run the live test",
)
def test_live_list_folders_contains_inbox():
    config = load_config(Path("config.yaml"))
    connector = MailConnector(config.accounts[0])
    folders = [folder.name for folder in connector.list_folders()]
    assert "INBOX" in folders
