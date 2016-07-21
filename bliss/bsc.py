'''
BLISS Binary Stream Capturer

The bliss.bsc module handles logging of network data to PCAP files
along with the server definition for RESTful manipulation of running
loggers.
'''

'''
Copyright 2008 California Institute of Technology.  ALL RIGHTS RESERVED.
U.S. Government Sponsorship acknowledged.
'''

import gevent.monkey
gevent.monkey.patch_all()

import calendar
import json
import os
import platform
import socket
import time

from bliss import pcap, log

import gevent
import gevent.pool
import gevent.socket

from bottle import run, request, Bottle

RAW_SOCKET_FD = None
try:
    import rawsocket
    RAW_SOCKET_FD = rawsocket.rawsocket_fd()
except ImportError:
    msg = (
        'The rawsocket library cannot be imported. '
        'Defaulting to the non-rawsocket approach.'
    )
    log.info(msg)
except IOError:
    msg = (
        'Unable to spawn rawsocket-helper. '
        'This may be a permissions issue (not SUID root?). '
        'Defaulting to non-rawsocket approach.'
    )
    log.info(msg)

ETH_P_IP = 0x0800
ETH_P_ALL = 0x0003
ETH_PROTOCOL = ETH_P_ALL

class SocketStreamCapturer(object):
    ''' Class for logging socket data to a PCAP file. '''

    def __init__(self, capture_handlers, address, conn_type):
        if type(capture_handlers) != type(list()):
            capture_handlers = [capture_handlers]

        self.capture_handlers = capture_handlers
        for h in self.capture_handlers:
            h['reads'] = 0
            h['data_read'] = 0

        self.conn_type = conn_type
        self.address = address

        if conn_type == 'udp':
            self.socket = gevent.socket.socket(gevent.socket.AF_INET,
                                               gevent.socket.SOCK_DGRAM)
            self.socket.bind((address[0], address[1]))
            # TODO: Make this configurable
            self._buffer_size = 65565
        elif conn_type == 'ethernet':
            socket_family = getattr(gevent.socket, 'AF_PACKET', gevent.socket.AF_INET)
            if RAW_SOCKET_FD:
                self.socket = gevent.socket.fromfd(RAW_SOCKET_FD,
                                                   socket_family,
                                                   gevent.socket.SOCK_RAW,
                                                   socket.htons(ETH_PROTOCOL))
            else:
                self.socket = gevent.socket.socket(socket_family,
                                                   gevent.socket.SOCK_RAW,
                                                   socket.htons(ETH_PROTOCOL))

            self.socket.bind((address[0], address[1]))
            self._buffer_size = 1518
        elif conn_type == 'tcp':
            self.socket = gevent.socket.socket(gevent.socket.AF_INET,
                                               gevent.socket.SOCK_STREAM)
            self.socket.connect((address[0], address[1]))
            # TODO: Make this configurable
            self._buffer_size = 65565

        self._init_log_file_handlers()

    @property
    def handler_count(self):
        return len(self.capture_handlers)

    def capture_packet(self):
        ''' Write packet data to the logger's log file. '''
        data = self.socket.recv(self._buffer_size)

        for h in self.capture_handlers:
            h['reads'] += 1
            h['data_read'] += len(data)

            d = data
            if 'pre_write_transforms' in h:
                for data_transform in h['pre_write_transforms']:
                    d = data_transform(d)
            h['logger'].write(d)

    def clean_up(self):
        ''' Clean up the socket and log file handles. '''
        self.socket.close()
        for h in self.capture_handlers:
            h['logger'].close()

    def socket_monitor_loop(self):
        ''' Monitor the socket and log captured data. '''
        try:
            while True:
                gevent.socket.wait_read(self.socket.fileno())

                self._handle_log_rotations()
                self.capture_packet()
        finally:
            self.clean_up()

    def add_handler(self, handler):
        ''''''
        handler['logger'] = self._get_logger(handler)
        handler['reads'] = 0
        handler['data_read'] = 0

        self.capture_handlers.append(handler)

    def remove_handler(self, name):
        ''''''
        index = None
        for i, h in enumerate(self.capture_handlers):
            if h['name'] == name:
                index = i

        if index is not None:
            self.capture_handlers[index]['logger'].close()
            del self.capture_handlers[index]

    def dump_handler_config_data(self):
        ''' Return capture handler configuration data.

        Return a dictionary of capture handler configuration data of the form:

            [{
                'handler': handler configuration dictionary,
                'log_file_path': Path to the current log file that the logger
                    is writing. Note that if rotation is used it's possible
                    this data will be stale eventually.
                'conn_type': The string defining the connection type of the
                    logger.
                'address': The list containing address info that the logger is
                    using for its connection.
            }, ...]
        '''
        ignored_keys = ['logger', 'log_rot_time', 'reads', 'data_read']
        config_data = []
        for h in self.capture_handlers:
            config_data.append({
                'handler': {
                    k:v for k,v in h.iteritems()
                    if k not in ignored_keys
                },
                'log_file_path': h['logger']._stream.name,
                'conn_type': self.conn_type,
                'address': self.address,
            })
        return config_data

    def dump_all_handler_stats(self):
        stats = []
        for h in self.capture_handlers:
            now = calendar.timegm(time.gmtime())
            rot_time = calendar.timegm(h['log_rot_time'])
            time_delta = now - rot_time
            approx_data_rate = '{} bytes/second'.format(h['data_read'] / float(time_delta))

            stats.append({
                'name': h['name'],
                'reads': h['reads'],
                'data_read_length': '{} bytes'.format(h['data_read']),
                'approx_data_rate': approx_data_rate
            })

        return stats

    def _handle_log_rotations(self):
        ''''''
        for h in self.capture_handlers:
            if self._should_rotate_log(h):
                self._rotate_log(h)

    def _should_rotate_log(self, handler):
        if handler['rotate_log']:
            rotate_time_index = handler.get('rotate_log_index', 'day')
            try:
                rotate_time_index = self._decode_time_rotation_index(rotate_time_index)
            except ValueError:
                rotate_time_index = 2

            rotate_time_delta = handler.get('rotate_log_delta', 1)

            time_delta = time.gmtime()[rotate_time_index] - handler['log_rot_time'][rotate_time_index]
            return time_delta >= rotate_time_delta

        return False

    def _decode_time_rotation_index(self, time_rot_index):
        ''''''
        time_index_decode_table = {
            'year': 0,    'years': 0,    'tm_year': 0,
            'month': 1,   'months': 1,   'tm_mon': 1,
            'day': 2,     'days': 2,     'tm_mday': 2,
            'hour': 3,    'hours': 3,    'tm_hour': 3,
            'minute': 4,  'minutes': 4,  'tm_min': 4,
            'second': 5,  'seconds': 5,  'tm_sec': 5,
        }

        if time_rot_index not in time_index_decode_table.keys():
            raise ValueError('Invalid time option specified for log rotation')

        return time_index_decode_table[time_rot_index]

    def _rotate_log(self, handler):
        ''''''
        handler['logger'].close()
        handler['logger'] = self._get_logger(handler)

    def _get_log_file(self, handler):
        ''''''
        if 'file_name_pattern' not in handler:
            filename = '%Y-%m-%d-%H-%M-%S-{name}.pcap'
        else:
            filename = handler['file_name_pattern']

        log_file = handler['log_dir']
        if 'path' in handler:
            log_file = os.path.join(log_file, handler['path'], filename)
        else:
            log_file = os.path.join(log_file, filename)

        log_file = time.strftime(log_file, time.gmtime())
        log_file = log_file.format(**handler)

        return log_file

    def _get_logger(self, handler):
        ''' Initialize a PCAP stream for logging data. '''
        log_file = self._get_log_file(handler)

        if not os.path.isdir(os.path.dirname(log_file)):
            os.makedirs(os.path.dirname(log_file))

        handler['log_rot_time'] = time.gmtime()
        return pcap.open(log_file, mode='a')

    def _init_log_file_handlers(self):
        for handler in self.capture_handlers:
            handler['logger'] = self._get_logger(handler)


class StreamCaptureManager(object):
    def __init__(self, mngr_conf, lgr_conf):
        '''
        Args:
            mngr_conf: Configuration dictionary for the manager. At
                the minimum this should contain the following:

                    {
                        'root_log_directory': <Root directory for log data>
                    }

            lgr_conf: Configuration data for all the logger instances that
                should be created by default. Additional information on
                parameters that are required for logger initialization can be
                found in :func:`add_logger`. Data should be of the form:

                    [
                        (name, address, conn_type, log_dir_path, misc_conf_dict),
                        (name, address, conn_type, log_dir_path, misc_conf_dict),
                    ]
        '''
        self._logger_data = {}
        self._stream_capturers = {}
        self._pool = gevent.pool.Pool(50)
        self._mngr_conf = mngr_conf

        #TODO: Remove this kwargs passing if not going to add more options
        #TODO: Abstract this out to a function call to handle conf parsing?
        for name, address, conn_type, log_dir_path, misc_conf in lgr_conf:
            self.add_logger(name, address, conn_type, log_dir_path, **misc_conf)

    def add_logger(self, name, address, conn_type, log_dir_path=None, **kwargs):
        ''' Add a new stream capturer to the manager.

        Add a new stream capturer to the manager with the provided configuration
        details. If an existing capturer is monitoring the same address the
        new handler will be added to it.

        Args:
            name: A string defining the new capturer's name.
            address: A tuple containing address data for the capturer. Check the
                :class:`SocketStreamCapturer` documentation for what is required.
            conn_type: A string defining the connection type. Check the
                :class:`SocketStreamCapturer` documentation for a list of valid options.
            log_dir_path: An optional path defining the directory where the
                capturer should write its files. If this isn't provided the root
                log directory from the manager configuration is used.

        '''
        capture_handler_conf = kwargs

        if not log_dir_path:
            log_dir_path = self._mngr_conf['root_log_directory']

        log_dir_path = os.path.normpath(os.path.expanduser(log_dir_path))

        capture_handler_conf['log_dir'] = log_dir_path
        capture_handler_conf['name'] = name
        if 'rotate_log' not in capture_handler_conf:
            capture_handler_conf['rotate_log'] = True

        transforms = []
        if 'pre_write_transforms' in capture_handler_conf:
            for transform in capture_handler_conf['pre_write_transforms']:
                if type(transform) == type(''):
                    if globals().has_key(transform):
                        transforms.append(globals().get(transform))
                    else:
                        msg = 'Unable to load data transformation "{}" for handler "{}"'.format(
                            transform,
                            capture_handler_conf['name']
                        )
                        log.warn(msg)
                elif hasattr(transform, '__call__'):
                    transforms.append(transform)
                else:
                    msg = 'Unable to determine how to load data transform "{}"'.format(transform)
                    log.warn(msg)
        capture_handler_conf['pre_write_transforms'] = transforms

        address_key = str(address)
        if address_key in self._stream_capturers:
            capturer = self._stream_capturers[address_key][0]
            capturer.add_handler(capture_handler_conf)
            return

        socket_logger = SocketStreamCapturer(capture_handler_conf, address, conn_type)
        greenlet = gevent.spawn(socket_logger.socket_monitor_loop)

        self._stream_capturers[address_key] = (
            socket_logger,
            greenlet
        )
        self._pool.add(greenlet)

    def stop_capture_handler(self, name):
        ''' Remove all handlers with a given name. '''
        empty_capturers_indeces = []
        for k, sc in self._stream_capturers.iteritems():
            stream_capturer = sc[0]
            stream_capturer.remove_handler(name)

            if stream_capturer.handler_count == 0:
                self._pool.killone(sc[1])
                empty_capturers_indeces.append(k)

        for i in empty_capturers_indeces:
            del self._stream_capturers[i]

    def stop_stream_capturer(self, address):
        ''' Stop a capturer that the manager controls.

        Args:
            address: An address array of the form ['host', 'port'] or similar
                depending on the connection type of the stream capturer being
                terminated. The capturer for the address will be terminated
                along with all handlers for that capturer if the address is
                that of a managed capturer.

        Raises:
            ValueError: The provided address doesn't match a capturer that is
                currently managed.
        '''
        address = str(address)
        if address not in self._stream_capturers:
            raise ValueError('Capturer address does not match a managed capturer')

        stream_cap = self._stream_capturers[address]
        self._pool.killone(stream_cap[1])
        del self._stream_capturers[address]

    def rotate_capture_handler_log(self, name):
        ''' Force a rotation of a handler's log file

        Args:
            name: The name of the handler who's log file should be rotated.
        '''
        for sc_key, sc in self._stream_capturers.iteritems():
            for h in sc[0].capture_handlers:
                if h['name'] == name:
                    sc[0]._rotate_log(h)

    def get_logger_data(self):
        ''' Return data on managed loggers.

        Returns a dictionary of managed logger configuration data. The format
        is primarily controlled by the :func:`SocketStreamCapturer.get_logger_data`
        function:

            {
                <capture address>: <list of handler config for data capturers>
            }
        '''
        return {
            address : stream_capturer[0].dump_handler_config_data()
            for address, stream_capturer in self._stream_capturers.iteritems()
        }

    def get_handler_stats(self):
        ''' Return handler read statistics

        Returns a dictionary of managed handler data read statistics. The format
        is primarily controlled by the :func:`SocketStreamCapturer.dump_all_handler_stats`
        function:

            {
                <capture address>: <list of handler capture statistics>
            }
        '''
        return {
            address : stream_capturer[0].dump_all_handler_stats()
            for address, stream_capturer in self._stream_capturers.iteritems()
        }

    def get_capture_handler_config_by_name(self, name):
        ''' Return data for handlers of a given name.

        Args:
            name: Name of the capture handler(s) to return config data for.

        Returns:
            Dictionary dump from the named capture handler as given by
            the meth:`SocketStreamCapturer.dump_handler_config_data method`.
        '''
        handler_confs = []
        for address, stream_capturer in self._stream_capturers.iteritems():
            handler_data = stream_capturer[0].dump_handler_config_data()
            for h in handler_data:
                if h['handler']['name'] == name:
                    handler_confs.append(h)

        return handler_confs

    def run_socket_event_loop(self):
        ''' Start monitoring managed loggers. '''
        try:
            while True:
                self._pool.join()

                # If we have no loggers we'll sleep briefly to ensure that we
                # allow other processes (I.e., the webserver) to do their work.
                if len(self._logger_data.keys()) == 0:
                    time.sleep(0.5)

        except KeyboardInterrupt:
            pass
        finally:
            self._pool.kill()


class StreamCaptureManagerServer(Bottle):
    ''' Webserver for management of Binary Stream Capturers. '''

    def __init__(self, logger_manager, host, port):
        '''
        Args:
            logger_manager: Instance of :class:`StreamCaptureManager` which the
                server will use to manage logger instances.
            host: The host for webserver configuration.
            port: The port for webserver configuration.
        '''
        self._host = host
        self._port = port
        self._logger_manager = logger_manager
        self._app = Bottle()
        self._route()

    def start(self):
        ''' Starts the server. '''
        self._app.run(host=self._host, port=self._port)

    def _route(self):
        ''' Handles server route instantiation. '''
        self._app.route('/',
                        method='GET',
                        callback=self._get_logger_list)
        self._app.route('/stats',
                        method='GET',
                        callback=self._fetch_handler_stats)
        self._app.route('/<name>/start',
                        method='POST',
                        callback=self._add_logger_by_name)
        self._app.route('/<name>/stop',
                        method='DELETE',
                        callback=self._stop_logger_by_name)
        self._app.route('/<name>/config',
                        method='GET',
                        callback=self._get_logger_conf)
        self._app.route('/<name>/rotate',
                        method='POST',
                        callback=self._rotate_capturer_log)

    def _add_logger_by_name(self, name):
        ''' Handles GET requests for adding a new logger.

        Expects logger configuration to be passed in the request's query string.
        The logger name is included in the URL and the address components and
        connection type should be included as well. The loc attribute is
        defaulted to "localhost" when making the socket connection if not
        defined.

        loc = IP / interface
        port = port / protocol
        conn_type = udp or ethernet

        Raises: ValueError if the port or connection type are not supplied.
        '''
        data = dict(request.forms)
        loc = data.pop('loc', '')
        port = data.pop('port', None)
        conn_type = data.pop('conn_type', None)

        if not port or not conn_type:
            e = 'Port and/or conn_type not set'
            raise ValueError(e)
        address = [loc, int(port)]

        if 'rotate_log' in data:
            data['rotate_log'] = True if data == 'true' else False

        self._logger_manager.add_logger(name, address, conn_type, **data)

    def _stop_logger_by_name(self, name):
        ''' Handles requests for termination of a handler by name '''
        self._logger_manager.stop_capture_handler(name)

    def _get_logger_list(self):
        ''' Retrieves a JSON object of running handler information.

        Returns a JSON object containing config data for all the currently
        running loggers. Structure of the JSON object is controlled by the
        form of the dictionary returned from
        :func:`StreamCaptureManager.get_logger_data`
        '''
        return json.dumps(self._logger_manager.get_logger_data())

    def _get_logger_conf(self, name):
        ''' Retrieves a config for loggers matching a given name.

        Note that there isn't a requirement that capture handles have unique
        names. This will return all handlers with a matching name in the event
        that there is more than one. If the name doesn't match you will get
        an empty JSON object.
        '''
        return json.dumps(self._logger_manager.get_capture_handler_config_by_name(name))

    def _rotate_capturer_log(self, name):
        ''' Trigger log rotation for a given handler name.

        Note that if the file name pattern provided isn't sufficient for
        a rotation to occur with a new unique file name you will not see
        a log rotation . Be sure to timestamp your files in such a way
        to ensure that this isn't the case! The default file name pattern
        includes year, month, day, hours, minutes, and seconds to make sure
        this works as expected.
        '''
        self._logger_manager.rotate_capture_handler_log(name)

    def _fetch_handler_stats(self):
        ''' Retrieves a JSON object of running handler stats

        Returns a JSON object containing data read statistics for all
        running handlers. Structure of the JOSN objects is controlled by
        :func:`StreamCaptureManager.dump_all_handler_stats`.
        '''
        return json.dumps(self._logger_manager.get_handler_stats())

def identity_transform(data):
    '''Example data transformation function for a capture handler.'''
    return data
