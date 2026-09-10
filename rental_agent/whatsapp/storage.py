"""Private encrypted attachment storage, on disk or in an S3 bucket."""
import os
import re
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .media import PrivateMediaStore, MediaError, MAX_BYTES, MIME_EXTENSIONS, validate_file

PREFIX = b'RENTAL-ENC-1\n'


class EncryptedStore:
    def __init__(self, backend, key):
        self.backend = backend
        self.cipher = Fernet(key.encode())

    def save(self, data, mime):
        validate_file(data, mime)
        return self.backend.put(PREFIX + self.cipher.encrypt(data), MIME_EXTENSIONS[mime])

    def read(self, key):
        data = self.backend.get(key)
        if not data.startswith(PREFIX): raise MediaError('Unencrypted document refused')
        try: return self.cipher.decrypt(data[len(PREFIX):])
        except InvalidToken as exc: raise MediaError('Document decryption failed') from exc

    def delete(self, key): self.backend.delete(key)

    def purge_before(self, cutoff):
        return self.backend.purge_before(cutoff)


def valid_key(key):
    if not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|pdf)', key):
        raise MediaError('Invalid document reference')


class DiskBackend:
    def __init__(self, root=None): self.store = PrivateMediaStore(root)
    def put(self, data, extension):
        root = self.store.root
        root.mkdir(mode=0o700, parents=True, exist_ok=True); root.chmod(0o700)
        key = uuid4().hex + extension
        fd = os.open(root / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f: f.write(data)
        return key
    def get(self, key):
        valid_key(key)
        path = self.store.root / key
        if path.is_symlink(): raise MediaError('Invalid document reference')
        with path.open('rb') as f: data = f.read(MAX_BYTES * 2 + 1)
        if len(data) > MAX_BYTES * 2: raise MediaError('Document too large')
        return data
    def delete(self, key): self.store.delete(key)
    def purge_before(self, cutoff):
        if not self.store.root.exists(): return
        for path in self.store.root.iterdir():
            if path.is_symlink() or not path.is_file(): continue
            if re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|pdf)', path.name) and path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)


class S3Backend:
    def __init__(self, bucket, client=None):
        if not bucket: raise ValueError('DOCUMENT_S3_BUCKET is required')
        if client is None:
            import boto3
            client = boto3.client('s3', endpoint_url=os.getenv('DOCUMENT_S3_ENDPOINT') or None)
        self.bucket, self.client = bucket, client
    def put(self, data, extension):
        key = uuid4().hex + extension
        self.client.put_object(Bucket=self.bucket, Key='documents/' + key, Body=data,
                               ContentType='application/octet-stream', ServerSideEncryption='AES256')
        return key
    def get(self, key):
        valid_key(key)
        stream = self.client.get_object(Bucket=self.bucket, Key='documents/' + key)['Body']
        try: data = stream.read(MAX_BYTES * 2 + 1)
        finally: stream.close()
        if len(data) > MAX_BYTES * 2: raise MediaError('Document too large')
        return data
    def delete(self, key):
        valid_key(key)
        self.client.delete_object(Bucket=self.bucket, Key='documents/' + key)
    def purge_before(self, cutoff):
        # Also removes orphaned uploads from a worker crash before DB commit.
        pages = self.client.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket, Prefix='documents/')
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key'].removeprefix('documents/')
                if re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|pdf)', key) and obj['LastModified'] < cutoff:
                    self.delete(key)


def build_store():
    mode = os.getenv('DOCUMENT_STORAGE', 'local')
    key = os.getenv('DOCUMENT_ENCRYPTION_KEY', '')
    if mode == 's3':
        if not key: raise ValueError('DOCUMENT_ENCRYPTION_KEY is required for S3 documents')
        return EncryptedStore(S3Backend(os.getenv('DOCUMENT_S3_BUCKET')), key)
    if mode != 'local': raise ValueError('Unknown DOCUMENT_STORAGE')
    return EncryptedStore(DiskBackend(), key) if key else PrivateMediaStore()


def existing_browser_cipher():
    """Restore the local tester's key on read-only turns after a restart."""
    path = DiskBackend().store.root / '.encryption-key'
    if path.is_symlink(): raise MediaError('Invalid encryption key path')
    return Fernet(path.read_bytes().strip()) if path.exists() else None


def build_browser_store():
    """Give the local tester a persistent private key without exposing it in UI."""
    if not os.getenv('DOCUMENT_ENCRYPTION_KEY'):
        backend = DiskBackend()
        root = backend.store.root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        path = root / '.encryption-key'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink(): raise MediaError('Invalid encryption key path')
        else:
            with os.fdopen(fd,'wb') as stream: stream.write(Fernet.generate_key())
        os.environ['DOCUMENT_ENCRYPTION_KEY'] = path.read_text().strip()
    return build_store()
