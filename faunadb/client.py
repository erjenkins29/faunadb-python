from logging import DEBUG, getLogger, StreamHandler
from os import environ
from time import time

from requests import codes, Request, Session

from .errors import BadRequest, FaunaError, FaunaHTTPError, InternalError, MethodNotAllowed,\
  NotFound, PermissionDenied, Unauthorized, UnavailableError
from .objects import Ref
from ._json import parse_json, to_json

if environ.get("FAUNA_DEBUG"):
  _debug_logger = getLogger(__name__)
  _debug_logger.setLevel(DEBUG)
  _debug_logger.addHandler(StreamHandler())
else:
  _debug_logger = None

class Client(object):
  """
  Directly communicates with FaunaDB via JSON.

  For data sent to the server, the ``to_fauna_json`` method will be called on any values.
  It is encouraged to pass e.g. :any:`Ref` objects instead of raw JSON data.

  All methods return a converted JSON response.
  This is a dict containing lists, ints, floats, strings, and other dicts.
  Any :any:`Ref` or :any:`Set` values in it will also be parsed.
  (So instead of ``{ "@ref": "classes/frogs/123" }``, you will get ``Ref("classes/frogs", "123")``.)

  There is no way to automatically convert to any other type, such as :any:`Event`,
  from the response; you'll have to do that yourself manually.
  """

  # pylint: disable=too-many-arguments, too-many-instance-attributes
  def __init__(
      self,
      logger=None,
      domain="rest.faunadb.com",
      scheme="https",
      port=None,
      timeout=60,
      secret=None):
    """
    :param logger:
      A `Logger <https://docs.python.org/2/library/logging.html#logger-objects>`_.
      Will be called to log every request and response.
      Setting the ``FAUNA_DEBUG`` environment variable will also log to ``STDERR``.
    :param domain:
      Base URL for the FaunaDB server.
    :param scheme:
      ``"http"`` or ``"https"``.
    :param port:
      Port of the FaunaDB server.
    :param timeout:
      Read timeout in seconds.
    :param secret:
      Auth token for the FaunaDB server.
      May be a (username, password) tuple, or "username:password", or just the username (no colon).
    """

    self.logger = logger
    self.domain = domain
    self.scheme = scheme
    self.port = (443 if scheme == "https" else 80) if port is None else port

    self.session = Session()
    if secret is not None:
      self.session.auth = Client._parse_secret(secret)

    self.session.headers.update({
      "Accept-Encoding": "gzip",
      "Content-Type": "application/json;charset=utf-8"
    })
    self.session.timeout = timeout

    self.base_url = "%s://%s:%s" % (self.scheme, self.domain, self.port)

  def get(self, path, query=None):
    """
    HTTP ``GET``.
    See the `docs <https://faunadb.com/documentation/rest>`__.

    :param path: Path relative to ``self.domain``. May be a Ref.
    :param query: Dict to be converted to URL parameters.
    :return: Converted JSON response.
    """
    return self._execute("GET", path, query=query)

  def post(self, path, data=None):
    """
    HTTP ``POST``.
    See the `docs <https://faunadb.com/documentation/rest>`__.

    :param path: Path relative to ``self.domain``. May be a Ref.
    :param data:
      Dict to be converted to request JSON.
      Values in this will have ``to_fauna_json`` called, recursively.
    :return: Converted JSON response.
    """
    return self._execute("POST", path, data)

  def put(self, path, data=None):
    """
    Like :any:`post`, but a ``PUT`` request.
    See the `docs <https://faunadb.com/documentation/rest>`__.
    """
    return self._execute("PUT", path, data)

  def patch(self, path, data=None):
    """
    Like :any:`post`, but a ``PATCH`` request.
    See the `docs <https://faunadb.com/documentation/rest>`__.
    """
    return self._execute("PATCH", path, data)

  def delete(self, path, data=None):
    """
    Like :any:`post`, but a ``DELETE`` request.
    See the `docs <https://faunadb.com/documentation/rest>`__.
    """
    return self._execute("DELETE", path, data)

  def query(self, expression):
    """
    Use the FaunaDB query API.
    See :doc:query.

    :param expression: Dict generated by functions in faunadb.query.
    :return: Converted JSON response.
    """
    return self._execute("POST", "", expression)

  def ping(self, scope=None, timeout=None):
    """
    Ping FaunaDB.
    See the `docs <https://faunadb.com/documentation/rest#other>`__.
    """
    return self.get("ping", {"scope": scope, "timeout": timeout})

  def _log(self, indented, logged):
    """Indents `logged` before sending it to self.logger."""
    if indented:
      indent_str = "  "
      logged = indent_str + ("\n" + indent_str).join(logged.split("\n"))

    if _debug_logger:
      _debug_logger.debug(logged)
    if self.logger:
      self.logger.debug(logged)

  def _execute(self, action, path, data=None, query=None):
    """Performs an HTTP action, logs it, and looks for errors."""
    # pylint: disable=too-many-branches

    if isinstance(path, Ref):
      path = path.value
    if query is not None:
      query = {k: v for k, v in query.iteritems() if v is not None}

    if self.logger is not None or _debug_logger is not None:
      self._log(False, "Fauna %s /%s%s" % (action, path, Client._query_string_for_logging(query)))

      if self.session.auth is None:
        self._log(True, "Credentials: None")
      else:
        self._log(True, "Credentials: %s:%s" % self.session.auth)

      if data:
        self._log(True, "Request JSON: %s" % to_json(data, pretty=True))

      real_time_begin = time()
      response = self._execute_without_logging(action, path, data, query)
      real_time_end = time()
      real_time = real_time_end - real_time_begin

      # response.headers a CaseInsensitiveDict, which can't be converted to JSON directly
      headers_json = to_json(dict(response.headers), pretty=True)
      response_dict = parse_json(response.text)
      response_json = to_json(response_dict, pretty=True)
      self._log(True, "Response headers: %s" % headers_json)
      self._log(True, "Response JSON: %s" % response_json)
      self._log(
        True,
        "Response (%i): API processing %sms, network latency %ims" % (
          response.status_code,
          response.headers.get("X-HTTP-Request-Processing-Time", "N/A"),
          int(real_time * 1000)))
      return Client._handle_response(response, response_dict)
    else:
      response = self._execute_without_logging(action, path, data, query)
      response_dict = parse_json(response.text)
      return Client._handle_response(response, response_dict)

  def _execute_without_logging(self, action, path, data, query):
    """Performs an HTTP action."""
    url = self.base_url + "/" + path
    req = Request(action, url, params=query, data=to_json(data))
    return self.session.send(self.session.prepare_request(req))

  @staticmethod
  def _handle_response(response, response_dict):
    """Looks for error codes in response. If not, parses it."""
    # pylint: disable=no-member
    code = response.status_code
    if 200 <= code <= 299:
      return response_dict["resource"]
    elif code == codes.bad_request:
      raise BadRequest(response_dict)
    elif code == codes.unauthorized:
      raise Unauthorized(response_dict)
    elif code == codes.forbidden:
      raise PermissionDenied(response_dict)
    elif code == codes.not_found:
      raise NotFound(response_dict)
    elif code == codes.method_not_allowed:
      raise MethodNotAllowed(response_dict)
    elif code == codes.internal_server_error:
      raise InternalError(response_dict)
    elif code == codes.unavailable:
      raise UnavailableError(response_dict)
    else:
      raise FaunaHTTPError(response_dict)

  @staticmethod
  def _query_string_for_logging(query):
    """Converts a query dict to URL params."""
    if not query:
      return ""
    return "?" + "&".join(("%s=%s" % (k, v) for k, v in query.iteritems()))

  @staticmethod
  def _parse_secret(secret):
    if isinstance(secret, tuple):
      if len(secret) != 2:
        raise FaunaError("Secret tuple must have exactly two entries")
      return secret
    else:
      pair = secret.split(":", 1)
      if len(pair) == 1:
        pair.append("")
      return tuple(pair)
