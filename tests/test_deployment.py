import pytest
from rental_agent.deployment import validate_environment


@pytest.fixture
def pilot(monkeypatch):
    from rental_agent.config import load_rules
    monkeypatch.setitem(load_rules()._data['booking'],'enforce_checkout_flow',True)
    settings={
        'APP_ENV':'production','DATABASE_URL':'postgresql+psycopg://localhost/test',
        'GEMINI_API_KEY':'test','WHATSAPP_PHONE_NUMBER_ID':'123','WHATSAPP_ACCESS_TOKEN':'test',
        'WHATSAPP_APP_SECRET':'test','WHATSAPP_VERIFY_TOKEN':'test','DOCUMENT_ENCRYPTION_KEY':'test',
        'WHATSAPP_DURABLE':'1','WHATSAPP_DOCUMENT_CHECKS':'auto','DOCUMENT_STORAGE':'local',
        'WHATSAPP_DOCUMENT_DIR':'/data/documents','DOCUMENT_RETENTION_DAYS':'30',
        'PAYMENT_PROVIDER':'stripe','STRIPE_API_KEY':'sk_test_fake','STRIPE_WEBHOOK_SECRET':'whsec_fake',
        'PAYMENT_SUCCESS_URL':'https://example.test/paid','PAYMENT_CANCEL_URL':'https://example.test/cancel'}
    for k,v in settings.items():monkeypatch.setenv(k,v)
    return monkeypatch


def test_valid_pilot_settings(pilot):validate_environment()


@pytest.mark.parametrize('key,value',[
    ('DATABASE_URL','sqlite:///demo.db'),('WHATSAPP_DURABLE','0'),
    ('WHATSAPP_DOCUMENT_CHECKS','staff'),('GEMINI_API_KEY',''),
    ('STRIPE_API_KEY','sk_live_fake'),('STRIPE_WEBHOOK_SECRET',''),
    ('PAYMENT_SUCCESS_URL','http://example.test'),('DOCUMENT_RETENTION_DAYS','0'),
    ('WHATSAPP_DOCUMENT_DIR','')])
def test_bad_pilot_settings_fail_before_serving(pilot,key,value):
    pilot.setenv(key,value)
    with pytest.raises(RuntimeError):validate_environment()
