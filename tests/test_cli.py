import pytest

from app.cli import run_seed, run_validate
from app.crypto import decrypt
from app.database import Base, engine, session_scope
from app.models import Company


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def test_restaurant_industry_has_no_errors():
    findings = run_validate("restaurant")
    assert [f for f in findings if f.level == "ERROR"] == []


def test_seed_creates_company_with_encrypted_credentials():
    cid = run_seed("restaurant", slug="bistro",
                   channel_secret="s3cret", channel_token="t0ken")
    with session_scope() as db:
        c = db.get(Company, cid)
        assert c.slug == "bistro"
        assert c.system_prompt  # 不是空字串
        assert decrypt(c.line_channel_secret_enc) == "s3cret"


def test_seed_is_idempotent_on_same_slug():
    """重跑 seed 應該更新同一列,不是新增一列 —— 否則改個 FAQ 就多一家公司。"""
    a = run_seed("restaurant", slug="bistro", channel_secret="s", channel_token="t")
    b = run_seed("restaurant", slug="bistro", channel_secret="s", channel_token="t")
    assert a == b
    with session_scope() as db:
        assert db.query(Company).count() == 1


def test_seed_creates_separate_company_for_different_slug():
    """補強:上一條測試兩次呼叫的 slug 相同,就算 upsert 寫成「隨便抓現有的第一列」
    也會矇混過關(全表本來就只有一列)。這裡用兩個不同的 slug 各 seed 一次,
    確認 upsert 真的是以 slug 為鍵——用同一個 slug 才更新,不同 slug 要各自成列。
    """
    a = run_seed("restaurant", slug="bistro-a", channel_secret="s", channel_token="t")
    b = run_seed("restaurant", slug="bistro-b", channel_secret="s", channel_token="t")
    assert a != b
    with session_scope() as db:
        assert db.query(Company).count() == 2
