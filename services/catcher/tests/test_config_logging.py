import logging

import pytest

from catcher.config import ConfigError
from catcher.logsetup import REDACTED, RedactingFilter
from tests.fakes import make_config


def test_config_requires_vars():
    with pytest.raises(ConfigError):
        make_config(MM_BOT_TOKEN="")
    with pytest.raises(ConfigError):
        make_config(CATCHER_ACTION_SECRET="short")
    cfg = make_config(OWNER_USER_IDS="a, b ,,c")
    assert cfg.owner_user_ids == {"a", "b", "c"}
    assert "mm-bot-token" not in repr(cfg) and "plane_api_key" not in repr(cfg)


def test_log_filter_redacts_secrets(caplog):
    cfg = make_config()
    flt = RedactingFilter(cfg.secret_values())
    logger = logging.getLogger("t")
    rec = logger.makeRecord(
        "t", logging.INFO, __file__, 1, "token %s and %s and ghs_%s", (cfg.mm_bot_token, cfg.plane_api_key, "a" * 36), None
    )
    flt.filter(rec)
    msg = rec.getMessage()
    assert cfg.mm_bot_token not in msg and cfg.plane_api_key not in msg and "ghs_aaaa" not in msg
    assert REDACTED in msg
    rec = logger.makeRecord("t", logging.INFO, __file__, 1, "oauth sk-ant-oat01-abcdefghijkl", (), None)
    flt.filter(rec)
    assert "sk-ant" not in rec.getMessage()
