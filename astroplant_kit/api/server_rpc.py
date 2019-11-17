import logging
import trio
import datetime
import json
from .schema import astroplant_capnp
from .errors import *

logger = logging.getLogger("astroplant_kit.api.server_rpc")


REQUEST_TIMEOUT_SECONDS = 15


def _if_error_response_raise_exception_(response):
    """
    Raise an exception if the response is an error response.
    """
    if response.which() == "error":
        error = response.error
        error_which = error.which()
        if error_which == "other":
            raise RpcErrorOther()
        elif error_which == "methodNotFound":
            raise RpcErrorMethodNotFound()
        elif error_which == "rateLimit":
            raise RpcErrorRateLimit(error.rateLimit)
        else:
            raise RpcErrorUnknown()


class ServerRpc(object):
    """
    Handles MQTT messages to implement the server RPC system.
    """

    def __init__(self, server_rpc_request_handle):
        self._request_handle = server_rpc_request_handle

        self._rpc_next_request_id = 0
        self._rpc_response_queue = {}
        self._rpc_response_timeout = []

    async def run(self):
        await self._cleanup_rpc_response_queue()

    async def _on_response(self, payload):
        """
        Handles payloads received on `kit/{kit_serial}/server-rpc/response`.
        """
        response = astroplant_capnp.ServerRpcResponse.from_bytes_packed(payload)
        id = response.id
        logger.debug(f"Got server rpc response for request {id}")
        if id in self._rpc_response_queue:
            async with self._rpc_response_queue[id] as sender:
                del self._rpc_response_queue[id]
                await sender.send(response)

    def _send_request(self, request):
        """
        Send an RPC request over MQTT.
        """
        logger.debug(f"Sending server rpc request {request.id}")
        self._request_handle(request.to_bytes_packed())

    def _next_base_request(self):
        """
        Prepares a new RPC request, and creates a channel receiver for the
        response.
        """
        request_id = self._rpc_next_request_id
        self._rpc_next_request_id += 1

        response_sender, response_receiver = trio.open_memory_channel(0)

        value_callback = lambda value: loop.call_soon_threadsafe(fut.set_result, value)
        exception_callback = lambda value: loop.call_soon_threadsafe(
            fut.set_exception, value
        )
        self._rpc_response_queue[request_id] = response_sender
        self._rpc_response_timeout.append((request_id, datetime.datetime.now()))

        return (
            astroplant_capnp.ServerRpcRequest.new_message(id=request_id),
            response_receiver,
        )

    async def _cleanup_rpc_response_queue(self):
        while True:
            await trio.sleep(10)
            now = datetime.datetime.now()
            while len(self._rpc_response_timeout) > 0:
                (id, start) = self._rpc_response_timeout[0]
                if (now - start).total_seconds() < REQUEST_TIMEOUT_SECONDS:
                    # Not timed out.
                    break
                else:
                    logger.warning("Dropping server RPC request %s: timed out.", id)
                    self._rpc_response_timeout.pop(0)
                    if id in self._rpc_response_queue:
                        # Using with-block to explicitly close the channel.
                        async with self._rpc_response_queue[id]:
                            del self._rpc_response_queue[id]

    async def version(self):
        """
        Request the version of the RPC server.
        """
        (request, response_receiver) = self._next_base_request()
        request.version = None
        self._send_request(request)

        try:
            response = await response_receiver.receive()
        except trio.EndOfChannel:
            raise ServerRpcRequestTimedOut()

        _if_error_response_raise_exception_(response)
        if response.which() == "version":
            return response.version
        else:
            raise RpcInvalidResponse()

    async def get_active_configuration(self):
        """
        Request the active configuration of this kit.
        """
        (request, response_receiver) = self._next_base_request()
        request.getActiveConfiguration = None
        self._send_request(request)

        try:
            response = await response_receiver.receive()
        except trio.EndOfChannel:
            raise ServerRpcRequestTimedOut()

        _if_error_response_raise_exception_(response)
        if response.which() == "getActiveConfiguration":
            maybe_configuration = response.getActiveConfiguration
            if maybe_configuration.which() == "configuration":
                return json.loads(maybe_configuration.configuration)
            else:
                return None
        else:
            raise RpcInvalidResponse()

    async def get_quantity_types(self):
        """
        Request the quantity types known to the RPC server.
        """
        (request, response_receiver) = self._next_base_request()
        request.getQuantityTypes = None
        self._send_request(request)

        try:
            response = await response_receiver.receive()
        except Exception as e:
            raise ServerRpcRequestTimedOut()

        _if_error_response_raise_exception_(response)
        if response.which() == "getQuantityTypes":
            return json.loads(response.getQuantityTypes)
        else:
            raise RpcInvalidResponse()
