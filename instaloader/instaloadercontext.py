import json
import pickle
import random
import shutil
import sys
import textwrap
import time
import urllib.parse
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

import requests
import requests.utils
import urllib3

from .exceptions import *

GRAPHQL_PAGE_LENGTH = 200


def copy_session(session: requests.Session) -> requests.Session:
    """Duplicates a requests.Session."""
    new = requests.Session()
    new.cookies = requests.utils.cookiejar_from_dict(requests.utils.dict_from_cookiejar(session.cookies))
    new.headers = session.headers.copy()
    return new


def default_user_agent() -> str:
    return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ' \
           '(KHTML, like Gecko) Chrome/51.0.2704.79 Safari/537.36'


class InstaloaderContext:
    """Class providing methods for (error) logging and low-level communication with Instagram.

    It is not thought to be instantiated directly, rather :class:`Instaloader` instances maintain a context
    object.

    For logging, it provides :meth:`log`, :meth:`error`, :meth:`error_catcher`.

    It provides low-level communication routines :meth:`get_json`, :meth:`graphql_query`, :meth:`graphql_node_list`,
    :meth:`get_and_write_raw` and implements mechanisms for rate controlling and error handling.

    Further, it provides methods for logging in and general session handles, which are used by that routines in
    class :class:`Instaloader`.
    """

    def __init__(self, sleep: bool = True, quiet: bool = False,
                 user_agent: Optional[str] = None, max_connection_attempts: int = 3):

        self.user_agent = user_agent if user_agent is not None else default_user_agent()
        self._session = self.get_anonymous_session()
        self.username = None
        self.sleep = sleep
        self.quiet = quiet
        self.max_connection_attempts = max_connection_attempts

        # error log, filled with error() and printed at the end of Instaloader.main()
        self.error_log = []

        # For the adaption of sleep intervals (rate control)
        self.previous_queries = dict()

        # Can be set to True for testing, disables supression of InstaloaderContext._error_catcher
        self.raise_all_errors = False

    @property
    def is_logged_in(self) -> bool:
        """True, if this Instaloader instance is logged in."""
        return bool(self.username)

    def log(self, *msg, sep='', end='\n', flush=False):
        """Log a message to stdout that can be suppressed with --quiet."""
        if not self.quiet:
            print(*msg, sep=sep, end=end, flush=flush)

    def error(self, msg, repeat_at_end=True):
        """Log a non-fatal error message to stderr, which is repeated at program termination.

        :param msg: Message to be printed.
        :param repeat_at_end: Set to false if the message should be printed, but not repeated at program termination."""
        print(msg, file=sys.stderr)
        if repeat_at_end:
            self.error_log.append(msg)

    def close(self):
        """Print error log and close session"""
        if self.error_log and not self.quiet:
            print("\nErrors occured:", file=sys.stderr)
            for err in self.error_log:
                print(err, file=sys.stderr)
        self._session.close()

    @contextmanager
    def error_catcher(self, extra_info: Optional[str] = None):
        """
        Context manager to catch, print and record InstaloaderExceptions.

        :param extra_info: String to prefix error message with."""
        try:
            yield
        except InstaloaderException as err:
            if extra_info:
                self.error('{}: {}'.format(extra_info, err))
            else:
                self.error('{}'.format(err))
            if self.raise_all_errors:
                raise err

    def _default_http_header(self, empty_session_only: bool = False) -> Dict[str, str]:
        """Returns default HTTP header we use for requests."""
        header = {'Accept-Encoding': 'gzip, deflate',
                  'Accept-Language': 'en-US,en;q=0.8',
                  'Connection': 'keep-alive',
                  'Content-Length': '0',
                  'Host': 'www.instagram.com',
                  'Origin': 'https://www.instagram.com',
                  'Referer': 'https://www.instagram.com/',
                  'User-Agent': self.user_agent,
                  'X-Instagram-AJAX': '1',
                  'X-Requested-With': 'XMLHttpRequest'}
        if empty_session_only:
            del header['Host']
            del header['Origin']
            del header['Referer']
            del header['X-Instagram-AJAX']
            del header['X-Requested-With']
        return header

    def get_anonymous_session(self) -> requests.Session:
        """Returns our default anonymous requests.Session object."""
        session = requests.Session()
        session.cookies.update({'sessionid': '', 'mid': '', 'ig_pr': '1',
                                'ig_vw': '1920', 'csrftoken': '',
                                's_network': '', 'ds_user_id': ''})
        session.headers.update(self._default_http_header(empty_session_only=True))
        return session

    def save_session_to_file(self, sessionfile):
        pickle.dump(requests.utils.dict_from_cookiejar(self._session.cookies), sessionfile)

    def load_session_from_file(self, username, sessionfile):
        session = requests.Session()
        session.cookies = requests.utils.cookiejar_from_dict(pickle.load(sessionfile))
        session.headers.update(self._default_http_header())
        session.headers.update({'X-CSRFToken': session.cookies.get_dict()['csrftoken']})
        self._session = session
        self.username = username

    def test_login(self) -> Optional[str]:
        data = self.graphql_query("d6f4427fbe92d846298cf93df0b937d3", {})
        return data["data"]["user"]["username"] if data["data"]["user"] is not None else None

    def login(self, user, passwd):
        session = requests.Session()
        session.cookies.update({'sessionid': '', 'mid': '', 'ig_pr': '1',
                                'ig_vw': '1920', 'csrftoken': '',
                                's_network': '', 'ds_user_id': ''})
        session.headers.update(self._default_http_header())
        self._sleep()
        resp = session.get('https://www.instagram.com/')
        session.headers.update({'X-CSRFToken': resp.cookies['csrftoken']})
        self._sleep()
        login = session.post('https://www.instagram.com/accounts/login/ajax/',
                             data={'password': passwd, 'username': user}, allow_redirects=True)
        session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
        if login.status_code == 200:
            self._session = session
            if user == self.test_login():
                self.username = user
            else:
                self.username = None
                self._session = None
                raise BadCredentialsException('Login error! Check your credentials!')
        else:
            raise ConnectionException('Login error! Connection error!')

    def _sleep(self):
        """Sleep a short time if self.sleep is set. Called before each request to instagram.com."""
        if self.sleep:
            time.sleep(random.uniform(0.5, 3))

    def get_json(self, path: str, params: Dict[str, Any], host: str = 'www.instagram.com',
                 session: Optional[requests.Session] = None, _attempt=1) -> Dict[str, Any]:
        """JSON request to Instagram.

        :param path: URL, relative to the given domain which defaults to www.instagram.com/
        :param params: GET parameters
        :param host: Domain part of the URL from where to download the requested JSON; defaults to www.instagram.com
        :param session: Session to use, or None to use self.session
        :return: Decoded response dictionary
        :raises QueryReturnedNotFoundException: When the server responds with a 404.
        :raises ConnectionException: When query repeatedly failed.
        """
        def graphql_query_waittime(query_hash: str, untracked_queries: bool = False) -> int:
            sliding_window = 660
            timestamps = self.previous_queries.get(query_hash)
            if not timestamps:
                return sliding_window if untracked_queries else 0
            current_time = time.monotonic()
            timestamps = list(filter(lambda t: t > current_time - sliding_window, timestamps))
            self.previous_queries[query_hash] = timestamps
            if len(timestamps) < 100 and not untracked_queries:
                return 0
            return round(min(timestamps) + sliding_window - current_time) + 6
        is_graphql_query = 'query_hash' in params and 'graphql/query' in path
        if is_graphql_query:
            query_hash = params['query_hash']
            waittime = graphql_query_waittime(query_hash)
            if waittime > 0:
                self.log('\nToo many queries in the last time. Need to wait {} seconds.'.format(waittime))
                time.sleep(waittime)
            timestamp_list = self.previous_queries.get(query_hash)
            if timestamp_list is not None:
                timestamp_list.append(time.monotonic())
            else:
                self.previous_queries[query_hash] = [time.monotonic()]
        sess = session if session else self._session
        try:
            self._sleep()
            resp = sess.get('https://{0}/{1}'.format(host, path), params=params, allow_redirects=False)
            while resp.is_redirect:
                redirect_url = resp.headers['location']
                self.log('\nHTTP redirect from https://{0}/{1} to {2}'.format(host, path, redirect_url))
                if redirect_url.index('https://{}/'.format(host)) == 0:
                    resp = sess.get(redirect_url if redirect_url.endswith('/') else redirect_url + '/',
                                    params=params, allow_redirects=False)
                else:
                    break
            if resp.status_code == 404:
                raise QueryReturnedNotFoundException("404")
            if resp.status_code == 429:
                raise TooManyRequestsException("429 - Too Many Requests")
            if resp.status_code != 200:
                raise ConnectionException("HTTP error code {}.".format(resp.status_code))
            resp_json = resp.json()
            if 'status' in resp_json and resp_json['status'] != "ok":
                if 'message' in resp_json:
                    raise ConnectionException("Returned \"{}\" status, message \"{}\".".format(resp_json['status'],
                                                                                               resp_json['message']))
                else:
                    raise ConnectionException("Returned \"{}\" status.".format(resp_json['status']))
            return resp_json
        except (ConnectionException, json.decoder.JSONDecodeError, requests.exceptions.RequestException) as err:
            error_string = "JSON Query to {}: {}".format(path, err)
            if _attempt == self.max_connection_attempts:
                raise ConnectionException(error_string)
            self.error(error_string + " [retrying; skip with ^C]", repeat_at_end=False)
            text_for_429 = ("HTTP error code 429 was returned because too many queries occured in the last time. "
                            "Please do not use Instagram in your browser or run multiple instances of Instaloader "
                            "in parallel.")
            try:
                if isinstance(err, TooManyRequestsException):
                    print(textwrap.fill(text_for_429), file=sys.stderr)
                    if is_graphql_query:
                        waittime = graphql_query_waittime(query_hash=params['query_hash'], untracked_queries=True)
                        if waittime > 0:
                            self.log('The request will be retried in {} seconds.'.format(waittime))
                            time.sleep(waittime)
                self._sleep()
                return self.get_json(path=path, params=params, host=host, session=sess, _attempt=_attempt + 1)
            except KeyboardInterrupt:
                self.error("[skipped by user]", repeat_at_end=False)
                raise ConnectionException(error_string)

    def graphql_query(self, query_hash: str, variables: Dict[str, Any],
                      referer: Optional[str] = None) -> Dict[str, Any]:
        """
        Do a GraphQL Query.

        :param query_hash: Query identifying hash.
        :param variables: Variables for the Query.
        :param referer: HTTP Referer, or None.
        :return: The server's response dictionary.
        """
        tmpsession = copy_session(self._session)
        tmpsession.headers.update(self._default_http_header(empty_session_only=True))
        del tmpsession.headers['Connection']
        del tmpsession.headers['Content-Length']
        tmpsession.headers['authority'] = 'www.instagram.com'
        tmpsession.headers['scheme'] = 'https'
        tmpsession.headers['accept'] = '*/*'
        if referer is not None:
            tmpsession.headers['referer'] = urllib.parse.quote(referer)
        resp_json = self.get_json('graphql/query',
                                  params={'query_hash': query_hash,
                                          'variables': json.dumps(variables, separators=(',', ':'))},
                                  session=tmpsession)
        tmpsession.close()
        if 'status' not in resp_json:
            self.error("GraphQL response did not contain a \"status\" field.")
        return resp_json

    def graphql_node_list(self, query_hash: str, query_variables: Dict[str, Any],
                          query_referer: Optional[str],
                          edge_extractor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        """Retrieve a list of GraphQL nodes."""
        query_variables['first'] = GRAPHQL_PAGE_LENGTH
        data = self.graphql_query(query_hash, query_variables, query_referer)
        while True:
            edge_struct = edge_extractor(data)
            yield from [edge['node'] for edge in edge_struct['edges']]
            if edge_struct['page_info']['has_next_page']:
                query_variables['after'] = edge_struct['page_info']['end_cursor']
                data = self.graphql_query(query_hash, query_variables, query_referer)
            else:
                break

    def get_and_write_raw(self, url: str, filename: str, _attempt=1) -> None:
        """Downloads raw data.

        :raises QueryReturnedNotFoundException: When the server responds with a 404.
        :raises QueryReturnedForbiddenException: When the server responds with a 403.
        :raises ConnectionException: When download repeatedly failed."""
        try:
            with self.get_anonymous_session() as anonymous_session:
                resp = anonymous_session.get(url)
            if resp.status_code == 200:
                self.log(filename, end=' ', flush=True)
                with open(filename, 'wb') as file:
                    resp.raw.decode_content = True
                    shutil.copyfileobj(resp.raw, file)
            else:
                if resp.status_code == 403:
                    # suspected invalid URL signature
                    raise QueryReturnedForbiddenException("403 when accessing {}.".format(url))
                if resp.status_code == 404:
                    # 404 not worth retrying.
                    raise QueryReturnedNotFoundException("404 when accessing {}.".format(url))
                raise ConnectionException("HTTP error code {}.".format(resp.status_code))
        except (urllib3.exceptions.HTTPError, requests.exceptions.RequestException, ConnectionException) as err:
            error_string = "URL {}: {}".format(url, err)
            if _attempt == self.max_connection_attempts:
                raise ConnectionException(error_string)
            self.error(error_string + " [retrying; skip with ^C]", repeat_at_end=False)
            try:
                self._sleep()
                self.get_and_write_raw(url, filename, _attempt + 1)
            except KeyboardInterrupt:
                self.error("[skipped by user]", repeat_at_end=False)
                raise ConnectionException(error_string)
