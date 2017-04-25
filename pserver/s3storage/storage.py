# -*- coding: utf-8 -*-

import aiohttp
import asyncio
import base64
import botocore
import boto3
import aiobotocore
import json
import logging
import transaction
import uuid
import multidict

from aiohttp.web import StreamResponse
from datetime import datetime
from datetime import timedelta
from dateutil.tz import tzlocal
from io import BytesIO
from persistent import Persistent
from plone.server.browser import Response
from plone.server.events import notify
from plone.server.interfaces import IAbsoluteURL
from plone.server.interfaces import IFileManager
from plone.server.interfaces import IRequest
from plone.server.interfaces import IResource
from plone.server.interfaces import IApplication
from plone.server.json.interfaces import IValueToJson
from plone.server.transactions import RequestNotFound
from plone.server.transactions import get_current_request
from plone.server.transactions import tm
from pserver.s3storage.events import FinishS3Upload
from pserver.s3storage.events import InitialS3Upload
from pserver.s3storage.interfaces import IS3BlobStore
from pserver.s3storage.interfaces import IS3File
from pserver.s3storage.interfaces import IS3FileField
from zope.component import adapter
from zope.component import getUtility
from zope.interface import implementer
from zope.schema import Object
from zope.schema.fieldproperty import FieldProperty
from plone.server import configure
import functools


log = logging.getLogger('pserver.storage')

MAX_SIZE = 1073741824

MIN_UPLOAD_SIZE = 5 * 1024 * 1024
CHUNK_SIZE = MIN_UPLOAD_SIZE
MAX_RETRIES = 5


class S3Exception(Exception):
    pass


@configure.adapter(
    for_=IS3File,
    provides=IValueToJson)
def json_converter(value):
    if value is None:
        return value

    return {
        'filename': value.filename,
        'contenttype': value.contentType,
        'size': value.size,
        'extension': value.extension,
        'md5': value.md5
    }


@configure.adapter(
    for_=(IResource, IRequest, IS3FileField),
    provides=IFileManager)
class S3FileManager(object):

    def __init__(self, context, request, field):
        self.context = context
        self.request = request
        self.field = field

    async def upload(self):
        """In order to support TUS and IO upload.

        we need to provide an upload that concats the incoming
        """
        file = self.field.get(self.context)
        if file is None:
            file = S3File(contentType=self.request.content_type)
            self.field.set(self.context, file)
            try:
                trns = tm(self.request).get()
            except RequestNotFound:
                trns = transaction.get()
            trns.savepoint()
        if 'X-UPLOAD-MD5HASH' in self.request.headers:
            file._md5hash = self.request.headers['X-UPLOAD-MD5HASH']
        else:
            file._md5hash = None

        if 'X-UPLOAD-EXTENSION' in self.request.headers:
            file._extension = self.request.headers['X-UPLOAD-EXTENSION']
        else:
            file._extension = None

        if 'X-UPLOAD-SIZE' in self.request.headers:
            file._size = int(self.request.headers['X-UPLOAD-SIZE'])
        else:
            raise AttributeError('x-upload-size header needed')

        if 'X-UPLOAD-FILENAME' in self.request.headers:
            file.filename = self.request.headers['X-UPLOAD-FILENAME']
        elif 'X-UPLOAD-FILENAME-B64' in self.request.headers:
            file.filename = base64.b64decode(self.request.headers['X-UPLOAD-FILENAME-B64']).decode("utf-8")    
        else:
            file.filename = uuid.uuid4().hex

        await file.initUpload(self.context)
        try:
            data = await self.request.content.readexactly(CHUNK_SIZE)
        except asyncio.IncompleteReadError as e:
            data = e.partial

        count = 0

        # If we have data or is an empty file
        while data or (len(data) == 0 and count == 0):
            old_current_upload = file._current_upload
            resp = await file.appendData(data)
            count +=1
            try:
                data = await self.request.content.readexactly(CHUNK_SIZE)  # noqa
            except asyncio.IncompleteReadError as e:
                data = e.partial

        # Test resp and checksum to finish upload
        await file.finishUpload(self.context)

    async def tus_create(self):

        # This only happens in tus-java-client, redirect this POST to a PATCH
        if self.request.headers.get('X-HTTP-Method-Override') == 'PATCH':
            return await self.tus_patch()

        file = self.field.get(self.context)
        if file is None:
            file = S3File(contentType=self.request.content_type)
            self.field.set(self.context, file)
        if 'CONTENT-LENGTH' in self.request.headers:
            file._current_upload = int(self.request.headers['CONTENT-LENGTH'])
        else:
            file._current_upload = 0

        if 'UPLOAD-LENGTH' in self.request.headers:
            file._size = int(self.request.headers['UPLOAD-LENGTH'])
        else:
            raise AttributeError('We need upload-length header')

        if 'UPLOAD-MD5' in self.request.headers:
            file._md5hash = self.request.headers['UPLOAD-MD5']

        if 'UPLOAD-EXTENSION' in self.request.headers:
            file._extension = self.request.headers['UPLOAD-EXTENSION']

        if 'TUS-RESUMABLE' not in self.request.headers:
            raise AttributeError('Its a TUS needs a TUS version')

        if 'UPLOAD-METADATA' not in self.request.headers:
            file.filename = uuid.uuid4().hex
        else:
            filename = self.request.headers['UPLOAD-METADATA']
            file.filename = base64.b64decode(filename.split()[1]).decode('utf-8')

        await file.initUpload(self.context)
        if file.size < MIN_UPLOAD_SIZE:
            file._one_tus_shoot = True
        else:
            file._one_tus_shoot = False
        # Location will need to be adapted on aiohttp 1.1.x
        resp = Response(headers=multidict.MultiDict({
            'Location': IAbsoluteURL(self.context, self.request)() + '/@tusupload/' + self.field.__name__,  # noqa
            'Tus-Resumable': '1.0.0',
            'Access-Control-Expose-Headers': 'Location,Tus-Resumable'
        }), status=201)
        return resp

    async def tus_patch(self):
        file = self.field.get(self.context)
        if 'CONTENT-LENGTH' in self.request.headers:
            to_upload = int(self.request.headers['CONTENT-LENGTH'])
        else:
            raise AttributeError('No content-length header')

        if 'UPLOAD-OFFSET' in self.request.headers:
            file._current_upload = int(self.request.headers['UPLOAD-OFFSET'])
        else:
            raise AttributeError('No upload-offset header')

        try:
            data = await self.request.content.readexactly(to_upload)
        except asyncio.IncompleteReadError as e:
            data = e.partial
        if file.one_tus_shoot:
            # One time shoot
            if file._block > 1:
                raise AttributeError('You should push 5Mb blocks AWS')
            await file.oneShotUpload(self.context, data)
            expiration = datetime.now() + timedelta(days=365)
        else:
            count = 0
            while data:
                resp = await file.appendData(data)
                count += 1

                try:
                    data = await self.request.content.readexactly(CHUNK_SIZE)  # noqa
                except asyncio.IncompleteReadError as e:
                    data = e.partial

            expiration = file._resumable_uri_date + timedelta(days=7)
        if file._size <= file._current_upload:
            await file.finishUpload(self.context)
        resp = Response(headers=multidict.MultiDict({
            'Upload-Offset': str(file.actualSize()),
            'Tus-Resumable': '1.0.0',
            'Upload-Expires': expiration.isoformat(),
            'Access-Control-Expose-Headers': 'Upload-Offset,Upload-Expires,Tus-Resumable'
        }))
        return resp

    async def tus_head(self):
        file = self.field.get(self.context)
        if file is None:
            raise KeyError('No file on this context')
        head_response = {
            'Upload-Offset': str(file.actualSize()),
            'Tus-Resumable': '1.0.0',
            'Access-Control-Expose-Headers': 'Upload-Offset,Upload-Length,Tus-Resumable'
        }
        if file.size:
            head_response['Upload-Length'] = str(file._size)
        resp = Response(headers=multidict.MultiDict(head_response))
        return resp

    async def tus_options(self):
        resp = Response(headers=multidict.MultiDict({
            'Tus-Resumable': '1.0.0',
            'Tus-Version': '1.0.0',
            'Tus-Max-Size': '1073741824',
            'Tus-Extension': 'creation,expiration'
        }))
        return resp


    async def download(self):
        file = self.field.get(self.context)
        if file is None:
            raise AttributeError('No field value')
        resp = StreamResponse(headers=multidict.MultiDict({
            'CONTENT-DISPOSITION': 'attachment; filename="%s"' % file.filename
        }))
        resp.content_type = file.contentType
        resp.content_type = 'application/octet-stream'
        resp.content_length = file._size

        downloader = await file.download(None)
        await resp.prepare(self.request)
        resp.start(self.request)

        async with downloader['Body'] as stream:
            data = await stream.read(CHUNK_SIZE)

            while data:
                resp.write(data)
                data = await stream.read(CHUNK_SIZE)

        return resp


@implementer(IS3File)
class S3File(Persistent):
    """File stored in a GCloud, with a filename."""

    filename = FieldProperty(IS3File['filename'])

    def __init__(  # noqa
            self,
            contentType='application/octet-stream',
            filename=None):
        self.contentType = contentType
        self._current_upload = 0
        if filename is not None:
            self.filename = filename
            extension_discovery = filename.split('.')
            if len(extension_discovery) > 1:
                self._extension = extension_discovery[-1]
        elif self.filename is not None:
            self.filename = uuid.uuid4().hex

    async def initUpload(self, context):
        """Init an upload.

        self._uload_file_id : temporal url to image beeing uploaded
        self._resumable_uri : uri to resumable upload
        self._uri : finished uploaded image
        """
        util = getUtility(IS3BlobStore)
        request = get_current_request()
        if hasattr(self, '_upload_file_id') and self._upload_file_id is not None:  # noqa
            util._s3client.abort_multipart_upload(
                Bucket=self._bucket_name,
                Key=self._upload_file_id,
                UploadId=self._mpu['UploadId'])
            self._mpu = None
            self._upload_file_id = None

        bucket = util.bucket
        self._bucket_name = bucket._name
        self._upload_file_id = request._site_id + '/' + uuid.uuid4().hex
        self._multipart = {'Parts': []}
        self._mpu = util._s3client.create_multipart_upload(
            Bucket=self._bucket_name, Key=self._upload_file_id)
        self._current_upload = 0
        self._block = 1
        self._resumable_uri_date = datetime.now(tz=tzlocal())
        await notify(InitialS3Upload(context))

    async def appendData(self, data):
        util = getUtility(IS3BlobStore)
        part = util._s3client.upload_part(
            Bucket=self._bucket_name,
            Key=self._upload_file_id,
            PartNumber=self._block,
            UploadId=self._mpu['UploadId'],
            Body=data)
        self._multipart['Parts'].append({
            'PartNumber': self._block,
            'ETag': part['ETag']
        })
        self._current_upload += len(data)
        self._block += 1
        return part

    def actualSize(self):
        return self._current_upload

    async def finishUpload(self, context):
        util = getUtility(IS3BlobStore)
        # It would be great to do on AfterCommit
        if hasattr(self, '_uri') and self._uri is not None:
            try:
                util._s3client.delete_object(
                    Bucket=self._bucket_name,
                    Key=self._uri)
            except botocore.exceptions.ClientError as e:
                pass
        self._uri = self._upload_file_id
        if self._mpu is not None:
            util._s3client.complete_multipart_upload(
                Bucket=self._bucket_name,
                Key=self._upload_file_id,
                UploadId=self._mpu['UploadId'],
                MultipartUpload=self._multipart)
        self._multipart = None
        self._block = None
        self._upload_file_id = None
        await notify(FinishS3Upload(context))

    async def oneShotUpload(self, context, data):
        util = getUtility(IS3BlobStore)

        if hasattr(self, '_upload_file_id') and self._upload_file_id is not None:  # noqa
            util._s3client.abort_multipart_upload(
                Bucket=self._bucket_name,
                Key=self._upload_file_id,
                UploadId=self._mpu['UploadId'])
            self._mpu = None
            self._upload_file_id = None
        file_data = BytesIO(data)
        bucket = util.bucket
        self._bucket_name = bucket._name
        request = get_current_request()
        self._upload_file_id = request._site_id + '/' + uuid.uuid4().hex
        response = util._s3client.upload_fileobj(
            file_data,
            self._bucket_name,
            self._upload_file_id)
        self._block += 1
        self._current_upload += len(data)
        return response

    async def deleteUpload(self):
        util = getUtility(IS3BlobStore)
        if hasattr(self, '_uri') and self._uri is not None:
            try:
                util._s3client.delete_object(
                    Bucket=self._bucket_name,
                    Key=self._uri)
            except botocore.exceptions.ClientError as e:
                pass
        else:
            raise AttributeError('No valid uri')

    async def download(self, buf):
        util = getUtility(IS3BlobStore)
        if not hasattr(self, '_uri'):
            url = self._upload_file_id
        else:
            url = self._uri

        downloader = await util._s3aioclient.get_object(Bucket=self._bucket_name, Key=url)
        return downloader

    def _set_data(self, data):
        raise NotImplemented('Only specific upload permitted')

    def _get_data(self):
        raise NotImplemented('Only specific download permitted')

    data = property(_get_data, _set_data)

    @property
    def size(self):
        if hasattr(self, '_size'):
            return self._size
        else:
            return None

    @property
    def md5(self):
        if hasattr(self, '_md5hash'):
            return self._md5hash
        else:
            return None

    @property
    def extension(self):
        if hasattr(self, '_extension'):
            return self._extension
        else:
            return None

    @property
    def one_tus_shoot(self):
        if hasattr(self, '_one_tus_shoot'):
            return self._one_tus_shoot
        else:
            return False

    def getSize(self):  # noqa
        return self.size


@implementer(IS3FileField)
class S3FileField(Object):
    """A NamedBlobFile field."""

    _type = S3File
    schema = IS3File

    def __init__(self, **kw):
        if 'schema' in kw:
            self.schema = kw.pop('schema')
        super(S3FileField, self).__init__(schema=self.schema, **kw)


# Configuration Utility

class S3BlobStore(object):

    def __init__(self, settings):
        self._aws_access_key = settings['aws_client_id']
        self._aws_secret_key = settings['aws_client_secret']

        loop = asyncio.get_event_loop()
        self.session = aiobotocore.get_session(loop=loop)

        # This client is for downloads only
        self._s3aioclient = self.session.create_client('s3',
                              aws_secret_access_key=self._aws_secret_key,
                              aws_access_key_id=self._aws_access_key)

        self._bucket_name = settings['bucket']

        self._s3client = boto3.client(
            's3',
            aws_access_key_id=self._aws_access_key,
            aws_secret_access_key=self._aws_secret_key
        )

        self._s3 = boto3.resource(
            's3',
            aws_access_key_id=self._aws_access_key,
            aws_secret_access_key=self._aws_secret_key
        )

    @property
    def bucket(self):
        request = get_current_request()
        bucket_name = request._site_id.lower() + '.' + self._bucket_name

        bucket = self._s3.Bucket(bucket_name)
        exists = True
        try:
            self._s3.meta.client.head_bucket(Bucket=bucket_name)
        except botocore.exceptions.ClientError as e:
            # If a client error is thrown, then check that it was a 404 error.
            # If it was a 404 error, then the bucket does not exist.
            error_code = int(e.response['Error']['Code'])
            if error_code == 404:
                exists = False
                self._s3.create_bucket(Bucket=bucket_name)
                bucket = s3.Bucket(bucket_name)
        return bucket

    async def initialize(self, app=None):
        # No asyncio loop to run
        self.app = app
