#  Copyright 2008-2014 Nokia Solutions and Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

__version__ = 'devel'

import re
import sys
import inspect
import traceback
from StringIO import StringIO
from SimpleXMLRPCServer import SimpleXMLRPCServer
from xmlrpclib import Binary
try:
    import signal
except ImportError:
    signal = None
try:
    from collections import Mapping
except ImportError:
    Mapping = dict


BINARY = re.compile('[\x00-\x08\x0B\x0C\x0E-\x1F]')


class RobotRemoteServer(SimpleXMLRPCServer):
    allow_reuse_address = True
    _generic_exceptions = (AssertionError, RuntimeError, Exception)
    _fatal_exceptions = (SystemExit, KeyboardInterrupt)

    def __init__(self, library, host='127.0.0.1', port=8270, port_file=None,
                 allow_stop=True):
        SimpleXMLRPCServer.__init__(self, (host, int(port)), logRequests=False)
        self._library = library
        self._allow_stop = allow_stop
        self._shutdown = False
        self._register_functions()
        self._register_signal_handlers()
        self._announce_start(port_file)
        self.serve_forever()

    def _register_functions(self):
        self.register_function(self.get_keyword_names)
        self.register_function(self.run_keyword)
        self.register_function(self.get_keyword_arguments)
        self.register_function(self.get_keyword_documentation)
        self.register_function(self.stop_remote_server)

    def _register_signal_handlers(self):
        def stop_with_signal(signum, frame):
            self._allow_stop = True
            self.stop_remote_server()
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, stop_with_signal)
        if hasattr(signal, 'SIGINT'):
            signal.signal(signal.SIGINT, stop_with_signal)

    def _announce_start(self, port_file=None):
        host, port = self.server_address
        self._log('Robot Framework remote server starting at %s:%s.'
                  % (host, port))
        if port_file:
            pf = open(port_file, 'w')
            try:
                pf.write(str(port))
            finally:
                pf.close()

    def serve_forever(self):
        while not self._shutdown:
            self.handle_request()

    def stop_remote_server(self):
        prefix = 'Robot Framework remote server at %s:%s ' % self.server_address
        if self._allow_stop:
            self._log(prefix + 'stopping')
            self._shutdown = True
        else:
            self._log(prefix + 'does not allow stopping', 'WARN')
        return True

    def get_keyword_names(self):
        get_kw_names = getattr(self._library, 'get_keyword_names', None) or \
                       getattr(self._library, 'getKeywordNames', None)
        if inspect.isroutine(get_kw_names):
            names = get_kw_names()
        else:
            names = [attr for attr in dir(self._library) if attr[0] != '_'
                     and inspect.isroutine(getattr(self._library, attr))]
        return names + ['stop_remote_server']

    def run_keyword(self, name, args, kwargs=None):
        args, kwargs = self._handle_binary_args(args, kwargs or {})
        result = {'return': '', 'output': '', 'error': '', 'traceback': ''}
        self._intercept_stdout()
        try:
            return_value = self._get_keyword(name)(*args, **kwargs)
        except:
            result['status'] = 'FAIL'
            result['error'], result['traceback'] = self._get_error_details()
        else:
            result['status'] = 'PASS'
            result['return'] = self._handle_return_value(return_value)
        result['output'] = self._restore_stdout()
        return result

    def _handle_binary_args(self, args, kwargs):
        args = [self._handle_binary_arg(a) for a in args]
        kwargs = dict([(k, self._handle_binary_arg(v)) for k, v in kwargs.items()])
        return args, kwargs

    def _handle_binary_arg(self, arg):
        return arg if not isinstance(arg, Binary) else str(arg)

    def get_keyword_arguments(self, name):
        kw = self._get_keyword(name)
        if not kw:
            return []
        return self._arguments_from_kw(kw)

    def _arguments_from_kw(self, kw):
        args, varargs, kwargs, defaults = inspect.getargspec(kw)
        if inspect.ismethod(kw):
            args = args[1:]  # drop 'self'
        if defaults:
            args, names = args[:-len(defaults)], args[-len(defaults):]
            args += ['%s=%s' % (n, d) for n, d in zip(names, defaults)]
        if varargs:
            args.append('*%s' % varargs)
        if kwargs:
            args.append('**%s' % kwargs)
        return args

    def get_keyword_documentation(self, name):
        if name == '__intro__':
            return inspect.getdoc(self._library) or ''
        if name == '__init__' and inspect.ismodule(self._library):
            return ''
        return inspect.getdoc(self._get_keyword(name)) or ''

    def _get_keyword(self, name):
        if name == 'stop_remote_server':
            return self.stop_remote_server
        kw = getattr(self._library, name, None)
        if inspect.isroutine(kw):
            return kw
        return None

    def _get_error_details(self):
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type in self._fatal_exceptions:
            self._restore_stdout()
            raise
        return (self._get_error_message(exc_type, exc_value),
                self._get_error_traceback(exc_tb))

    def _get_error_message(self, exc_type, exc_value):
        name = exc_type.__name__
        message = self._get_message_from_exception(exc_value)
        if not message:
            return name
        if exc_type in self._generic_exceptions:
            return message
        return '%s: %s' % (name, message)

    def _get_message_from_exception(self, value):
        # UnicodeError occurs below 2.6 and if message contains non-ASCII bytes
        try:
            msg = unicode(value)
        except UnicodeError:
            # TODO: This fails if args contain non-strings
            msg = ' '.join([unicode(a, errors='replace') for a in value.args])
        return self._handle_binary_result(msg)

    def _get_error_traceback(self, exc_tb):
        # Latest entry originates from this class so it can be removed
        entries = traceback.extract_tb(exc_tb)[1:]
        trace = ''.join(traceback.format_list(entries))
        return 'Traceback (most recent call last):\n' + trace

    def _handle_return_value(self, ret):
        if isinstance(ret, basestring):
            return self._handle_binary_result(ret)
        if isinstance(ret, (int, long, float)):
            return ret
        if isinstance(ret, Mapping):
            return dict([(self._str(key), self._handle_return_value(value))
                         for key, value in ret.items()])
        try:
            return [self._handle_return_value(item) for item in ret]
        except TypeError:
            return self._str(ret)

    def _handle_binary_result(self, result):
        if not BINARY.search(result):
            # TODO: Clean-up. This isn't the right place to do this.
            # Or at least this method is badly named.
            if isinstance(result, str):
                result = unicode(result, errors='replace')
            return result
        try:
            result = str(result)
        except UnicodeError:
            # TODO: Is this tested anywhere?
            raise ValueError("Cannot represent %r as binary." % result)
        return Binary(result)

    def _str(self, item):
        if item is None:
            return ''
        if not isinstance(item, basestring):
            item = unicode(item)
        return self._handle_binary_result(item)

    def _intercept_stdout(self):
        # TODO: What about stderr?
        sys.stdout = StringIO()

    def _restore_stdout(self):
        output = sys.stdout.getvalue()
        sys.stdout.close()
        sys.stdout = sys.__stdout__
        return self._handle_binary_result(output)

    def _log(self, msg, level=None):
        if level:
            msg = '*%s* %s' % (level.upper(), msg)
        sys.stdout.write(msg + '\n')
        sys.stdout.flush()
