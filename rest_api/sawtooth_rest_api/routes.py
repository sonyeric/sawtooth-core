# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------
import asyncio
import json
import base64
from aiohttp import web
# pylint: disable=no-name-in-module,import-error
# needed for the google.protobuf imports to pass pylint
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from google.protobuf.message import Message as BaseMessage

from sawtooth_sdk.client.exceptions import ValidatorConnectionError
from sawtooth_sdk.client.future import FutureTimeoutError
from sawtooth_sdk.client.stream import Stream
from sawtooth_sdk.protobuf.validator_pb2 import Message

import sawtooth_rest_api.exceptions as errors
import sawtooth_rest_api.error_handlers as error_handlers
from sawtooth_rest_api.protobuf import client_pb2 as client
from sawtooth_rest_api.protobuf.block_pb2 import BlockHeader
from sawtooth_rest_api.protobuf.batch_pb2 import BatchList
from sawtooth_rest_api.protobuf.batch_pb2 import BatchHeader
from sawtooth_rest_api.protobuf.transaction_pb2 import TransactionHeader


class RouteHandler(object):
    def __init__(self, stream_url, timeout=300):
        self._stream = Stream(stream_url)
        self._timeout = timeout

    @asyncio.coroutine
    def batches_post(self, request):
        """
        Takes protobuf binary from HTTP POST, and sends it to the validator
        """
        # Parse request
        if request.headers['Content-Type'] != 'application/octet-stream':
            return errors.WrongBodyType()

        payload = yield from request.read()
        if not payload:
            return errors.EmptyProtobuf()

        try:
            batch_list = BatchList()
            batch_list.ParseFromString(payload)
        except DecodeError:
            return errors.BadProtobuf()

        # Query validator
        error_traps = [error_handlers.InvalidBatch()]
        validator_query = client.ClientBatchSubmitRequest(
            batches=batch_list.batches)
        self._set_wait(request, validator_query)

        response = self._query_validator(
            Message.CLIENT_BATCH_SUBMIT_REQUEST,
            client.ClientBatchSubmitResponse,
            validator_query,
            error_traps)

        # Build response envelope
        data = response['batch_statuses'] or None
        link = '{}://{}/batch_status?id={}'.format(
            request.scheme,
            request.host,
            ','.join(b.header_signature for b in batch_list.batches))

        if data is None:
            status = 202
        elif any(s != 'COMMITTED' for _, s in data.items()):
            status = 200
        else:
            status = 201
            data = None
            link = link.replace('batch_status', 'batches')

        return RouteHandler._wrap_response(
            data=data,
            metadata={'link': link},
            status=status)

    @asyncio.coroutine
    def status_list(self, request):
        """
        Fetches the status of a set of batches submitted to the validator.
        Batch ids can be submitted by query string (GET request), or by a
        POST body with a JSON formatted list of id strings.
        Will wait for batches to commit if the `wait` parameter is set
        """
        error_traps = [error_handlers.StatusesNotReturned()]

        if request.method == 'POST':
            if request.headers['Content-Type'] != 'application/json':
                return errors.BadStatusBody()

            batch_ids = yield from request.json()

            if not isinstance(batch_ids, list):
                return errors.BadStatusBody()
            if len(batch_ids) == 0:
                return errors.MissingStatusId()
            if not isinstance(batch_ids[0], str):
                return errors.BadStatusBody()

        else:
            try:
                batch_ids = request.url.query['id'].split(',')
            except KeyError:
                return errors.MissingStatusId()

        validator_query = client.ClientBatchStatusRequest(batch_ids=batch_ids)
        self._set_wait(request, validator_query)

        response = self._query_validator(
            Message.CLIENT_BATCH_STATUS_REQUEST,
            client.ClientBatchStatusResponse,
            validator_query,
            error_traps)

        if request.method != 'POST':
            metadata = RouteHandler._get_metadata(request, response)
        else:
            metadata = None

        return RouteHandler._wrap_response(
            data=response.get('batch_statuses'),
            metadata=metadata)

    @asyncio.coroutine
    def state_list(self, request):
        """
        Fetch a list of data leaves from the validator's state merkle-tree
        """
        head = request.url.query.get('head', None)
        address = request.url.query.get('address', None)

        response = self._query_validator(
            Message.CLIENT_STATE_LIST_REQUEST,
            client.ClientStateListResponse,
            client.ClientStateListRequest(head_id=head, address=address))

        return RouteHandler._wrap_response(
            data=response.get('leaves', []),
            metadata=RouteHandler._get_metadata(request, response))

    @asyncio.coroutine
    def state_get(self, request):
        """
        Fetch a specific data leaf from the validator's state merkle-tree
        """
        error_traps = [
            error_handlers.MissingLeaf(),
            error_handlers.BadAddress()]

        address = request.match_info.get('address', '')
        head = request.url.query.get('head', None)

        response = self._query_validator(
            Message.CLIENT_STATE_GET_REQUEST,
            client.ClientStateGetResponse,
            client.ClientStateGetRequest(head_id=head, address=address),
            error_traps)

        return RouteHandler._wrap_response(
            data=response['value'],
            metadata=RouteHandler._get_metadata(request, response))

    @asyncio.coroutine
    def block_list(self, request):
        """
        Fetch a particular block from the validator
        """
        head = request.url.query.get('head', None)
        block_ids = RouteHandler._get_filter_ids(request)

        response = self._query_validator(
            Message.CLIENT_BLOCK_LIST_REQUEST,
            client.ClientBlockListResponse,
            client.ClientBlockListRequest(head_id=head, block_ids=block_ids))

        blocks = [RouteHandler._expand_block(b) for b in response['blocks']]
        return RouteHandler._wrap_response(
            data=blocks,
            metadata=RouteHandler._get_metadata(request, response))

    @asyncio.coroutine
    def block_get(self, request):
        """
        Fetch a list of blocks from the validator
        """
        error_traps = [
            error_handlers.MissingBlock(),
            error_handlers.InvalidBlockId()]

        block_id = request.match_info.get('block_id', '')

        response = self._query_validator(
            Message.CLIENT_BLOCK_GET_REQUEST,
            client.ClientBlockGetResponse,
            client.ClientBlockGetRequest(block_id=block_id),
            error_traps)

        return RouteHandler._wrap_response(
            data=RouteHandler._expand_block(response['block']),
            metadata=RouteHandler._get_metadata(request, response))

    @asyncio.coroutine
    def batch_list(self, request):
        """
        Fetch a list of batches from the validator
        """
        head = request.url.query.get('head', None)
        batch_ids = RouteHandler._get_filter_ids(request)

        response = self._query_validator(
            Message.CLIENT_BATCH_LIST_REQUEST,
            client.ClientBatchListResponse,
            client.ClientBatchListRequest(head_id=head, batch_ids=batch_ids))

        batches = [RouteHandler._expand_batch(b) for b in response['batches']]
        return RouteHandler._wrap_response(
            data=batches,
            metadata=RouteHandler._get_metadata(request, response))

    @asyncio.coroutine
    def batch_get(self, request):
        """
        Fetch a particular batch from the validator
        """
        error_traps = [
            error_handlers.MissingBatch(),
            error_handlers.InvalidBatchId()]

        batch_id = request.match_info.get('batch_id', '')

        response = self._query_validator(
            Message.CLIENT_BATCH_GET_REQUEST,
            client.ClientBatchGetResponse,
            client.ClientBatchGetRequest(batch_id=batch_id),
            error_traps)

        return RouteHandler._wrap_response(
            data=RouteHandler._expand_batch(response['batch']),
            metadata=RouteHandler._get_metadata(request, response))

    def _query_validator(self, req_type, resp_proto, content, traps=None):
        """
        Sends a request to the validator and parses the response
        """
        response = self._try_validator_request(req_type, content)
        return RouteHandler._try_response_parse(resp_proto, response, traps)

    def _try_validator_request(self, message_type, content):
        """
        Sends a protobuf message to the validator
        Handles a possible timeout if validator is unresponsive
        """
        if isinstance(content, BaseMessage):
            content = content.SerializeToString()

        future = self._stream.send(message_type=message_type, content=content)

        try:
            response = future.result(timeout=self._timeout)
        except FutureTimeoutError:
            raise errors.ValidatorUnavailable()

        try:
            return response.content
            # the error is caused by resolving a FutureError
            # on validator disconnect.
        except ValidatorConnectionError:
            raise errors.ValidatorUnavailable()

    @staticmethod
    def _try_response_parse(proto, response, traps=None):
        """
        Parses a protobuf response from the validator
        Raises common validator error statuses as HTTP errors
        """
        parsed = proto()
        parsed.ParseFromString(response)
        traps = traps or []

        try:
            traps.append(error_handlers.Unknown(proto.INTERNAL_ERROR))
        except AttributeError:
            # Not every protobuf has every status enum, so pass AttributeErrors
            pass
        try:
            traps.append(error_handlers.NotReady(proto.NOT_READY))
        except AttributeError:
            pass
        try:
            traps.append(error_handlers.MissingHead(proto.NO_ROOT))
        except AttributeError:
            pass

        for trap in traps:
            trap.check(parsed.status)

        return MessageToDict(
            parsed,
            including_default_value_fields=True,
            preserving_proto_field_name=True,
        )

    @staticmethod
    def _wrap_response(data=None, metadata=None, status=200):
        """
        Creates a JSON response envelope and sends it back to the client
        """
        envelope = metadata or {}

        if data is not None:
            envelope['data'] = data

        return web.Response(
            status=status,
            content_type='application/json',
            text=json.dumps(
                envelope,
                indent=2,
                separators=(',', ': '),
                sort_keys=True))

    @staticmethod
    def _get_metadata(request, response):
        head = response.get('head_id', None)
        if not head:
            return {'link': str(request.url)}

        link = '{}://{}{}?head={}'.format(
            request.scheme,
            request.host,
            request.path,
            head)

        headless = filter(lambda i: i[0] != 'head', request.url.query.items())
        queries = ['{}={}'.format(k, v) for k, v in headless]
        if len(queries) > 0:
            link += '&' + '&'.join(queries)

        return {'head': head, 'link': link}

    def _set_wait(self, request, validator_query):
        """
        Parses the `wait` query parameter, and sets the corresponding
        fields in a validator query
        """
        wait = request.url.query.get('wait', 'false')
        if wait.lower() != 'false':
            validator_query.wait_for_commit = True
            try:
                validator_query.timeout = int(wait)
            except ValueError:
                # By default, waits for 95% of REST API's configured timeout
                validator_query.timeout = int(self._timeout * 0.95)

    @staticmethod
    def _expand_block(block):
        RouteHandler._parse_header(BlockHeader, block)
        if 'batches' in block:
            block['batches'] = [RouteHandler._expand_batch(b)
                                for b in block['batches']]
        return block

    @staticmethod
    def _expand_batch(batch):
        RouteHandler._parse_header(BatchHeader, batch)
        if 'transactions' in batch:
            batch['transactions'] = [RouteHandler._expand_transaction(t)
                                     for t in batch['transactions']]
        return batch

    @staticmethod
    def _expand_transaction(transaction):
        return RouteHandler._parse_header(TransactionHeader, transaction)

    @staticmethod
    def _parse_header(header_proto, obj):
        """
        A helper method to parse a byte string encoded protobuf 'header'
        Args:
            header_proto: The protobuf class of the encoded header
            obj: The dict formatted object containing the 'header'
        """
        header = header_proto()
        header_bytes = base64.b64decode(obj['header'])
        header.ParseFromString(header_bytes)
        obj['header'] = MessageToDict(header, preserving_proto_field_name=True)
        return obj

    @staticmethod
    def _get_filter_ids(request):
        filter_ids = request.url.query.get('id', None)
        return filter_ids and filter_ids.split(',')
