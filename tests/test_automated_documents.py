import io
from datetime import date, timedelta

import pytest
from PIL import Image
from cryptography.fernet import Fernet
from sqlalchemy import select

from rental_agent.services.document_checks import DocumentChecker, ExtractedDocument, purge_expired
from rental_agent.store.models import CustomerDocument, DocumentCheck
from rental_agent.whatsapp.storage import DiskBackend, EncryptedStore, S3Backend
from rental_agent.whatsapp.media import MediaError
from tests.test_document_collection import PDF


class Reader:
    model='synthetic-reader'
    def __init__(self, **changes):
        self.values=dict(document_type='passport',readable=True,full_name='Sample Driver',
                         expiry_date=date(2035,1,1),confidence=.98)
        self.values.update(changes); self.calls=0
    def extract(self,*_):
        self.calls+=1
        return ExtractedDocument(**self.values)


@pytest.fixture
def store(tmp_path): return EncryptedStore(DiskBackend(tmp_path/'private'),Fernet.generate_key().decode())


def receipt(ctx,store,data=PDF,mime='application/pdf',suffix='1'):
    row=CustomerDocument(document_id='DOC-'+suffix,provider_message_id='wamid.'+suffix,
        customer_id=ctx.customer_id,conversation_id=ctx.conversation_id,
        created_at=ctx.now(),status='pending_review',storage_key=store.save(data,mime),mime_type=mime)
    ctx.session.add(row);ctx.session.flush();return row


@pytest.mark.parametrize('changes,issue',[
    ({'readable':False},'unreadable'),
    ({'confidence':.5},'uncertain'),
    ({'expiry_date':date(2020,1,1)},'expired'),
    ({'expiry_date':None},'missing_expiry'),
    ({'full_name':''},'missing_name'),
    ({'document_type':'unknown'},'unsupported_document'),
])
def test_uncertain_documents_never_pass(booking_ctx,store,changes,issue):
    row=receipt(booking_ctx,store)
    result=DocumentChecker(Reader(**changes),match_key='test-only').check(booking_ctx,row,store)
    assert issue in result.issues and row.status=='needs_replacement'
    assert not booking_ctx.customers.get(booking_ctx.customer_id).documents_on_file


def test_matching_names_pass_without_identity_data_in_database(booking_ctx,store):
    checker=DocumentChecker(Reader(),match_key='test-only')
    row=receipt(booking_ctx,store); result=checker.check(booking_ctx,row,store)
    assert row.status=='checks_passed' and not result.issues
    checker.reader=Reader(full_name='SAMPLE  DRIVER',document_type='driving_license')
    row2=receipt(booking_ctx,store,suffix='2');checker.check(booking_ctx,row2,store)
    assert row2.status=='checks_passed'
    assert 'Sample' not in result.name_digest
    assert booking_ctx.customers.get(booking_ctx.customer_id).documents_on_file==[]


def test_mismatched_names_request_replacement(booking_ctx,store):
    checker=DocumentChecker(Reader(),match_key='test-only')
    checker.check(booking_ctx,receipt(booking_ctx,store),store)
    checker.reader=Reader(full_name='Different Driver')
    row=receipt(booking_ctx,store,suffix='2')
    result=checker.check(booking_ctx,row,store)
    assert result.issues==['name_mismatch'] and row.status=='needs_replacement'


def test_arabic_names_compare_without_contact_display_name(booking_ctx,store):
    checker=DocumentChecker(Reader(full_name='سائق تجريبي'),match_key='test-only')
    checker.check(booking_ctx,receipt(booking_ctx,store),store)
    row=receipt(booking_ctx,store,suffix='2')
    assert checker.check(booking_ctx,row,store).issues==[]


def test_expiry_today_is_not_expired(booking_ctx,store):
    checker=DocumentChecker(Reader(expiry_date=booking_ctx.now().date()),match_key='test-only')
    assert checker.check(booking_ctx,receipt(booking_ctx,store),store).issues==[]


def test_small_image_rejected_before_model(booking_ctx,store):
    data=io.BytesIO();Image.new('RGB',(100,100),'white').save(data,format='PNG')
    reader=Reader();row=receipt(booking_ctx,store,data.getvalue(),'image/png')
    result=DocumentChecker(reader,match_key='test-only').check(booking_ctx,row,store)
    assert result.issues==['image_too_small'] and reader.calls==0


def test_repeated_check_does_not_call_provider_twice(booking_ctx,store):
    reader=Reader();checker=DocumentChecker(reader,match_key='test-only')
    row=receipt(booking_ctx,store);checker.check(booking_ctx,row,store)
    booking_ctx.session.flush();checker.check(booking_ctx,row,store)
    assert reader.calls==1


def test_encrypted_disk_and_tamper_detection(store):
    key=store.save(PDF,'application/pdf');path=store.backend.store.root/key
    assert PDF not in path.read_bytes() and store.read(key)==PDF
    content=bytearray(path.read_bytes());content[-10]^=1;path.write_bytes(content)
    with pytest.raises(MediaError):store.read(key)
    with pytest.raises(MediaError):store.read('../escape.pdf')


def test_retention_removes_bytes_and_name_digest(booking_ctx,store):
    row=receipt(booking_ctx,store);key=row.storage_key
    DocumentChecker(Reader(),match_key='test-only').check(booking_ctx,row,store)
    row.created_at=booking_ctx.now()-timedelta(days=31)
    booking_ctx.session.flush()
    assert purge_expired(booking_ctx,store,30)==1
    assert row.status=='deleted' and row.storage_key is None
    assert booking_ctx.session.get(DocumentCheck,row.document_id).name_digest is None
    with pytest.raises(FileNotFoundError):store.read(key)
    assert purge_expired(booking_ctx,store,30)==0


class FakeS3:
    def __init__(self):self.objects={};self.last=None
    def put_object(self,**kw):self.last=kw;self.objects[kw['Key']]=kw['Body']
    def get_object(self,**kw):return {'Body':io.BytesIO(self.objects[kw['Key']])}
    def delete_object(self,**kw):self.objects.pop(kw['Key'],None)


def test_s3_stores_only_ciphertext_and_no_public_acl():
    s3=FakeS3();store=EncryptedStore(S3Backend('private-test',s3),Fernet.generate_key().decode())
    key=store.save(PDF,'application/pdf')
    assert s3.last['ServerSideEncryption']=='AES256' and 'ACL' not in s3.last
    assert PDF not in s3.last['Body'] and store.read(key)==PDF
    store.delete(key);assert s3.objects=={}


def test_gemini_adapter_uses_bounded_structured_extraction():
    from types import SimpleNamespace
    from rental_agent.services.document_checks import GeminiDocumentReader
    captured=[]
    def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(text='{"document_type":"passport","readable":true,"full_name":"Sample Driver","expiry_date":"2035-01-01","confidence":0.98}')
    reader=GeminiDocumentReader(SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    result=reader.extract(PDF,'application/pdf')
    assert result.full_name=='Sample Driver'
    schema = captured[0]['config'].response_schema
    assert 'additionalProperties' not in schema
    assert 'expiry_date' in schema['properties']
    assert captured[0]['config'].max_output_tokens==1500
    assert captured[0]['contents'][1].inline_data.data==PDF


def test_orphaned_encrypted_file_is_removed_after_retention(booking_ctx,store):
    import os
    key=store.save(PDF,'application/pdf')
    old=(booking_ctx.now()-timedelta(days=31)).timestamp()
    os.utime(store.backend.store.root/key,(old,old))
    assert purge_expired(booking_ctx,store,30)==0
    with pytest.raises(FileNotFoundError):store.read(key)
