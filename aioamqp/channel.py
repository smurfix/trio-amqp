"""
    Amqp channel specification
"""

import asyncio
import logging
import uuid
import io
from itertools import count
import warnings

import pamqp.specification

from . import constants as amqp_constants
from . import frame as amqp_frame
from . import exceptions
from .envelope import Envelope, ReturnEnvelope


logger = logging.getLogger(__name__)


class Channel:

    def __init__(self, protocol, channel_id, return_callback=None):
        self._loop = protocol._loop
        self.protocol = protocol
        self.channel_id = channel_id
        self.consumer_queues = {}
        self.consumer_callbacks = {}
        self.cancellation_callbacks = []
        self.return_callback = return_callback
        self.response_future = None
        self.close_event = asyncio.Event(loop=self._loop)
        self.cancelled_consumers = set()
        self.last_consumer_tag = None
        self.publisher_confirms = False
        self.delivery_tag_iter = None  # used for mapping delivered messages to publisher confirms

        self._futures = {}
        self._ctag_events = {}

    def _set_waiter(self, rpc_name):
        if rpc_name in self._futures:
            raise exceptions.SynchronizationError("Waiter already exists")

        fut = asyncio.Future(loop=self._loop)
        self._futures[rpc_name] = fut
        return fut

    def _get_waiter(self, rpc_name):
        fut = self._futures.pop(rpc_name, None)
        if not fut:
            raise exceptions.SynchronizationError("Call %s didn't set a waiter" % rpc_name)
        return fut

    @property
    def is_open(self):
        return not self.close_event.is_set()

    def connection_closed(self, server_code=None, server_reason=None, exception=None):
        for future in self._futures.values():
            if future.done():
                continue
            if exception is None:
                kwargs = {}
                if server_code is not None:
                    kwargs['code'] = server_code
                if server_reason is not None:
                    kwargs['message'] = server_reason
                exception = exceptions.ChannelClosed(**kwargs)
            future.set_exception(exception)

        self.protocol.release_channel_id(self.channel_id)
        self.close_event.set()

    @asyncio.coroutine
    def dispatch_frame(self, frame):
        methods = {
            (amqp_constants.CLASS_CHANNEL, amqp_constants.CHANNEL_OPEN_OK): self.open_ok,
            (amqp_constants.CLASS_CHANNEL, amqp_constants.CHANNEL_FLOW_OK): self.flow_ok,
            (amqp_constants.CLASS_CHANNEL, amqp_constants.CHANNEL_CLOSE_OK): self.close_ok,
            (amqp_constants.CLASS_CHANNEL, amqp_constants.CHANNEL_CLOSE): self.server_channel_close,

            (amqp_constants.CLASS_EXCHANGE, amqp_constants.EXCHANGE_DECLARE_OK): self.exchange_declare_ok,
            (amqp_constants.CLASS_EXCHANGE, amqp_constants.EXCHANGE_BIND_OK): self.exchange_bind_ok,
            (amqp_constants.CLASS_EXCHANGE, amqp_constants.EXCHANGE_UNBIND_OK): self.exchange_unbind_ok,
            (amqp_constants.CLASS_EXCHANGE, amqp_constants.EXCHANGE_DELETE_OK): self.exchange_delete_ok,

            (amqp_constants.CLASS_QUEUE, amqp_constants.QUEUE_DECLARE_OK): self.queue_declare_ok,
            (amqp_constants.CLASS_QUEUE, amqp_constants.QUEUE_DELETE_OK): self.queue_delete_ok,
            (amqp_constants.CLASS_QUEUE, amqp_constants.QUEUE_BIND_OK): self.queue_bind_ok,
            (amqp_constants.CLASS_QUEUE, amqp_constants.QUEUE_UNBIND_OK): self.queue_unbind_ok,
            (amqp_constants.CLASS_QUEUE, amqp_constants.QUEUE_PURGE_OK): self.queue_purge_ok,

            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_QOS_OK): self.basic_qos_ok,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_CONSUME_OK): self.basic_consume_ok,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_CANCEL_OK): self.basic_cancel_ok,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_GET_OK): self.basic_get_ok,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_GET_EMPTY): self.basic_get_empty,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_DELIVER): self.basic_deliver,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_CANCEL): self.server_basic_cancel,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_ACK): self.basic_server_ack,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_NACK): self.basic_server_nack,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_RECOVER_OK): self.basic_recover_ok,
            (amqp_constants.CLASS_BASIC, amqp_constants.BASIC_RETURN): self.basic_return,

            (amqp_constants.CLASS_CONFIRM, amqp_constants.CONFIRM_SELECT_OK): self.confirm_select_ok,
        }

        if (frame.class_id, frame.method_id) not in methods:
            raise NotImplementedError("Frame (%s, %s) is not implemented" % (frame.class_id, frame.method_id))
        yield from methods[(frame.class_id, frame.method_id)](frame)

    @asyncio.coroutine
    def _write_frame(self, channel_id, request, check_open=True, drain=True):
        yield from self.protocol.ensure_open()
        if not self.is_open and check_open:
            raise exceptions.ChannelClosed()
        amqp_frame.write(self.protocol._stream_writer, channel_id, request)
        if drain:
            yield from self.protocol._drain()

    @asyncio.coroutine
    def _write_frame_awaiting_response(self, waiter_id, channel_id, request, no_wait, check_open=True, drain=True):
        '''Write a frame and set a waiter for the response (unless no_wait is set)'''
        if no_wait:
            yield from self._write_frame(channel_id, request, check_open=check_open, drain=drain)
            return None

        f = self._set_waiter(waiter_id)
        try:
            yield from self._write_frame(channel_id, request, check_open=check_open, drain=drain)
        except Exception:
            self._get_waiter(waiter_id)
            f.cancel()
            raise
        return (yield from f)

#
## Channel class implementation
#

    @asyncio.coroutine
    def open(self):
        """Open the channel on the server."""
        request = pamqp.specification.Channel.Open()
        return (yield from self._write_frame_awaiting_response(
            'open', self.channel_id, request, no_wait=False, check_open=False))

    @asyncio.coroutine
    def open_ok(self, frame):
        self.close_event.clear()
        fut = self._get_waiter('open')
        fut.set_result(True)
        logger.debug("Channel is open")

    @asyncio.coroutine
    def close(self, reply_code=0, reply_text="Normal Shutdown"):
        """Close the channel."""
        if not self.is_open:
            raise exceptions.ChannelClosed("channel already closed or closing")
        self.close_event.set()
        request = pamqp.specification.Channel.Close(reply_code, reply_text, class_id=0, method_id=0)
        return (yield from self._write_frame_awaiting_response(
            'close', self.channel_id, request, no_wait=False, check_open=False))

    @asyncio.coroutine
    def close_ok(self, frame):
        self._get_waiter('close').set_result(True)
        logger.info("Channel closed")
        self.protocol.release_channel_id(self.channel_id)

    @asyncio.coroutine
    def _send_channel_close_ok(self):
        request = pamqp.specification.Channel.CloseOk()
        yield from self._write_frame(self.channel_id, request)

    @asyncio.coroutine
    def server_channel_close(self, frame):
        yield from self._send_channel_close_ok()
        results = {
            'reply_code': frame.payload_decoder.read_short(),
            'reply_text': frame.payload_decoder.read_shortstr(),
            'class_id': frame.payload_decoder.read_short(),
            'method_id': frame.payload_decoder.read_short(),
        }
        self.connection_closed(results['reply_code'], results['reply_text'])

    @asyncio.coroutine
    def flow(self, active):
        request = pamqp.specification.Channel.Flow(active)
        return (yield from self._write_frame_awaiting_response(
            'flow', self.channel_id, request, no_wait=False,
            check_open=False))

    @asyncio.coroutine
    def flow_ok(self, frame):
        decoder = amqp_frame.AmqpDecoder(frame.payload)
        active = bool(decoder.read_octet())
        self.close_event.clear()
        fut = self._get_waiter('flow')
        fut.set_result({'active': active})

        logger.debug("Flow ok")

#
## Exchange class implementation
#

    @asyncio.coroutine
    def exchange_declare(self, exchange_name, type_name, passive=False, durable=False,
                         auto_delete=False, no_wait=False, arguments=None):
        request = pamqp.specification.Exchange.Declare(
            exchange=exchange_name,
            exchange_type=type_name,
            passive=passive,
            durable=durable,
            auto_delete=auto_delete,
            nowait=no_wait,
            arguments=arguments
        )

        return (yield from self._write_frame_awaiting_response(
            'exchange_declare', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def exchange_declare_ok(self, frame):
        future = self._get_waiter('exchange_declare')
        future.set_result(True)
        logger.debug("Exchange declared")
        return future

    @asyncio.coroutine
    def exchange_delete(self, exchange_name, if_unused=False, no_wait=False):
        request = pamqp.specification.Exchange.Delete(exchange=exchange_name, if_unused=if_unused, nowait=no_wait)
        return (yield from self._write_frame_awaiting_response(
            'exchange_delete', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def exchange_delete_ok(self, frame):
        future = self._get_waiter('exchange_delete')
        future.set_result(True)
        logger.debug("Exchange deleted")

    @asyncio.coroutine
    def exchange_bind(self, exchange_destination, exchange_source, routing_key,
                      no_wait=False, arguments=None):
        if arguments is None:
            arguments = {}
        request = pamqp.specification.Exchange.Bind(
            destination=exchange_destination,
            source=exchange_source,
            routing_key=routing_key,
            nowait=no_wait,
            arguments=arguments
        )
        return (yield from self._write_frame_awaiting_response(
            'exchange_bind', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def exchange_bind_ok(self, frame):
        future = self._get_waiter('exchange_bind')
        future.set_result(True)
        logger.debug("Exchange bound")

    @asyncio.coroutine
    def exchange_unbind(self, exchange_destination, exchange_source, routing_key,
                        no_wait=False, arguments=None):
        if arguments is None:
            arguments = {}

        request = pamqp.specification.Exchange.Unbind(
            destination=exchange_destination,
            source=exchange_source,
            routing_key=routing_key,
            nowait=no_wait,
            arguments=arguments,
        )
        return (yield from self._write_frame_awaiting_response(
            'exchange_unbind', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def exchange_unbind_ok(self, frame):
        future = self._get_waiter('exchange_unbind')
        future.set_result(True)
        logger.debug("Exchange bound")

#
## Queue class implementation
#

    @asyncio.coroutine
    def queue_declare(self, queue_name=None, passive=False, durable=False,
                      exclusive=False, auto_delete=False, no_wait=False, arguments=None):
        """Create or check a queue on the broker
           Args:
               queue_name:     str, the queue to receive message from.
                               The server generate a queue_name if not specified.
               passive:        bool, if set, the server will reply with
                               Declare-Ok if the queue already exists with the same name, and
                               raise an error if not. Checks for the same parameter as well.
               durable:        bool: If set when creating a new queue, the queue
                               will be marked as durable. Durable queues remain active when a
               server restarts.
               exclusive:      bool, request exclusive consumer access,
                               meaning only this consumer can access the queue
               no_wait:        bool, if set, the server will not respond to the method
               arguments:      dict, AMQP arguments to be passed when creating
               the queue.
        """
        if arguments is None:
            arguments = {}

        if not queue_name:
            queue_name = ''
        request = pamqp.specification.Queue.Declare(
            queue=queue_name,
            passive=passive,
            durable=durable,
            exclusive=exclusive,
            auto_delete=auto_delete,
            nowait=no_wait,
            arguments=arguments
        )
        return (yield from self._write_frame_awaiting_response(
            'queue_declare', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def queue_declare_ok(self, frame):
        results = {
            'queue': frame.payload_decoder.read_shortstr(),
            'message_count': frame.payload_decoder.read_long(),
            'consumer_count': frame.payload_decoder.read_long(),
        }
        future = self._get_waiter('queue_declare')
        future.set_result(results)
        logger.debug("Queue declared")


    @asyncio.coroutine
    def queue_delete(self, queue_name, if_unused=False, if_empty=False, no_wait=False):
        """Delete a queue in RabbitMQ
            Args:
               queue_name:     str, the queue to receive message from
               if_unused:      bool, the queue is deleted if it has no consumers. Raise if not.
               if_empty:       bool, the queue is deleted if it has no messages. Raise if not.
               no_wait:        bool, if set, the server will not respond to the method
        """
        request = pamqp.specification.Queue.Delete(
            queue=queue_name,
            if_unused=if_unused,
            if_empty=if_empty,
            nowait=no_wait
        )
        return (yield from self._write_frame_awaiting_response(
            'queue_delete', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def queue_delete_ok(self, frame):
        future = self._get_waiter('queue_delete')
        future.set_result(True)
        logger.debug("Queue deleted")

    @asyncio.coroutine
    def queue_bind(self, queue_name, exchange_name, routing_key, no_wait=False, arguments=None):
        """Bind a queue and a channel."""
        if arguments is None:
            arguments = {}

        request = pamqp.specification.Queue.Bind(
            queue=queue_name,
            exchange=exchange_name,
            routing_key=routing_key,
            nowait=no_wait,
            arguments=arguments
        )
        # short reserved-1
        return (yield from self._write_frame_awaiting_response(
            'queue_bind', self.channel_id, request, no_wait))

    @asyncio.coroutine
    def queue_bind_ok(self, frame):
        future = self._get_waiter('queue_bind')
        future.set_result(True)
        logger.debug("Queue bound")

    @asyncio.coroutine
    def queue_unbind(self, queue_name, exchange_name, routing_key, arguments=None):
        if arguments is None:
            arguments = {}

        request = pamqp.specification.Queue.Unbind(
            queue=queue_name,
            exchange=exchange_name,
            routing_key=routing_key,
            arguments=arguments
        )

        return (yield from self._write_frame_awaiting_response(
            'queue_unbind', self.channel_id, request, no_wait=False))

    @asyncio.coroutine
    def queue_unbind_ok(self, frame):
        future = self._get_waiter('queue_unbind')
        future.set_result(True)
        logger.debug("Queue unbound")

    @asyncio.coroutine
    def queue_purge(self, queue_name, no_wait=False):
        request = pamqp.specification.Queue.Purge(
            queue=queue_name, nowait=no_wait
        )
        return (yield from self._write_frame_awaiting_response(
            'queue_purge', self.channel_id, request, no_wait=no_wait))

    @asyncio.coroutine
    def queue_purge_ok(self, frame):
        decoder = amqp_frame.AmqpDecoder(frame.payload)
        message_count = decoder.read_long()
        future = self._get_waiter('queue_purge')
        future.set_result({'message_count': message_count})

#
## Basic class implementation
#

    @asyncio.coroutine
    def basic_publish(self, payload, exchange_name, routing_key, properties=None, mandatory=False, immediate=False):
        if isinstance(payload, str):
            warnings.warn("Str payload support will be removed in next release", DeprecationWarning)
            payload = payload.encode()

        if properties is None:
            properties = {}

        method_request = pamqp.specification.Basic.Publish(
            exchange=exchange_name,
            routing_key=routing_key,
            mandatory=mandatory,
            immediate=immediate
        )

        yield from self._write_frame(self.channel_id, method_request, drain=False)

        header_request = pamqp.header.ContentHeader(
            body_size=len(payload),
            properties=pamqp.specification.Basic.Properties(**properties)
        )
        yield from self._write_frame(self.channel_id, header_request, drain=False)

        # split the payload

        frame_max = self.protocol.server_frame_max or len(payload)
        for chunk in (payload[0+i:frame_max+i] for i in range(0, len(payload), frame_max)):
            content_request = pamqp.body.ContentBody(chunk)
            yield from self._write_frame(self.channel_id, content_request, drain=False)

        yield from self.protocol._drain()

    @asyncio.coroutine
    def basic_qos(self, prefetch_size=0, prefetch_count=0, connection_global=False):
        """Specifies quality of service.

        Args:
            prefetch_size:      int, request that messages be sent in advance so
                                that when the client finishes processing a message, the
                                following message is already held locally
            prefetch_count:     int: Specifies a prefetch window in terms of
                                whole messages. This field may be used in combination with the
                                prefetch-size field; a message will only be sent in advance if
                                both prefetch windows (and those at the channel and connection
                                level) allow it
            connection_global:  bool: global=false means that the QoS
                                settings should apply per-consumer channel; and global=true to mean
                                that the QoS settings should apply per-channel.
        """
        request = pamqp.specification.Basic.Qos(
            prefetch_size, prefetch_count, connection_global
        )
        return (yield from self._write_frame_awaiting_response(
            'basic_qos', self.channel_id, request, no_wait=False)
        )

    @asyncio.coroutine
    def basic_qos_ok(self, frame):
        future = self._get_waiter('basic_qos')
        future.set_result(True)
        logger.debug("Qos ok")


    @asyncio.coroutine
    def basic_server_nack(self, frame, delivery_tag=None):
        if delivery_tag is None:
            decoder = amqp_frame.AmqpDecoder(frame.payload)
            delivery_tag = decoder.read_long_long()
        fut = self._get_waiter('basic_server_ack_{}'.format(delivery_tag))
        logger.debug('Received nack for delivery tag %r', delivery_tag)
        fut.set_exception(exceptions.PublishFailed(delivery_tag))

    @asyncio.coroutine
    def basic_consume(self, callback, queue_name='', consumer_tag='', no_local=False, no_ack=False,
                      exclusive=False, no_wait=False, arguments=None):
        """Starts the consumption of message into a queue.
        the callback will be called each time we're receiving a message.

            Args:
                callback:       coroutine, the called callback
                queue_name:     str, the queue to receive message from
                consumer_tag:   str, optional consumer tag
                no_local:       bool, if set the server will not send messages
                                to the connection that published them.
                no_ack:         bool, if set the server does not expect
                                acknowledgements for messages
                exclusive:      bool, request exclusive consumer access,
                                meaning only this consumer can access the queue
                no_wait:        bool, if set, the server will not respond to the method
                arguments:      dict, AMQP arguments to be passed to the server
        """
        # If a consumer tag was not passed, create one
        consumer_tag = consumer_tag or 'ctag%i.%s' % (self.channel_id, uuid.uuid4().hex)

        if arguments is None:
            arguments = {}

        request = pamqp.specification.Basic.Consume(
            queue=queue_name,
            consumer_tag=consumer_tag,
            no_local=no_local,
            no_ack=no_ack,
            exclusive=exclusive,
            nowait=no_wait,
            arguments=arguments
        )

        self.consumer_callbacks[consumer_tag] = callback
        self.last_consumer_tag = consumer_tag

        return_value = yield from self._write_frame_awaiting_response(
            'basic_consume', self.channel_id, request, no_wait)
        if no_wait:
            return_value = {'consumer_tag': consumer_tag}
        else:
            self._ctag_events[consumer_tag].set()
        return return_value

    @asyncio.coroutine
    def basic_consume_ok(self, frame):
        ctag = frame.payload_decoder.read_shortstr()
        results = {
            'consumer_tag': ctag,
        }
        future = self._get_waiter('basic_consume')
        future.set_result(results)
        self._ctag_events[ctag] = asyncio.Event(loop=self._loop)

    @asyncio.coroutine
    def basic_deliver(self, frame):
        response = amqp_frame.AmqpDecoder(frame.payload)
        consumer_tag = response.read_shortstr()
        delivery_tag = response.read_long_long()
        is_redeliver = response.read_bit()
        exchange_name = response.read_shortstr()
        routing_key = response.read_shortstr()
        content_header_frame = yield from self.protocol.get_frame()

        buffer = io.BytesIO()
        while(buffer.tell() < content_header_frame.body_size):
            content_body_frame = yield from self.protocol.get_frame()
            buffer.write(content_body_frame.payload)

        body = buffer.getvalue()
        envelope = Envelope(consumer_tag, delivery_tag, exchange_name, routing_key, is_redeliver)
        properties = content_header_frame.properties

        callback = self.consumer_callbacks[consumer_tag]

        event = self._ctag_events.get(consumer_tag)
        if event:
            yield from event.wait()
            del self._ctag_events[consumer_tag]

        yield from callback(self, body, envelope, properties)

    @asyncio.coroutine
    def server_basic_cancel(self, frame):
        # https://www.rabbitmq.com/consumer-cancel.html
        consumer_tag = frame.payload_decoder.read_shortstr()
        _no_wait = frame.payload_decoder.read_bit()
        self.cancelled_consumers.add(consumer_tag)
        logger.info("consume cancelled received")
        for callback in self.cancellation_callbacks:
            try:
                yield from callback(self, consumer_tag)
            except Exception as error:  # pylint: disable=broad-except
                logger.error("cancellation callback %r raised exception %r",
                             callback, error)

    @asyncio.coroutine
    def basic_cancel(self, consumer_tag, no_wait=False):
        request = pamqp.specification.Basic.Cancel(consumer_tag, no_wait)
        return (yield from self._write_frame_awaiting_response(
            'basic_cancel', self.channel_id, request, no_wait=no_wait)
        )

    @asyncio.coroutine
    def basic_cancel_ok(self, frame):
        results = {
            'consumer_tag': frame.payload_decoder.read_shortstr(),
        }
        future = self._get_waiter('basic_cancel')
        future.set_result(results)
        logger.debug("Cancel ok")

    @asyncio.coroutine
    def basic_get(self, queue_name='', no_ack=False):
        request = pamqp.specification.Basic.Get(queue=queue_name, no_ack=no_ack)
        return (yield from self._write_frame_awaiting_response(
            'basic_get', self.channel_id, request, no_wait=False)
        )

    @asyncio.coroutine
    def basic_get_ok(self, frame):
        data = {}
        decoder = amqp_frame.AmqpDecoder(frame.payload)
        data['delivery_tag'] = decoder.read_long_long()
        data['redelivered'] = bool(decoder.read_octet())
        data['exchange_name'] = decoder.read_shortstr()
        data['routing_key'] = decoder.read_shortstr()
        data['message_count'] = decoder.read_long()
        content_header_frame = yield from self.protocol.get_frame()

        buffer = io.BytesIO()
        while(buffer.tell() < content_header_frame.body_size):
            content_body_frame = yield from self.protocol.get_frame()
            buffer.write(content_body_frame.payload)

        data['message'] = buffer.getvalue()
        data['properties'] = content_header_frame.properties
        future = self._get_waiter('basic_get')
        future.set_result(data)

    @asyncio.coroutine
    def basic_get_empty(self, frame):
        future = self._get_waiter('basic_get')
        future.set_exception(exceptions.EmptyQueue)

    @asyncio.coroutine
    def basic_client_ack(self, delivery_tag, multiple=False):
        request = pamqp.specification.Basic.Ack(delivery_tag, multiple)
        yield from self._write_frame(self.channel_id, request)

    @asyncio.coroutine
    def basic_client_nack(self, delivery_tag, multiple=False, requeue=True):
        request = pamqp.specification.Basic.Nack(delivery_tag, multiple, requeue)
        yield from self._write_frame(self.channel_id, request)


    @asyncio.coroutine
    def basic_server_ack(self, frame):
        decoder = amqp_frame.AmqpDecoder(frame.payload)
        delivery_tag = decoder.read_long_long()
        fut = self._get_waiter('basic_server_ack_{}'.format(delivery_tag))
        logger.debug('Received ack for delivery tag %s', delivery_tag)
        fut.set_result(True)

    @asyncio.coroutine
    def basic_reject(self, delivery_tag, requeue=False):
        request = pamqp.specification.Basic.Reject(delivery_tag, requeue)
        yield from self._write_frame(self.channel_id, request)

    @asyncio.coroutine
    def basic_recover_async(self, requeue=True):
        request = pamqp.specification.Basic.RecoverAsync(requeue)
        yield from self._write_frame(self.channel_id, request)

    @asyncio.coroutine
    def basic_recover(self, requeue=True):
        request = pamqp.specification.Basic.Recover(requeue)
        return (yield from self._write_frame_awaiting_response(
            'basic_recover', self.channel_id, request, no_wait=False)
        )

    @asyncio.coroutine
    def basic_recover_ok(self, frame):
        future = self._get_waiter('basic_recover')
        future.set_result(True)
        logger.debug("Cancel ok")

    @asyncio.coroutine
    def basic_return(self, frame):
        response = amqp_frame.AmqpDecoder(frame.payload)
        reply_code = response.read_short()
        reply_text = response.read_shortstr()
        exchange_name = response.read_shortstr()
        routing_key = response.read_shortstr()
        content_header_frame = yield from self.protocol.get_frame()

        buffer = io.BytesIO()
        while buffer.tell() < content_header_frame.body_size:
            content_body_frame = yield from self.protocol.get_frame()
            buffer.write(content_body_frame.payload)

        body = buffer.getvalue()
        envelope = ReturnEnvelope(reply_code, reply_text,
                                  exchange_name, routing_key)
        properties = content_header_frame.properties
        callback = self.return_callback
        if callback is None:
            # they have set mandatory bit, but havent added a callback
            logger.warning('You have received a returned message, but dont have a callback registered for returns.'
                           ' Please set channel.return_callback')
        else:
            yield from callback(self, body, envelope, properties)


#
## convenient aliases
#
    queue = queue_declare
    exchange = exchange_declare

    @asyncio.coroutine
    def publish(self, payload, exchange_name, routing_key, properties=None, mandatory=False, immediate=False):
        if isinstance(payload, str):
            warnings.warn("Str payload support will be removed in next release", DeprecationWarning)
            payload = payload.encode()

        if properties is None:
            properties = {}

        if self.publisher_confirms:
            delivery_tag = next(self.delivery_tag_iter)  # pylint: disable=stop-iteration-return
            fut = self._set_waiter('basic_server_ack_{}'.format(delivery_tag))

        method_request = pamqp.specification.Basic.Publish(
            exchange=exchange_name,
            routing_key=routing_key,
            mandatory=mandatory,
            immediate=immediate
        )
        yield from self._write_frame(self.channel_id, method_request, drain=False)

        properties = pamqp.specification.Basic.Properties(**properties)
        header_request = pamqp.header.ContentHeader(
            body_size=len(payload), properties=properties
        )
        yield from self._write_frame(self.channel_id, header_request, drain=False)

        # split the payload

        frame_max = self.protocol.server_frame_max or len(payload)
        for chunk in (payload[0+i:frame_max+i] for i in range(0, len(payload), frame_max)):
            content_request = pamqp.body.ContentBody(chunk)
            yield from self._write_frame(self.channel_id, content_request, drain=False)

        yield from self.protocol._drain()

        if self.publisher_confirms:
            yield from fut

    @asyncio.coroutine
    def confirm_select(self, *, no_wait=False):
        if self.publisher_confirms:
            raise ValueError('publisher confirms already enabled')
        request = pamqp.specification.Confirm.Select(nowait=no_wait)

        return (yield from self._write_frame_awaiting_response(
            'confirm_select', self.channel_id, request, no_wait)
        )

    @asyncio.coroutine
    def confirm_select_ok(self, frame):
        self.publisher_confirms = True
        self.delivery_tag_iter = count(1)
        fut = self._get_waiter('confirm_select')
        fut.set_result(True)
        logger.debug("Confirm selected")

    def add_cancellation_callback(self, callback):
        """Add a callback that is invoked when a consumer is cancelled.

        :param callback: function to call

        `callback` is called with the channel and consumer tag as positional
        parameters.  The callback can be either a plain callable or an
        asynchronous co-routine.

        """
        self.cancellation_callbacks.append(callback)
