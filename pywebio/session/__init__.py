r"""

.. autofunction:: run_async
.. autofunction:: run_asyncio_coroutine
.. autofunction:: download
.. autofunction:: run_js
.. autofunction:: eval_js
.. autofunction:: register_thread
.. autofunction:: defer_call
.. autofunction:: hold
.. autofunction:: data
.. autofunction:: set_env
.. autofunction:: go_app
.. autofunction:: get_info

.. autoclass:: pywebio.session.coroutinebased.TaskHandle
   :members:
"""

import threading
from base64 import b64encode
from functools import wraps

from .base import Session
from .coroutinebased import CoroutineBasedSession
from .threadbased import ThreadBasedSession, ScriptModeSession
from ..exceptions import SessionNotFoundException, SessionException
from ..utils import iscoroutinefunction, isgeneratorfunction, run_as_function, to_coroutine

# 当前进程中正在使用的会话实现的列表
_active_session_cls = []

__all__ = ['run_async', 'run_asyncio_coroutine', 'register_thread', 'hold', 'defer_call', 'data', 'get_info',
           'run_js', 'eval_js', 'download', 'set_env', 'go_app']


def register_session_implement_for_target(target_func):
    """根据target_func函数类型注册会话实现，并返回会话实现"""
    if iscoroutinefunction(target_func) or isgeneratorfunction(target_func):
        cls = CoroutineBasedSession
    else:
        cls = ThreadBasedSession

    if ScriptModeSession in _active_session_cls:
        raise RuntimeError("Already in script mode, can't start server")

    if cls not in _active_session_cls:
        _active_session_cls.append(cls)

    return cls


def get_session_implement():
    """获取当前会话实现。仅供内部实现使用。应在会话上下文中调用"""
    if not _active_session_cls:
        _active_session_cls.append(ScriptModeSession)
        _start_script_mode_server()

    # 当前正在使用的会话实现只有一个
    if len(_active_session_cls) == 1:
        return _active_session_cls[0]

    # 当前有多个正在使用的会话实现
    for cls in _active_session_cls:
        try:
            cls.get_current_session()
            return cls
        except SessionNotFoundException:
            pass

    raise SessionNotFoundException


def _start_script_mode_server():
    from ..platform.tornado import start_server_in_current_thread_session
    start_server_in_current_thread_session()


def get_current_session() -> "Session":
    return get_session_implement().get_current_session()


def get_current_task_id():
    return get_session_implement().get_current_task_id()


def check_session_impl(session_type):
    def decorator(func):
        """装饰器：在函数调用前检查当前会话实现是否满足要求"""

        @wraps(func)
        def inner(*args, **kwargs):
            curr_impl = get_session_implement()

            # Check if 'now_impl' is a derived from session_type or is the same class
            if not issubclass(curr_impl, session_type):
                func_name = getattr(func, '__name__', str(func))
                require = getattr(session_type, '__name__', str(session_type))
                curr = getattr(curr_impl, '__name__', str(curr_impl))

                raise RuntimeError("Only can invoke `{func_name:s}` in {require:s} context."
                                   " You are now in {curr:s} context".format(func_name=func_name, require=require,
                                                                             curr=curr))
            return func(*args, **kwargs)

        return inner

    return decorator


def chose_impl(gen_func):
    """
    装饰器，使用chose_impl对gen_func进行装饰后，gen_func() 操作将根据当前会话实现 返回协程对象 或 直接运行函数体
    """

    @wraps(gen_func)
    def inner(*args, **kwargs):
        gen = gen_func(*args, **kwargs)
        if get_session_implement() == CoroutineBasedSession:
            return to_coroutine(gen)
        else:
            return run_as_function(gen)

    return inner


@chose_impl
def next_client_event():
    res = yield get_current_session().next_client_event()
    return res


@chose_impl
def hold():
    """Keep the session alive until the browser page is closed by user.

    .. note::

        After the PyWebIO session closed, the functions that need communicate with the PyWebIO server (such as the event callback of `put_buttons()` and download link of `put_file()`) will not work. You can call the ``hold()`` function at the end of the task function to hold the session, so that the event callback and download link will always be available before the browser page is closed by user.

    Note: When using :ref:`coroutine-based session <coroutine_based_session>`, you need to use the ``await hold()`` syntax to call the function.
    """
    while True:
        try:
            yield next_client_event()
        except SessionException:
            return


def download(name, content):
    """Send file to user, and the user browser will download the file to the local

    :param str name: File name when downloading
    :param content: File content. It is a bytes-like object

    Example:

    .. exportable-codeblock::
        :name: download
        :summary: `download()` usage

        put_buttons(['Click to download'], [lambda: download('hello-world.txt', b'hello world!')])

    """
    from ..io_ctrl import send_msg
    content = b64encode(content).decode('ascii')
    send_msg('download', spec=dict(name=name, content=content))


def run_js(code_, **args):
    """Execute JavaScript code in user browser.

    The code is run in the browser's JS global scope.

    :param str code_: JavaScript code
    :param args: Local variables passed to js code. Variables need to be JSON-serializable.

    Example::

        run_js('console.log(a + b)', a=1, b=2)

    """
    from ..io_ctrl import send_msg
    send_msg('run_script', spec=dict(code=code_, args=args))


@chose_impl
def eval_js(expression_, **args):
    """Execute JavaScript expression in the user's browser and get the value of the expression

    :param str expression_: JavaScript expression. The value of the expression need to be JSON-serializable.
    :param args: Local variables passed to js code. Variables need to be JSON-serializable.
    :return: The value of the expression.

    Note: When using :ref:`coroutine-based session <coroutine_based_session>`, you need to use the ``await eval_js(expression)`` syntax to call the function.

    Example:

    .. exportable-codeblock::
        :name: eval_js
        :summary: `eval_js()` usage

        current_url = eval_js("window.location.href")
        put_text(current_url)  # ..demo-only
        
        ## ----
        function_res = eval_js('''(function(){
            var a = 1;
            a += b;
            return a;
        })()''', b=100)
        put_text(function_res)  # ..demo-only

    """
    script = r"""
    (function(WebIO){
        let ____result____ = null;  // to avoid naming conflict
        try{
            ____result____ = eval(%r);
        }catch{};
        
        WebIO.sendMessage({
            event: "js_yield",
            task_id: WebIOCurrentTaskID,  // local var in run_script command
            data: ____result____ || null
        });
    })(WebIO);""" % expression_

    run_js(script, **args)

    res = yield next_client_event()
    assert res['event'] == 'js_yield', "Internal Error, please report this bug on " \
                                       "https://github.com/wang0618/PyWebIO/issues"
    return res['data']


@check_session_impl(CoroutineBasedSession)
def run_async(coro_obj):
    """Run the coroutine object asynchronously. PyWebIO interactive functions are also available in the coroutine.

    ``run_async()`` can only be used in :ref:`coroutine-based session <coroutine_based_session>`.

    :param coro_obj: Coroutine object
    :return: `TaskHandle <pywebio.session.coroutinebased.TaskHandle>` instance, which can be used to query the running status of the coroutine or close the coroutine.

    See also: :ref:`Concurrency in coroutine-based sessions <coroutine_based_concurrency>`
    """
    return get_current_session().run_async(coro_obj)


@check_session_impl(CoroutineBasedSession)
async def run_asyncio_coroutine(coro_obj):
    """
    If the thread running sessions are not the same as the thread running the asyncio event loop, you need to wrap ``run_asyncio_coroutine()`` to run the coroutine in asyncio.

    Can only be used in :ref:`coroutine-based session <coroutine_based_session>`.

    :param coro_obj: Coroutine object in `asyncio`

    Example::

        async def app():
            put_text('hello')
            await run_asyncio_coroutine(asyncio.sleep(1))
            put_text('world')

        pywebio.platform.flask.start_server(app)

    """
    return await get_current_session().run_asyncio_coroutine(coro_obj)


@check_session_impl(ThreadBasedSession)
def register_thread(thread: threading.Thread):
    """Register the thread so that PyWebIO interactive functions are available in the thread.

    Can only be used in the thread-based session.

    See :ref:`Concurrent in Server mode <thread_in_server_mode>`

    :param threading.Thread thread: Thread object
    """
    return get_current_session().register_thread(thread)


def defer_call(func):
    """Set the function to be called when the session closes.

    Whether it is because the user closes the page or the task finishes to cause session closed, the function set by ``defer_call(func)`` will be executed. Can be used for resource cleaning.

    You can call ``defer_call(func)`` multiple times in the session, and the set functions will be executed sequentially after the session closes.

    ``defer_call()`` can also be used as decorator::

         @defer_call
         def cleanup():
            pass

    .. attention:: PyWebIO interactive functions cannot be called inside the function ``func``.

    """
    get_current_session().defer_call(func)
    return func


def data():
    """Get the session-local object of current session.

    When accessing a property that does not exist in the data object, it returns ``None`` instead of throwing an exception.

    When you need to save some session-independent data with multiple functions, it is more convenient to use session-local objects to save state than to use function parameters.

    Here is a example of a session independent counter implementation::

        def add():
            data().cnt = (data().cnt or 0) + 1

        def show():
            put_text(data().cnt or 0)

        def main():
            put_buttons(['Add counter', 'Show counter'], [add, show])
            hold()

    The way to pass state through function parameters is::

        from functools import partial
        def add(cnt):
            cnt[0] += 1

        def show(cnt):
            put_text(cnt[0])

        def main():
            cnt = [0]  # 将计数器保存在数组中才可以实现引用传参
            put_buttons(['Add counter', 'Show counter'], [partial(add, cnt), partial(show, cnt)])
            hold()

    Of course, you can also use function closures to achieved the same::

        def main():
            cnt = 0

            def add():
                nonlocal cnt
                cnt += 1

            def show():
                put_text(cnt)

            put_buttons(['Add counter', 'Show counter'], [add, show])
            hold()
    """
    return get_current_session().save


def set_env(**env_info):
    """Config the environment of current session.

    Available configuration are:

    * ``title`` (str): Title of current page.
    * ``output_animation`` (bool): Whether to enable output animation, enabled by default
    * ``auto_scroll_bottom`` (bool): Whether to automatically scroll the page to the bottom after output content, it is closed by default.  Note that after enabled, only outputting to ROOT scope can trigger automatic scrolling.
    * ``http_pull_interval`` (int): The period of HTTP polling messages (in milliseconds, default 1000ms), only available in sessions based on HTTP connection.

    Example::

        set_env(title='Awesome PyWebIO!!', output_animation=False)
    """
    from ..io_ctrl import send_msg
    assert all(k in ('title', 'output_animation', 'auto_scroll_bottom', 'http_pull_interval')
               for k in env_info.keys())
    send_msg('set_env', spec=env_info)


def go_app(name, new_window=True):
    """Jump to another task of a same PyWebIO application. Only available in PyWebIO Server mode

    :param str name: Target PyWebIO task name.
    :param bool new_window: Whether to open in a new window, the default is `True`

    See also: :ref:`Server mode <server_and_script_mode>`
    """
    run_js('javascript:WebIO.openApp(app, new_window)', app=name, new_window=new_window)


def get_info():
    """Get information about the current session

    :return: Object of session information, whose attributes are:

       * ``user_agent`` : The Object of the user browser information, whose attributes are

            * ``is_mobile`` (bool): whether user agent is identified as a mobile phone (iPhone, Android phones, Blackberry, Windows Phone devices etc)
            * ``is_tablet`` (bool): whether user agent is identified as a tablet device (iPad, Kindle Fire, Nexus 7 etc)
            * ``is_pc`` (bool): whether user agent is identified to be running a traditional "desktop" OS (Windows, OS X, Linux)
            * ``is_touch_capable`` (bool): whether user agent has touch capabilities

            * ``browser.family`` (str): Browser family. such as 'Mobile Safari'
            * ``browser.version`` (tuple): Browser version. such as (5, 1)
            * ``browser.version_string`` (str): Browser version string. such as '5.1'

            * ``os.family`` (str): User OS family. such as 'iOS'
            * ``os.version`` (tuple): User OS version. such as (5, 1)
            * ``os.version_string`` (str): User OS version string. such as '5.1'

            * ``device.family`` (str): User agent's device family. such as 'iPhone'
            * ``device.brand`` (str): Device brand. such as 'Apple'
            * ``device.model`` (str): Device model. such as 'iPhone'

       * ``user_language`` (str): Language used by the user's operating system. 比如 ``'zh-CN'``
       * ``server_host`` (str): PyWebIO server host, including domain and port, the port can be omitted when 80.
       * ``origin`` (str): Indicate where the user from. Including protocol, host, and port parts. Such as ``'http://localhost:8080'`` .
         It may be empty, but it is guaranteed to have a value when the user's page address is not under the server host. (that is, the host, port part are inconsistent with ``server_host``).
       * ``user_ip`` (str): User's ip address.
       * ``backend`` (str): The current PyWebIO backend server implementation. The possible values are ``'tornado'``, ``'flask'``, ``'django'`` , ``'aiohttp'``.
       * ``request`` (object): The request object when creating the current session. Depending on the backend server, the type of ``request`` can be:

            * When using Tornado, ``request`` is instance of
              `tornado.httputil.HTTPServerRequest <https://www.tornadoweb.org/en/stable/httputil.html#tornado.httputil.HTTPServerRequest>`_
            * When using Flask, ``request`` is instance of `flask.Request <https://flask.palletsprojects.com/en/1.1.x/api/#incoming-request-data>`_ 实例
            * When using Django, ``request`` is instance of `django.http.HttpRequest <https://docs.djangoproject.com/en/3.0/ref/request-response/#django.http.HttpRequest>`_
            * When using aiohttp, ``request`` is instance of `aiohttp.web.BaseRequest <https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.BaseRequest>`_

    The ``user_agent`` attribute of the session information object is parsed by the user-agents library. See https://github.com/selwin/python-user-agents#usage
    
    Example:

    .. exportable-codeblock::
        :name: get_info
        :summary: `get_info()` usage

        import json

        info = get_info()
        put_code(json.dumps({
            k: str(v)
            for k,v in info.items()
        }, indent=4), 'json')
    """
    return get_current_session().info
