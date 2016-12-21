# Import from the Standard Library
from uuid import uuid4
from traceback import format_exc
import logging

# Import from Django
from django.conf import settings

# Other
import msgpack
from zmq_rpc.client import ZmqRpcClient

# Import from xbus
from .models import Event


registry = {}
logger = logging.getLogger(__name__)


def register_handler(event_type, handler):
    registry[event_type] = handler


def send_event(instance, event_type, item, immediate=False, admin_url=None):
    """
    Utility function used typically by signal handlers to send an xbus event.
    For now we only support sending 1 event per envelop, and 1 item per event.
    """
    # Identify the object and the message
    xbus_message_correlation_id = str(uuid4())

    # If the instance hasn't a xref, we create it
    if not instance.xref:
        instance.xref = str(uuid4())
        instance.save(update_fields=['xref', ])

    xref = str(instance.xref)

    # Fill item
    item['xref'] = xref
    item['xbus_message_correlation_id'] = xbus_message_correlation_id

    # The broker, written in Python 3, expects unicode for everything
    item = {unicode(k): unicode(v) if type(v) is str else v
            for k, v in item.items()}
    event_type = unicode(event_type)

    # Pack
    item = msgpack.packb(item)

    if immediate:
        direction = 'immediate-out'
    else:
        direction = 'out'

    # Add to the queue
    event = Event.objects.create(
        direction=direction,
        state='pending',
        xbus_message_correlation_id=xbus_message_correlation_id,
        xref=xref,
        event_type=event_type,
        item=item,
        admin_url=admin_url,
    )

    if immediate:
        try:
            success, reply, event_id = send_immediate_reply_event(event)
            event.event_id = event_id
            event.comment = (
                "Returned code: %s\nReturned val: %s" % (success, reply)
            )
            if success is True:
                event.state = 'done'

            event.save()

            return event, success, reply
        except Exception:
            event.state = 'error'
            event.comment = format_exc()
            event.save()
            return event, False, None

    return event


def new_connection_to_xbus():
    front_url = settings.XBUS_EMITTER_URL
    login = settings.XBUS_EMITTER_LOGIN
    password = settings.XBUS_EMITTER_PASSWORD

    conn = ZmqRpcClient(front_url, timeout=1000)
    token = conn.login(login, password)

    if not token:
        raise Exception('Error: Authentication failed.')

    return conn, token


def send_immediate_reply_event(event):
    conn, token = new_connection_to_xbus()
    return _xbus_send_event(conn, token, event)


def _xbus_send_event(conn, token, event):
    """
    Returns a tuple with three values:

    - success : True if the operation succeeded, False if not
    - reply   : returned value for immediate-reply, None otherwise
    - event_id: broker event-id, for debugging purpuses
    """

    event_type = event.event_type
    item = event.item

    # conn.packer.pack != msgpack.packb
    item = msgpack.unpackb(item, encoding='utf-8')
    item = conn.packer.pack(item)

    # Send
    logger.info(u'Sending event {event_type}'.format(event_type=event_type))
    envelope_id = conn.start_envelope(token)
    event_id = conn.start_event(token, envelope_id, event_type, 0)

    # In case of error event_id will be the empty string, otherwise an UUID
    assert bool(event_id), (
            "Error: the following event_type isn't registered with "
            "xbus or you might not have the right permissions to send "
            "it: %s" % event_type)

    reply = None
    success = conn.send_item(token, envelope_id, event_id, item)
    if success:
        success, reply = conn.end_event(token, envelope_id, event_id)
        if success:
            success = conn.end_envelope(token, envelope_id)

    return success, reply, event_id


def _send_healtcheck_event(item):
    """To send healtcheck"""
    conn, token = new_connection_to_xbus()
    event_type = getattr(
        settings, 'XBUS_EMITTER_HEALTCHECK', u'healthcheck_emitter')
    envelope_id = conn.start_envelope(token)
    event_id = conn.start_event(token, envelope_id, event_type, 0)
    assert bool(event_id), (
            "Error: the following event_type isn't registered with "
            "xbus or you might not have the right permissions to send "
            "it: %s" % event_type)

    logger.info(u'Send the message')
    reply = None
    success = conn.send_item(token, envelope_id, event_id, item)
    if success:
        success, reply = conn.end_event(token, envelope_id, event_id)

        if success:
            success = conn.end_envelope(token, envelope_id)

    return True, []
