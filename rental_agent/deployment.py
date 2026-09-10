"""Fail-fast checks for the hosted synthetic pilot."""
import os


def validate_environment():
    if os.getenv('APP_ENV') != 'production': return
    from .config import load_rules
    if load_rules().get('booking',{}).get('enforce_checkout_flow',True) is not True:
        raise RuntimeError('Production requires the enforced checkout flow')
    required=['DATABASE_URL','GEMINI_API_KEY','WHATSAPP_PHONE_NUMBER_ID','WHATSAPP_ACCESS_TOKEN',
              'WHATSAPP_APP_SECRET','WHATSAPP_VERIFY_TOKEN','DOCUMENT_ENCRYPTION_KEY']
    missing=[name for name in required if not os.getenv(name)]
    if missing: raise RuntimeError('Missing deployment settings: '+', '.join(missing))
    if not os.environ['DATABASE_URL'].startswith(('postgresql://','postgresql+psycopg://')):
        raise RuntimeError('Production requires PostgreSQL')
    if os.getenv('WHATSAPP_DURABLE')!='1': raise RuntimeError('Enable WHATSAPP_DURABLE')
    if os.getenv('WHATSAPP_DOCUMENT_CHECKS')!='auto': raise RuntimeError('Enable automatic document checks')
    if os.getenv('PAYMENT_PROVIDER','simulated')=='stripe':
        if not os.getenv('STRIPE_API_KEY','').startswith(('sk_test_','rk_test_')):
            raise RuntimeError('This pilot accepts Stripe test keys only')
        if not os.getenv('STRIPE_WEBHOOK_SECRET'): raise RuntimeError('Set STRIPE_WEBHOOK_SECRET')
        for key in ['PAYMENT_SUCCESS_URL','PAYMENT_CANCEL_URL']:
            if not os.getenv(key,'').startswith('https://'): raise RuntimeError('Set an HTTPS '+key)
    if int(os.getenv('DOCUMENT_RETENTION_DAYS','30'))<1:
        raise RuntimeError('Retention must be positive')
    if os.getenv('DOCUMENT_STORAGE','local')=='local' and not os.getenv('WHATSAPP_DOCUMENT_DIR'):
        raise RuntimeError('Set WHATSAPP_DOCUMENT_DIR to a shared persistent volume')
