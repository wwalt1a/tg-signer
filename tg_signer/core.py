import asyncio
import json
import logging
import os
import pathlib
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import (
    Annotated,
    Awaitable,
    BinaryIO,
    Callable,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)
from urllib import parse

import httpx
from croniter import CroniterBadCronError, croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyrogram import Client as BaseClient
from pyrogram import errors, filters
from pyrogram.enums import ChatMembersFilter, ChatType
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.methods.utilities.idle import idle
from pyrogram.session import Session
from pyrogram.storage import SQLiteStorage
from pyrogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Object,
    User,
)

from tg_signer.config import (
    ActionGroup,
    ActionT,
    BaseJSONConfig,
    ChooseOptionByImageAction,
    ClickKeyboardByTextAction,
    HttpCallback,
    MatchConfig,
    MonitorConfig,
    ReplyByCalculationProblemAction,
    SendDiceAction,
    SendTextAction,
    SignChatV3,
    SignConfigV3,
    SupportAction,
    UDPForward,
)

# --- 深度稳定性补丁 (Deep Stability Patch) ---
# 1. 运行时屏蔽 Pyrogram 内核解析错误 (Monkey Patch)
# 解决他人回复已失效/无权访问频道消息导致的 ChannelInvalid 崩溃
_original_parse_message = Message._parse_message


async def _patched_parse_message(*args, **kwargs):
    try:
        return await _original_parse_message(*args, **kwargs)
    except (errors.ChannelInvalid, errors.PeerIdInvalid):
        # 如果解析失败，返回 None 以跳过这条消息的后续处理，而不是崩溃
        return None


Message._parse_message = _patched_parse_message
# ----------------------------------------

from .ai_tools import AITools, OpenAIConfigManager
from .notification.server_chan import sc_send
from .utils import UserInput, print_to_user

logger = logging.getLogger("tg-signer")

DICE_EMOJIS = ("🎲", "🎯", "🏀", "⚽", "🎳", "🎰")

Session.START_TIMEOUT = 5  # 原始超时时间为2秒，但一些代理访问会超时，所以这里调大一点

OPENAI_USE_PROMPT = "当前任务需要配置大模型，请确保运行前正确设置`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`等环境变量，或通过`tg-signer llm-config`持久化配置。"


def readable_message(message: Message):
    s = "\nMessage: "
    s += f"\n  text: {message.text or ''}"
    if message.photo:
        s += f"\n  图片: [({message.photo.width}x{message.photo.height}) {message.caption}]"
    if message.reply_markup:
        if isinstance(message.reply_markup, InlineKeyboardMarkup):
            s += "\n  InlineKeyboard: "
            for row in message.reply_markup.inline_keyboard:
                s += "\n   "
                for button in row:
                    s += f"{button.text} | "
    return s


def readable_chat(chat: Chat):
    if chat.type == ChatType.BOT:
        type_ = "BOT"
    elif chat.type == ChatType.GROUP:
        type_ = "群组"
    elif chat.type == ChatType.SUPERGROUP:
        type_ = "超级群组"
    elif chat.type == ChatType.CHANNEL:
        type_ = "频道"
    else:
        type_ = "个人"

    none_or_dash = lambda x: x or "-"  # noqa: E731

    return f"id: {chat.id}, username: {none_or_dash(chat.username)}, title: {none_or_dash(chat.title)}, type: {type_}, name: {none_or_dash(chat.first_name)}"


_CLIENT_INSTANCES: dict[str, "Client"] = {}

# reference counts and async locks for shared client lifecycle management
# Keyed by account name. Use asyncio locks to serialize start/stop operations
# so multiple coroutines in the same process can safely share one Client.
_CLIENT_REFS: defaultdict[str, int] = defaultdict(int)
_CLIENT_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}

# login bootstrap state keyed by account key. This prevents concurrent tasks
# from repeatedly calling get_me/get_dialogs for the same account.
_LOGIN_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}
_LOGIN_USERS: dict[str, User] = {}

_API_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}
_API_LAST_CALL_AT: dict[str, float] = {}
_API_MIN_INTERVAL_SECONDS = 0.35
_API_FLOODWAIT_PADDING_SECONDS = 0.5
_API_MAX_FLOODWAIT_RETRIES = 2


class Client(BaseClient):
    def __init__(self, name: str, *args, **kwargs):
        key = kwargs.pop("key", None)
        super().__init__(name, *args, **kwargs)
        self.key = key or str(pathlib.Path(self.workdir).joinpath(self.name).resolve())
        if self.in_memory and not self.session_string:
            self.load_session_string()
            self.storage = SQLiteStorage(
                name=self.name,
                workdir=self.workdir,
                session_string=self.session_string,
                in_memory=True,
            )

    async def __aenter__(self):
        lock = _CLIENT_ASYNC_LOCKS.get(self.key)
        if lock is None:
            lock = asyncio.Lock()
            _CLIENT_ASYNC_LOCKS[self.key] = lock
        async with lock:
            _CLIENT_REFS[self.key] += 1
            if _CLIENT_REFS[self.key] == 1:
                # 针对多进程并发导致的 database is locked 增加指数退避重试
                max_retries = 5
                for i in range(max_retries):
                    try:
                        # 在启动前尝试预先开启 WAL 模式（如果文件已存在）
                        try:
                            db_path = f"{self.name}.session"
                            if os.path.exists(db_path):
                                import sqlite3
                                conn = sqlite3.connect(db_path, timeout=10)
                                conn.execute("PRAGMA journal_mode=WAL")
                                conn.execute("PRAGMA busy_timeout=30000")
                                conn.close()
                        except Exception:
                            pass

                        await self.start()
                        # 启动后再次确保 WAL 模式开启
                        try:
                            await self.storage.conn.execute("PRAGMA journal_mode=WAL")
                            await self.storage.conn.execute("PRAGMA busy_timeout=30000")
                        except Exception:
                            pass
                        break
                    except (OSError, Exception) as e:
                        # 检查 e 是否有 __name__ 属性，或者直接转字符串匹配
                        err_msg = str(e).lower()
                        if "database is locked" in err_msg and i < max_retries - 1:
                            wait_time = (i + 1) * 2 + random.random()
                            logger.warning(f"数据库被锁定，{wait_time:.1f}秒后重试第 {i+1} 次...")
                            await asyncio.sleep(wait_time)
                        else:
                            raise e
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        lock = _CLIENT_ASYNC_LOCKS.get(self.key)
        if lock is None:
            return
        async with lock:
            _CLIENT_REFS[self.key] -= 1
            if _CLIENT_REFS[self.key] == 0:
                try:
                    await self.stop()
                except ConnectionError:
                    pass
                _CLIENT_INSTANCES.pop(self.key, None)

    @property
    def session_string_file(self):
        return self.workdir / (self.name + ".session_string")

    async def save_session_string(self):
        with open(self.session_string_file, "w") as fp:
            fp.write(await self.export_session_string())

    def load_session_string(self):
        logger.info("Loading session_string from local file.")
        if self.session_string_file.is_file():
            with open(self.session_string_file, "r") as fp:
                self.session_string = fp.read()
                logger.info("The session_string has been loaded.")
        return self.session_string

    async def log_out(
        self,
    ):
        await super().log_out()
        if self.session_string_file.is_file():
            os.remove(self.session_string_file)


def get_api_config():
    api_id = int(os.environ.get("TG_API_ID", 611335))
    api_hash = os.environ.get("TG_API_HASH", "d524b414d21f4d37f08684c1df41ac9c")
    return api_id, api_hash


def get_proxy(proxy: str = None):
    proxy = proxy or os.environ.get("TG_PROXY")
    if proxy:
        r = parse.urlparse(proxy)
        return {
            "scheme": r.scheme,
            "hostname": r.hostname,
            "port": r.port,
            "username": r.username,
            "password": r.password,
        }
    return None


def get_client(
    name: str = "my_account",
    proxy: dict = None,
    workdir: Union[str, pathlib.Path] = ".",
    session_string: str = None,
    in_memory: bool = False,
    **kwargs,
) -> Client:
    proxy = proxy or get_proxy()
    api_id, api_hash = get_api_config()
    key = str(pathlib.Path(workdir).joinpath(name).resolve())
    if key in _CLIENT_INSTANCES:
        return _CLIENT_INSTANCES[key]
    client = Client(
        name,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
        workdir=workdir,
        session_string=session_string,
        in_memory=in_memory,
        key=key,
        **kwargs,
    )
    _CLIENT_INSTANCES[key] = client
    return client


def get_now():
    return datetime.now(tz=timezone(timedelta(hours=8)))


def make_dirs(path: pathlib.Path, exist_ok=True):
    path = pathlib.Path(path)
    if not path.is_dir():
        os.makedirs(path, exist_ok=exist_ok)
    return path


ConfigT = TypeVar("ConfigT", bound=BaseJSONConfig)
ApiCallResultT = TypeVar("ApiCallResultT")


class BaseUserWorker(Generic[ConfigT]):
    _workdir = "."
    _tasks_dir = "tasks"
    cfg_cls: Type["ConfigT"] = BaseJSONConfig

    def __init__(
        self,
        task_name: str = None,
        session_dir: str = ".",
        account: str = "my_account",
        proxy=None,
        workdir=None,
        session_string: str = None,
        in_memory: bool = False,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.task_name = task_name or "my_task"
        self._session_dir = pathlib.Path(session_dir)
        self._account = account
        self._proxy = proxy
        if workdir:
            self._workdir = pathlib.Path(workdir)
        self.app = get_client(
            account,
            proxy,
            workdir=self._session_dir,
            session_string=session_string,
            in_memory=in_memory,
            loop=loop,
        )
        self.loop = self.app.loop
        self.user: Optional[User] = None
        self._config = None
        self.context = self.ensure_ctx()

    def ensure_ctx(self):
        return {}

    def app_run(self, coroutine=None):
        if coroutine is not None:
            run = self.loop.run_until_complete
            run(coroutine)
        else:
            self.app.run()

    @property
    def workdir(self) -> pathlib.Path:
        workdir = self._workdir
        make_dirs(workdir)
        return pathlib.Path(workdir)

    @property
    def tasks_dir(self):
        tasks_dir = self.workdir / self._tasks_dir
        make_dirs(tasks_dir)
        return pathlib.Path(tasks_dir)

    @property
    def task_dir(self):
        task_dir = self.tasks_dir / self.task_name
        make_dirs(task_dir)
        return task_dir

    def get_user_dir(self, user: User):
        user_dir = self.workdir / "users" / str(user.id)
        make_dirs(user_dir)
        return user_dir

    @property
    def config_file(self):
        return self.task_dir.joinpath("config.json")

    @property
    def config(self) -> ConfigT:
        return self._config or self.load_config()

    @config.setter
    def config(self, value):
        self._config = value

    def log(self, msg, level: str = "INFO", **kwargs):
        msg = f"账户「{self._account}」- 任务「{self.task_name}」: {msg}"
        if level.upper() == "INFO":
            logger.info(msg, **kwargs)
        elif level.upper() == "WARNING":
            logger.warning(msg, **kwargs)
        elif level.upper() == "ERROR":
            logger.error(msg, **kwargs)
        elif level.upper() == "CRITICAL":
            logger.critical(msg, **kwargs)
        else:
            logger.debug(msg, **kwargs)

    async def _call_telegram_api(
        self,
        operation: str,
        call: Callable[[], Awaitable[ApiCallResultT]],
        *,
        retry_on_floodwait: bool = True,
    ) -> ApiCallResultT:
        key = self.app.key
        lock = _API_ASYNC_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _API_ASYNC_LOCKS[key] = lock

        retries_left = _API_MAX_FLOODWAIT_RETRIES
        while True:
            async with lock:
                loop = asyncio.get_running_loop()
                last_called_at = _API_LAST_CALL_AT.get(key)
                if last_called_at is not None:
                    wait_for = _API_MIN_INTERVAL_SECONDS - (
                        loop.time() - last_called_at
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                try:
                    result = await call()
                    _API_LAST_CALL_AT[key] = loop.time()
                    return result
                except errors.FloodWait as e:
                    _API_LAST_CALL_AT[key] = loop.time()
                    if not retry_on_floodwait or retries_left <= 0:
                        raise
                    retries_left -= 1
                    wait_seconds = (
                        max(float(getattr(e, "value", 0) or 0), 0)
                        + _API_FLOODWAIT_PADDING_SECONDS
                    )
                    self.log(
                        f"{operation} 触发 FloodWait，等待 {wait_seconds:.1f}s 后重试（剩余重试 {retries_left} 次）",
                        level="WARNING",
                    )
                    await asyncio.sleep(wait_seconds)

    def ask_for_config(self):
        raise NotImplementedError

    def write_config(self, config: BaseJSONConfig):
        with open(self.config_file, "w", encoding="utf-8") as fp:
            json.dump(config.to_jsonable(), fp, ensure_ascii=False)

    def reconfig(self):
        config = self.ask_for_config()
        self.write_config(config)
        return config

    def load_config(self, cfg_cls: Type[ConfigT] = None) -> ConfigT:
        cfg_cls = cfg_cls or self.cfg_cls
        if not self.config_file.exists():
            config = self.reconfig()
        else:
            with open(self.config_file, "r", encoding="utf-8") as fp:
                config, from_old = cfg_cls.load(json.load(fp))
                if from_old:
                    self.write_config(config)
        self.config = config
        return config

    def get_task_list(self):
        signs = []
        for d in os.listdir(self.tasks_dir):
            if self.tasks_dir.joinpath(d).is_dir():
                signs.append(d)
        return signs

    def list_(self):
        for d in self.get_task_list():
            print_to_user(d)

    def set_me(self, user: User):
        self.user = user
        with open(
            self.get_user_dir(user).joinpath("me.json"), "w", encoding="utf-8"
        ) as fp:
            fp.write(str(user))

    async def login(self, num_of_dialogs=20, print_chat=True):
        self.log("开始登录...")
        app = self.app
        key = app.key
        lock = _LOGIN_ASYNC_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOGIN_ASYNC_LOCKS[key] = lock

        async with lock:
            me = _LOGIN_USERS.get(key)
            if me is None:
                async with app:
                    me = await self._call_telegram_api("users.GetFullUser", app.get_me)

                    async def load_latest_chats():
                        latest_chats = []
                        async for dialog in app.get_dialogs(limit=num_of_dialogs):
                            chat = dialog.chat
                            latest_chats.append(
                                {
                                    "id": chat.id,
                                    "title": chat.title,
                                    "type": chat.type,
                                    "username": chat.username,
                                    "first_name": chat.first_name,
                                    "last_name": chat.last_name,
                                }
                            )
                            if print_chat:
                                print_to_user(readable_chat(chat))
                        return latest_chats

                    latest_chats = await self._call_telegram_api(
                        "messages.GetDialogs", load_latest_chats
                    )

                    with open(
                        self.get_user_dir(me).joinpath("latest_chats.json"),
                        "w",
                        encoding="utf-8",
                    ) as fp:
                        json.dump(
                            latest_chats,
                            fp,
                            indent=4,
                            default=Object.default,
                            ensure_ascii=False,
                        )
                    await self._call_telegram_api(
                        "auth.ExportAuthorization", self.app.save_session_string
                    )
                _LOGIN_USERS[key] = me
            else:
                self.log("检测到同账号已完成登录初始化，复用已有会话信息")
            self.set_me(me)

    async def logout(self):
        self.log("开始登出...")
        is_authorized = await self.app.connect()
        if not is_authorized:
            await self.app.storage.delete()
            _LOGIN_USERS.pop(self.app.key, None)
            self.user = None
            return None
        result = await self.app.log_out()
        _LOGIN_USERS.pop(self.app.key, None)
        self.user = None
        return result

    async def send_message(
        self, chat_id: Union[int, str], text: str, delete_after: int = None,
        message_thread_id: int = None, **kwargs
    ):
        """
        发送文本消息
        :param chat_id:
        :param text:
        :param delete_after: 秒, 发送消息后进行删除，``None`` 表示不删除, ``0`` 表示立即删除.
        :param message_thread_id: 话题 Thread ID，发送到指定话题
        :param kwargs:
        :return:
        """
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        message = await self._call_telegram_api(
            "messages.SendMessage",
            lambda: self.app.send_message(chat_id, text, **kwargs),
        )
        if delete_after is not None:
            self.log(
                f"消息「{text[:20]}...」将在 {delete_after} 秒后被删除。"
            )
            await asyncio.sleep(delete_after)
            try:
                await self._call_telegram_api(
                    "messages.DeleteMessages",
                    lambda: self.app.delete_messages(chat_id, message.id)
                )
                self.log(f"消息「{text[:20]}...」已完成定时删除。")
            except Exception as e:
                self.log(f"定时删除消息失败: {e}", level="WARNING")
        return message

    async def send_dice(
        self,
        chat_id: Union[int, str],
        emoji: str = "🎲",
        delete_after: int = None,
        message_thread_id: int = None,
        **kwargs,
    ):
        """
        发送DICE类型消息
        :param chat_id:
        :param emoji: Should be one of "🎲", "🎯", "🏀", "⚽", "🎳", or "🎰".
        :param delete_after:
        :param message_thread_id: 话题 Thread ID，发送到指定话题
        :param kwargs:
        :return:
        """
        emoji = emoji.strip()
        if emoji not in DICE_EMOJIS:
            self.log(
                f"Warning, emoji should be one of {', '.join(DICE_EMOJIS)}",
                level="WARNING",
            )
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        message = await self._call_telegram_api(
            "messages.SendMedia",
            lambda: self.app.send_dice(chat_id, emoji, **kwargs),
        )
        if message and delete_after is not None:
            self.log(
                f"骰子/媒体「{emoji}」将在 {delete_after} 秒后被删除。"
            )
            await asyncio.sleep(delete_after)
            try:
                await self._call_telegram_api(
                    "messages.DeleteMessages",
                    lambda: self.app.delete_messages(chat_id, message.id)
                )
                self.log(f"骰子/媒体「{emoji}」已完成定时删除。")
            except Exception as e:
                self.log(f"定时删除骰子/媒体失败: {e}", level="WARNING")
        return message

    async def search_members(
        self, chat_id: Union[int, str], query: str, admin=False, limit=10
    ):
        filter_ = ChatMembersFilter.SEARCH
        if admin:
            filter_ = ChatMembersFilter.ADMINISTRATORS
            query = ""
        async for member in self.app.get_chat_members(
            chat_id, query, limit=limit, filter=filter_
        ):
            yield member

    async def list_members(
        self, chat_id: Union[int, str], query: str = "", admin=False, limit=10
    ):
        async with self.app:
            async for member in self.search_members(chat_id, query, admin, limit):
                print_to_user(
                    User(
                        id=member.user.id,
                        username=member.user.username,
                        first_name=member.user.first_name,
                        last_name=member.user.last_name,
                        is_bot=member.user.is_bot,
                    )
                )

    def export(self):
        with open(self.config_file, "r", encoding="utf-8") as fp:
            data = fp.read()
        return data

    def import_(self, config_str: str):
        with open(self.config_file, "w", encoding="utf-8") as fp:
            fp.write(config_str)

    def ask_one(self):
        raise NotImplementedError

    def ensure_ai_cfg(self):
        cfg_manager = OpenAIConfigManager(self.workdir)
        cfg = cfg_manager.load_config()
        if not cfg:
            cfg = cfg_manager.ask_for_config()
        return cfg

    def get_ai_tools(self):
        return AITools(self.ensure_ai_cfg())


class Waiter:
    def __init__(self):
        self.waiting_ids = set()
        self.waiting_counter = Counter()

    def add(self, elm):
        self.waiting_ids.add(elm)
        self.waiting_counter[elm] += 1

    def discard(self, elm):
        self.waiting_ids.discard(elm)
        self.waiting_counter.pop(elm, None)

    def sub(self, elm):
        self.waiting_counter[elm] -= 1
        if self.waiting_counter[elm] <= 0:
            self.discard(elm)

    def clear(self):
        self.waiting_ids.clear()
        self.waiting_counter.clear()

    def __bool__(self):
        return bool(self.waiting_ids)

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self.waiting_counter}>"


class UserSignerWorkerContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    waiter: Waiter
    sign_chats: defaultdict[int, list[SignChatV3]]  # 签到配置列表
    chat_messages: defaultdict[
        int,
        Annotated[
            dict[int, Optional[Message]],
            Field(default_factory=dict),
        ],
    ]  # 收到的消息，key为chat id
    waiting_message: Optional[Message]  # 正在处理的消息


class UserSigner(BaseUserWorker[SignConfigV3]):
    _workdir = ".signer"
    _tasks_dir = "signs"
    cfg_cls = SignConfigV3
    context: UserSignerWorkerContext

    def ensure_ctx(self) -> UserSignerWorkerContext:
        return UserSignerWorkerContext(
            waiter=Waiter(),
            sign_chats=defaultdict(list),
            chat_messages=defaultdict(dict),
            waiting_message=None,
        )

    @property
    def sign_record_file(self):
        sign_record_dir = self.task_dir / str(self.user.id)
        make_dirs(sign_record_dir)
        return sign_record_dir / "sign_record.json"

    def _ask_actions(
        self, input_: UserInput, available_actions: List[SupportAction] = None
    ) -> List[ActionT]:
        print_to_user(f"{input_.index_str}开始配置<动作>，请按照实际签到顺序配置。")
        available_actions = available_actions or list(SupportAction)
        actions = []
        while True:
            try:
                local_input_ = UserInput()
                print_to_user(f"第{len(actions) + 1}个动作: ")
                for action in available_actions:
                    print_to_user(f"  {action.value}: {action.desc}")
                print_to_user()
                action_str = local_input_("输入对应的数字选择动作: ").strip()
                action = SupportAction(int(action_str))
                if action not in available_actions:
                    raise ValueError(f"不支持的动作: {action}")
                if len(actions) == 0 and action not in [
                    SupportAction.SEND_TEXT,
                    SupportAction.SEND_DICE,
                ]:
                    raise ValueError(
                        f"第一个动作必须为「{SupportAction.SEND_TEXT.desc}」或「{SupportAction.SEND_DICE.desc}」"
                    )
                if action == SupportAction.SEND_TEXT:
                    text = local_input_("输入要发送的文本: ")
                    actions.append(SendTextAction(text=text))
                elif action == SupportAction.SEND_DICE:
                    dice = local_input_("输入要发送的骰子（如 🎲, 🎯）: ")
                    actions.append(SendDiceAction(dice=dice))
                elif action == SupportAction.CLICK_KEYBOARD_BY_TEXT:
                    text_of_btn_to_click = local_input_("键盘中需要点击的按钮文本: ")
                    actions.append(ClickKeyboardByTextAction(text=text_of_btn_to_click))
                elif action == SupportAction.CHOOSE_OPTION_BY_IMAGE:
                    print_to_user(
                        "图片识别将使用大模型回答，请确保大模型支持图片识别。"
                    )
                    actions.append(ChooseOptionByImageAction())
                elif action == SupportAction.REPLY_BY_CALCULATION_PROBLEM:
                    print_to_user("计算题将使用大模型回答。")
                    actions.append(ReplyByCalculationProblemAction())
                else:
                    raise ValueError(f"不支持的动作: {action}")
                if local_input_("是否继续添加动作？(y/N)：").strip().lower() != "y":
                    break
            except (ValueError, ValidationError) as e:
                print_to_user("错误: ")
                print_to_user(e)
        input_.incr()
        return actions

    def ask_one(self) -> SignChatV3:
        input_ = UserInput(numbering_lang="chinese_simple")
        chat_id = int(input_("Chat ID（登录时最近对话输出中的ID）: "))
        name = input_("Chat名称（可选）: ")
        thread_id_str = input_("话题 Thread ID（可选，仅群聊话题模式需要，直接回车跳过）: ").strip()
        thread_id = int(thread_id_str) if thread_id_str else None
        actions = self._ask_actions(input_)
        delete_after = (
            input_(
                "等待N秒后删除消息（发送消息后等待进行删除, '0'表示立即删除, 不需要删除直接回车）, N: "
            )
            or None
        )
        if delete_after:
            delete_after = int(delete_after)
        enabled = input_("是否默认启用该签到任务(Y/n): ").lower() != "n"
        cfgs = {
            "chat_id": chat_id,
            "name": name,
            "enabled": enabled,
            "thread_id": thread_id,
            "delete_after": delete_after,
            "actions": actions,
        }
        return SignChatV3.model_validate(cfgs)

    def ask_for_config(self) -> "SignConfigV3":
        chats = []
        i = 1
        print_to_user(f"开始配置任务<{self.task_name}>\n")
        while True:
            print_to_user(f"第{i}个任务: ")
            try:
                chat = self.ask_one()
                print_to_user(chat)
                print_to_user(f"第{i}个任务配置成功\n")
                chats.append(chat)
            except Exception as e:
                print_to_user(e)
                print_to_user("配置失败")
                i -= 1
            continue_ = input("继续配置任务？(y/N)：")
            if continue_.strip().lower() != "y":
                break
            i += 1
        sign_at_prompt = "签到时间（time或crontab表达式，如'06:00:00'或'0 6 * * *'）: "
        sign_at_str = input(sign_at_prompt) or "06:00:00"
        while not (sign_at := self._validate_sign_at(sign_at_str)):
            print_to_user("请输入正确的时间格式")
            sign_at_str = input(sign_at_prompt) or "06:00:00"

        random_seconds_str = input("签到时间误差随机秒数（默认为0）: ") or "0"
        random_seconds = int(float(random_seconds_str))
        config = SignConfigV3.model_validate(
            {
                "chats": chats,
                "sign_at": sign_at,
                "random_seconds": random_seconds,
            }
        )
        if config.requires_ai:
            print_to_user(OPENAI_USE_PROMPT)
        return config

    def _validate_sign_at(
        self,
        sign_at_str: str,
    ) -> Optional[str]:
        sign_at_str = sign_at_str.replace("：", ":").strip()

        try:
            sign_at = dt_time.fromisoformat(sign_at_str)
            crontab_expr = self._time_to_crontab(sign_at)
        except ValueError:
            try:
                croniter(sign_at_str)
                crontab_expr = sign_at_str
            except CroniterBadCronError:
                self.log(f"时间格式错误: {sign_at_str}", level="error")
                return None
        return crontab_expr

    @staticmethod
    def _time_to_crontab(sign_at: dt_time) -> str:
        return f"{sign_at.minute} {sign_at.hour} * * *"

    def load_sign_record(self):
        sign_record = {}
        if not self.sign_record_file.is_file():
            with open(self.sign_record_file, "w", encoding="utf-8") as fp:
                json.dump(sign_record, fp)
        else:
            with open(self.sign_record_file, "r", encoding="utf-8") as fp:
                sign_record = json.load(fp)
        return sign_record

    def _save_sign_record(self, sign_record: dict):
        with open(self.sign_record_file, "w", encoding="utf-8") as fp:
            json.dump(sign_record, fp)

    async def sign_a_chat(
        self,
        chat: SignChatV3,
    ):
        self.log(f"开始执行: \n{chat}")
        for action in chat.actions:
            self.log(f"等待处理动作: {action}")
            await self.wait_for(chat, action)
            self.log(f"处理完成: {action}")
            self.context.waiting_message = None
            await asyncio.sleep(chat.action_interval)

    async def _run_action_group_loop(
        self,
        chat: SignChatV3,
        group_index: int,
        group: "ActionGroup",
        default_sign_at: str,
        default_random_seconds: int,
        sign_record: dict,
        only_once: bool,
        force_rerun: bool,
    ):
        """单个 ActionGroup 的独立调度循环。"""
        effective_sign_at = self._validate_sign_at(group.sign_at or default_sign_at)
        effective_random_seconds = group.random_seconds or default_random_seconds
        record_key_prefix = f"g_{chat.chat_id}_{group_index}"

        # 构造一个仅含本 group actions 的临时 SignChatV3 用于执行
        group_chat = chat.model_copy(
            update={
                "actions": group.actions,
                "action_interval": group.action_interval,
                "action_groups": None,
            }
        )
        self.context.sign_chats[chat.chat_id].append(group_chat)

        while True:
            now = get_now()
            record_key = f"{record_key_prefix}_{now.date()}"

            def need_run():
                if force_rerun:
                    return True
                if record_key not in sign_record:
                    return True
                _last_at = datetime.fromisoformat(sign_record[record_key])
                self.log(f"[Group {group_index}] 上次执行时间: {_last_at}")
                _cron_it = croniter(effective_sign_at, _last_at)
                _next = _cron_it.next(datetime)
                if _next > now:
                    self.log(f"[Group {group_index}] 未到下次执行时间，无需执行")
                    return False
                return True

            if need_run():
                self.log(f"[Group {group_index}] 开始执行 chat={chat.chat_id}")
                try:
                    await self.sign_a_chat(group_chat)
                except (errors.RPCError, OSError, ConnectionError, TimeoutError) as _e:
                    self.log(
                        f"[Group {group_index}] 执行失败: {_e}", level="WARNING"
                    )
                    logger.warning(_e, exc_info=True)
                sign_record[record_key] = now.isoformat()
                self._save_sign_record(sign_record)

            if only_once:
                break

            cron_it = croniter(effective_sign_at, get_now())
            next_run: datetime = cron_it.next(datetime) + timedelta(
                seconds=random.randint(0, int(effective_random_seconds))
            )
            self.log(f"[Group {group_index}] chat={chat.chat_id} 下次运行: {next_run}")
            await asyncio.sleep((next_run - get_now()).total_seconds())

    async def _run_with_action_groups(
        self,
        num_of_dialogs: int = 20,
        only_once: bool = False,
        force_rerun: bool = False,
    ):
        """当存在 action_groups 时，在单一 client 连接内并发调度所有 groups。"""
        if self.user is None:
            await self.login(num_of_dialogs, print_chat=True)

        config = self.load_config(self.cfg_cls)
        if config.requires_ai:
            self.ensure_ai_cfg()

        sign_record = self.load_sign_record()
        chat_ids = [c.chat_id for c in config.chats]

        self.log(f"[ActionGroups] 为以下Chat添加消息回调处理函数：{chat_ids}")
        self.app.add_handler(
            MessageHandler(self.on_message, filters.chat(chat_ids))
        )
        self.app.add_handler(
            EditedMessageHandler(self.on_edited_message, filters.chat(chat_ids))
        )

        while True:
            try:
                async with self.app:
                    self.log("[ActionGroups] 开始连接...")
                    tasks = []
                    for chat in config.chats:
                        if not getattr(chat, 'enabled', True):
                            self.log(f"群聊任务 {chat.chat_id} 已禁用，跳过", level="DEBUG")
                            continue
                        if chat.action_groups:
                            for i, group in enumerate(chat.action_groups):
                                if not getattr(group, 'enabled', True):
                                    self.log(f"群聊任务 {chat.chat_id} 的组 {i} 已禁用，跳过", level="DEBUG")
                                    continue
                                tasks.append(
                                    self._run_action_group_loop(
                                        chat=chat,
                                        group_index=i,
                                        group=group,
                                        default_sign_at=config.sign_at,
                                        default_random_seconds=config.random_seconds,
                                        sign_record=sign_record,
                                        only_once=only_once,
                                        force_rerun=force_rerun,
                                    )
                                )
                        elif chat.actions:
                            # 无 action_groups 的 chat：用 config.sign_at 包装成单 group
                            legacy_group = ActionGroup(
                                sign_at=config.sign_at,
                                actions=chat.actions,
                                action_interval=chat.action_interval,
                                random_seconds=config.random_seconds,
                            )
                            tasks.append(
                                self._run_action_group_loop(
                                    chat=chat,
                                    group_index=0,
                                    group=legacy_group,
                                    default_sign_at=config.sign_at,
                                    default_random_seconds=config.random_seconds,
                                    sign_record=sign_record,
                                    only_once=only_once,
                                    force_rerun=force_rerun,
                                )
                            )
                    if tasks:
                        await asyncio.gather(*tasks)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as e:
                wait_time = 10 + random.random() * 5
                self.log(
                    f"[ActionGroups] 连接异常中断: {e}，将在 {wait_time:.1f} 秒后尝试重新连接...",
                    level="WARNING",
                )
                await asyncio.sleep(wait_time)
            if only_once:
                break

    async def run(
        self, num_of_dialogs=20, only_once: bool = False, force_rerun: bool = False
    ):
        if self.app.in_memory or self.app.session_string:
            return await self.in_memory_run(
                num_of_dialogs, only_once=only_once, force_rerun=force_rerun
            )
        return await self.normal_run(
            num_of_dialogs, only_once=only_once, force_rerun=force_rerun
        )

    async def in_memory_run(
        self, num_of_dialogs=20, only_once: bool = False, force_rerun: bool = False
    ):
        async with self.app:
            await self.normal_run(
                num_of_dialogs, only_once=only_once, force_rerun=force_rerun
            )

    async def normal_run(
        self, num_of_dialogs=20, only_once: bool = False, force_rerun: bool = False
    ):
        # 若存在 action_groups，进入多 group 并发调度模式
        config = self.load_config(self.cfg_cls)
        if any(c.action_groups for c in config.chats):
            return await self._run_with_action_groups(
                num_of_dialogs, only_once=only_once, force_rerun=force_rerun
            )

        if self.user is None:
            await self.login(num_of_dialogs, print_chat=True)

        if config.requires_ai:
            self.ensure_ai_cfg()

        sign_record = self.load_sign_record()
        chat_ids = [c.chat_id for c in config.chats if getattr(c, 'enabled', True)]

        async def sign_once():
            for chat in config.chats:
                if not getattr(chat, 'enabled', True):
                    continue
                self.context.sign_chats[chat.chat_id].append(chat)
                try:
                    await self.sign_a_chat(chat)
                except errors.RPCError as _e:
                    self.log(f"签到失败: {_e} \nchat: \n{chat}")
                    logger.warning(_e, exc_info=True)
                    continue

                self.context.chat_messages[chat.chat_id].clear()
                await asyncio.sleep(config.sign_interval)
            sign_record[str(now.date())] = now.isoformat()
            self._save_sign_record(sign_record)

        def need_sign(last_date_str):
            if force_rerun:
                return True
            if last_date_str not in sign_record:
                return True
            _last_sign_at = datetime.fromisoformat(sign_record[last_date_str])
            self.log(f"上次执行时间: {_last_sign_at}")
            _cron_it = croniter(self._validate_sign_at(config.sign_at), _last_sign_at)
            _next_run: datetime = _cron_it.next(datetime)
            if _next_run > now:
                self.log("当前未到下次执行时间，无需执行")
                return False
            return True

        self.log(f"为以下Chat添加消息回调处理函数：{chat_ids}")
        self.app.add_handler(
            MessageHandler(self.on_message, filters.chat(chat_ids))
        )
        self.app.add_handler(
            EditedMessageHandler(self.on_edited_message, filters.chat(chat_ids))
        )
        while True:
            try:
                async with self.app:
                    config = self.load_config(self.cfg_cls)  # 循环内加载，配置实时生效
                    now = get_now()
                    self.log(f"当前时间: {now}")
                    now_date_str = str(now.date())
                    self.context = self.ensure_ctx()
                    if need_sign(now_date_str):
                        await sign_once()

            except (OSError, errors.Unauthorized) as e:
                logger.exception(e)
                await asyncio.sleep(30)
                continue

            if only_once:
                break
            cron_it = croniter(self._validate_sign_at(config.sign_at), now)
            next_run: datetime = cron_it.next(datetime) + timedelta(
                seconds=random.randint(0, int(config.random_seconds))
            )
            self.log(f"下次运行时间: {next_run}")
            await asyncio.sleep((next_run - now).total_seconds())

    async def run_once(self, num_of_dialogs):
        return await self.run(num_of_dialogs, only_once=True, force_rerun=True)

    async def send_text(
        self, chat_id: int, text: str, delete_after: int = None,
        message_thread_id: int = None, **kwargs
    ):
        if self.user is None:
            await self.login(print_chat=False)
        async with self.app:
            await self.send_message(chat_id, text, delete_after,
                                    message_thread_id=message_thread_id, **kwargs)

    async def send_dice_cli(
        self,
        chat_id: Union[str, int],
        emoji: str = "🎲",
        delete_after: int = None,
        message_thread_id: int = None,
        **kwargs,
    ):
        if self.user is None:
            await self.login(print_chat=False)
        async with self.app:
            await self.send_dice(chat_id, emoji, delete_after,
                                 message_thread_id=message_thread_id, **kwargs)

    async def _on_message(self, client: Client, message: Message):
        chats = self.context.sign_chats.get(message.chat.id)
        if not chats:
            self.log("忽略意料之外的聊天", level="WARNING")
            return
        self.context.chat_messages[message.chat.id][message.id] = message

    async def on_message(self, client: Client, message: Message):
        self.log(
            f"收到来自「{message.from_user.username or message.from_user.id}」的消息: {readable_message(message)}"
        )
        await self._on_message(client, message)

    async def on_edited_message(self, client, message: Message):
        self.log(
            f"收到来自「{message.from_user.username or message.from_user.id}」对消息的更新，消息: {readable_message(message)}"
        )
        # 避免更新正在处理的消息，等待处理完成
        while (
            self.context.waiting_message
            and self.context.waiting_message.id == message.id
        ):
            await asyncio.sleep(0.3)
        await self._on_message(client, message)

    async def _click_keyboard_by_text(
        self, action: ClickKeyboardByTextAction, message: Message
    ):
        if reply_markup := message.reply_markup:
            if isinstance(reply_markup, InlineKeyboardMarkup):
                flat_buttons = (b for row in reply_markup.inline_keyboard for b in row)
                option_to_btn: dict[str, InlineKeyboardButton] = {}
                for btn in flat_buttons:
                    option_to_btn[btn.text] = btn
                    if action.text in btn.text:
                        self.log(f"点击按钮: {btn.text}")
                        try:
                            await self.request_callback_answer(
                                self.app,
                                message.chat.id,
                                message.id,
                                btn.callback_data,
                            )
                        except errors.MessageDeleted:
                            self.log("尝试点击时消息已被删除", level="DEBUG")
                        except Exception as e:
                            self.log(f"请求回调发生异常: {e}", level="WARNING")
                        return True
        return False

    async def _reply_by_calculation_problem(
        self, action: ReplyByCalculationProblemAction, message
    ):
        if message.text:
            self.log("检测到文本回复，尝试调用大模型进行计算题回答")
            self.log(f"问题: \n{message.text}")
            answer = await self.get_ai_tools().calculate_problem(message.text)
            self.log(f"回答为: {answer}")
            await self.send_message(message.chat.id, answer)
            return True
        return False

    async def _choose_option_by_image(self, action: ChooseOptionByImageAction, message):
        if reply_markup := message.reply_markup:
            if isinstance(reply_markup, InlineKeyboardMarkup) and message.photo:
                flat_buttons = (b for row in reply_markup.inline_keyboard for b in row)
                option_to_btn = {btn.text: btn for btn in flat_buttons if btn.text}
                self.log("检测到图片，尝试调用大模型进行图片识别并选择选项")
                image_buffer: BinaryIO = await self.app.download_media(
                    message.photo.file_id, in_memory=True
                )
                image_buffer.seek(0)
                image_bytes = image_buffer.read()
                options = list(option_to_btn)
                result_index = await self.get_ai_tools().choose_option_by_image(
                    image_bytes,
                    "选择正确的选项",
                    list(enumerate(options)),
                )
                result = options[result_index]
                self.log(f"选择结果为: {result}")
                target_btn = option_to_btn.get(result.strip())
                if not target_btn:
                    self.log("未找到匹配的按钮", level="WARNING")
                    return False
                await self.request_callback_answer(
                    self.app,
                    message.chat.id,
                    message.id,
                    target_btn.callback_data,
                )
                return True
        return False

    async def wait_for(self, chat: SignChatV3, action: ActionT, timeout=10):
        if isinstance(action, SendTextAction):
            return await self.send_message(
                chat.chat_id, action.text, chat.delete_after,
                message_thread_id=chat.thread_id,
            )
        elif isinstance(action, SendDiceAction):
            return await self.send_dice(
                chat.chat_id, action.dice, chat.delete_after,
                message_thread_id=chat.thread_id,
            )
        self.context.waiter.add(chat.chat_id)
        start = time.perf_counter()
        last_message = None
        while time.perf_counter() - start < timeout:
            await asyncio.sleep(0.3)
            messages_dict = self.context.chat_messages.get(chat.chat_id)
            if not messages_dict:
                continue
            messages = list(messages_dict.values())
            # 暂无新消息
            if messages[-1] == last_message:
                continue
            last_message = messages[-1]
            for message in messages:
                self.context.waiting_message = message
                ok = False
                if isinstance(action, ClickKeyboardByTextAction):
                    ok = await self._click_keyboard_by_text(action, message)
                elif isinstance(action, ReplyByCalculationProblemAction):
                    ok = await self._reply_by_calculation_problem(action, message)
                elif isinstance(action, ChooseOptionByImageAction):
                    ok = await self._choose_option_by_image(action, message)
                if ok:
                    self.context.waiter.sub(message.chat.id)
                    # 将消息ID对应value置为None，保证收到消息的编辑时消息所处的顺序
                    self.context.chat_messages[chat.chat_id][message.id] = None
                    return None
                self.log(f"忽略消息: {readable_message(message)}")
        self.log(f"等待超时: \nchat: \n{chat} \naction: {action}", level="WARNING")
        return None

    async def request_callback_answer(
        self,
        client: Client,
        chat_id: Union[int, str],
        message_id: int,
        callback_data: Union[str, bytes],
        **kwargs,
    ):
        try:
            await self._call_telegram_api(
                "messages.GetBotCallbackAnswer",
                lambda: client.request_callback_answer(
                    chat_id, message_id, callback_data=callback_data, **kwargs
                ),
            )
            self.log("点击完成")
        except (errors.BadRequest, TimeoutError) as e:
            self.log(e, level="ERROR")

    async def schedule_messages(
        self,
        chat_id: Union[int, str],
        text: str,
        crontab: str = None,
        next_times: int = 1,
        random_seconds: int = 0,
    ):
        now = get_now()
        it = croniter(crontab, start_time=now)
        if self.user is None:
            await self.login(print_chat=False)
        results = []
        async with self.app:
            for n in range(next_times):
                next_dt: datetime = it.next(ret_type=datetime) + timedelta(
                    seconds=random.randint(0, random_seconds)
                )
                results.append({"at": next_dt.isoformat(), "text": text})
                await self._call_telegram_api(
                    "messages.SendScheduledMessage",
                    lambda schedule_date=next_dt: self.app.send_message(
                        chat_id,
                        text,
                        schedule_date=schedule_date,
                    ),
                )
                await asyncio.sleep(0.1)
                print_to_user(f"已配置次数：{n + 1}")
        self.log(f"已配置定时发送消息，次数{next_times}")
        return results

    async def get_schedule_messages(self, chat_id):
        if self.user is None:
            await self.login(print_chat=False)
        async with self.app:
            messages = await self._call_telegram_api(
                "messages.GetScheduledHistory",
                lambda: self.app.get_scheduled_messages(chat_id),
            )
            for message in messages:
                print_to_user(f"{message.date}: {message.text}")


class UserMonitor(BaseUserWorker[MonitorConfig]):
    _workdir = ".monitor"
    _tasks_dir = "monitors"
    cfg_cls = MonitorConfig
    config: MonitorConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clicked_callbacks: dict[str, float] = {}
        self._handled_messages: dict[str, float] = {}

    def ask_one(self):
        input_ = UserInput()
        chat_id = (input_("Chat ID（登录时最近对话输出中的ID）: ")).strip()
        if not chat_id.startswith("@"):
            chat_id = int(chat_id)
        thread_id_str = input_("话题 Thread ID（可选，仅监控特定群聊话题时填写，直接回车跳过）: ").strip()
        thread_id = int(thread_id_str) if thread_id_str else None
        rules = ["exact", "contains", "regex", "all"]
        while rule := (input_(f"匹配规则({', '.join(rules)}): ") or "exact"):
            if rule in rules:
                break
            print_to_user("不存在的规则, 请重新输入!")
        rule_value = None
        if rule != "all":
            while not (rule_value := input_("规则值（不可为空）: ")):
                print_to_user("不可为空！")
                continue
        from_user_ids = (
            input_(
                "只匹配来自特定用户ID的消息（多个用逗号隔开, 匹配所有用户直接回车）: "
            )
            or None
        )
        always_ignore_me = input_("总是忽略自己发送的消息（y/N）: ").lower() == "y"
        if from_user_ids:
            from_user_ids = [
                i if i.startswith("@") else int(i) for i in from_user_ids.split(",")
            ]
        default_send_text = input_("默认发送文本（不需要则回车）: ") or None
        ai_reply = False
        ai_prompt = None
        use_ai_reply = input_("是否使用AI进行回复(y/N): ") or "n"
        if use_ai_reply.lower() == "y":
            ai_reply = True
            while not (ai_prompt := input_("输入你的提示词（作为`system prompt`）: ")):
                print_to_user("不可为空！")
                continue
            print_to_user(OPENAI_USE_PROMPT)

        send_text_search_regex = None
        ai_phishing_check = False
        ai_phishing_prompt = None
        if not ai_reply:
            send_text_search_regex = (
                input_("从消息中提取发送文本的正则表达式（不需要则直接回车）: ") or None
            )
            if send_text_search_regex:
                ai_phishing_check = input_("是否启用于防钓鱼语义审查（过滤侮辱、诈骗口令）(y/N): ").lower() == "y"
                if ai_phishing_check:
                    ai_phishing_prompt = input_("AI 防钓鱼检测 Prompt （直接回车使用默认）: ") or None

        click_inline_keyboard_button = None
        if not default_send_text and not ai_reply:
            click_inline_keyboard_button = (
                input_("自动点击按钮文本（如果消息带内联键盘，包含此文本的按钮将被点击，不需要则回车）: ") or None
            )

        amount_search_regex = None
        min_amount = None
        if default_send_text or ai_reply or send_text_search_regex or click_inline_keyboard_button:
            amount_search_regex = input_("用于提取金额用于过滤的正则表达式（如提取红包金额，不需要则直接回车）: ") or None
            if amount_search_regex:
                min_amount_str = input_("触发操作的最小金额（提取的金额小于等于该值将被忽略）: ").strip()
                min_amount = float(min_amount_str) if min_amount_str else None

            delay_str = input_("响应延迟（秒，匹配后等待多久再发送，直接回车则不延迟）: ").strip()
            delay = float(delay_str) if delay_str else 0
            send_times_str = input_("发送次数（触发后重复发送几次，默认为1）: ").strip()
            send_times = int(send_times_str) if send_times_str else 1
            send_interval = 1.0
            if send_times > 1:
                send_interval_str = input_("多次发送时每次之间的间隔秒数（默认为1秒）: ").strip()
                send_interval = float(send_interval_str) if send_interval_str else 1.0
            delete_after = (
                input_(
                    "发送消息后等待N秒进行删除（'0'表示立即删除, 不需要删除直接回车）， N: "
                )
                or None
            )
            if delete_after:
                delete_after = int(delete_after)
                
            time_window = input_(
                "仅在指定时间段内触发（如 08:00-23:30，表示只在早8点到晚11点半之间工作，不需要限制直接回车）: "
            ).strip() or None
                
            forward_to_chat_id = (
                input_("转发消息到该聊天ID，默认为消息来源：")
            ).strip()
            if forward_to_chat_id and not forward_to_chat_id.startswith("@"):
                forward_to_chat_id = int(forward_to_chat_id)
            forward_to_thread_id_str = input_(
                "转发到该话题 Thread ID（可选，不填则与来源话题相同，直接回车跳过）: "
            ).strip()
            forward_to_thread_id = int(forward_to_thread_id_str) if forward_to_thread_id_str else None
            enabled = input_("是否默认启用该监控任务(Y/n): ").lower() != "n"
        else:
            delay = 0
            send_times = 1
            send_interval = 1.0
            delete_after = None
            time_window = None
            forward_to_chat_id = None
            forward_to_thread_id = None
            enabled = input_("是否默认启用该监控任务(Y/n): ").lower() != "n"

        push_via_server_chan = (
            input_("是否通过Server酱推送消息(y/N): ") or "n"
        ).lower() == "y"
        server_chan_send_key = None
        if push_via_server_chan:
            server_chan_send_key = (
                input_(
                    "Server酱的SendKey（不填将从环境变量`SERVER_CHAN_SEND_KEY`读取）: "
                )
                or None
            )

        forward_to_external = (
            input_("是否需要转发到外部（UDP, Http）(y/N): ").lower() == "y"
        )
        external_forwards = None
        if forward_to_external:
            external_forwards = []
            if input_("是否需要转发到UDP(y/N): ").lower() == "y":
                addr = input_("请输入UDP服务器地址和端口（形如`127.0.0.1:1234`）: ")
                host, port = addr.split(":")
                external_forwards.append(
                    {
                        "host": host,
                        "port": int(port),
                    }
                )

            if input_("是否需要转发到Http(y/N): ").lower() == "y":
                url = input_("请输入Http地址（形如`http://127.0.0.1:1234`）: ")
                external_forwards.append(
                    {
                        "url": url,
                    }
                )

        return MatchConfig.model_validate(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "enabled": enabled,
                "rule": rule,
                "rule_value": rule_value,
                "from_user_ids": from_user_ids,
                "always_ignore_me": always_ignore_me,
                "default_send_text": default_send_text,
                "click_inline_keyboard_button": click_inline_keyboard_button,
                "ai_reply": ai_reply,
                "ai_prompt": ai_prompt,
                "ai_phishing_check": ai_phishing_check,
                "ai_phishing_prompt": ai_phishing_prompt,
                "send_text_search_regex": send_text_search_regex,
                "amount_search_regex": amount_search_regex,
                "min_amount": min_amount,
                "time_window": time_window,
                "delay": delay,
                "send_times": send_times,
                "send_interval": send_interval,
                "delete_after": delete_after,
                "forward_to_chat_id": forward_to_chat_id,
                "forward_to_thread_id": forward_to_thread_id,
                "push_via_server_chan": push_via_server_chan,
                "server_chan_send_key": server_chan_send_key,
                "external_forwards": external_forwards,
            }
        )

    def ask_for_config(self) -> "MonitorConfig":
        i = 1
        print_to_user(f"开始配置任务<{self.task_name}>")
        print_to_user(
            "聊天chat id和用户user id均同时支持整数id和字符串username, username必须以@开头，如@neo"
        )
        match_cfgs = []
        while True:
            print_to_user(f"\n配置第{i}个监控项")
            try:
                match_cfgs.append(self.ask_one())
            except Exception as e:
                print_to_user(e)
                print_to_user("配置失败")
                i -= 1
            continue_ = input("继续配置？(y/N)：")
            if continue_.strip().lower() != "y":
                break
            i += 1
        config = MonitorConfig(match_cfgs=match_cfgs)
        if config.requires_ai:
            print_to_user(OPENAI_USE_PROMPT)
        return config

    @classmethod
    async def udp_forward(cls, f: UDPForward, message: Message):
        data = str(message).encode("utf-8")
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(), remote_addr=(f.host, f.port)
        )
        try:
            transport.sendto(data)
        finally:
            transport.close()

    @classmethod
    async def http_api_callback(cls, f: HttpCallback, message: Message):
        headers = f.headers or {}
        headers.update({"Content-Type": "application/json"})
        content = str(message).encode("utf-8")
        async with httpx.AsyncClient() as client:
            await client.post(
                str(f.url),
                content=content,
                headers=headers,
                timeout=10,
            )

    async def forward_to_external(self, match_cfg: MatchConfig, message: Message):
        if not match_cfg.external_forwards:
            return
        for forward in match_cfg.external_forwards:
            self.log(f"转发消息至{forward}")
            if isinstance(forward, UDPForward):
                asyncio.create_task(
                    self.udp_forward(
                        forward,
                        message,
                    )
                )
            elif isinstance(forward, HttpCallback):
                asyncio.create_task(
                    self.http_api_callback(
                        forward,
                        message,
                    )
                )

    async def request_callback_answer(
        self,
        client: Client,
        chat_id: Union[int, str],
        message_id: int,
        callback_data: Union[str, bytes],
        **kwargs,
    ):
        try:
            result = await client.request_callback_answer(
                chat_id, message_id, callback_data=callback_data, **kwargs
            )
            ans = getattr(result, "message", result)
            self.log(f"点击完成, 服务器返回: {ans}")
        except Exception as e:
            self.log(f"点击回调失败: {type(e).__name__} - {e}", level="ERROR")

    async def _click_keyboard_by_text(
        self, action: ClickKeyboardByTextAction, message: Message
    ):
        # 清理过期的回调记录 (保留 1 小时内的记录)
        current_time = time.time()
        expired_keys = [k for k, v in self._clicked_callbacks.items() if current_time - v > 3600]
        for k in expired_keys:
            del self._clicked_callbacks[k]

        if reply_markup := message.reply_markup:
            if isinstance(reply_markup, InlineKeyboardMarkup):
                flat_buttons = (b for row in reply_markup.inline_keyboard for b in row)
                option_to_btn: dict[str, InlineKeyboardButton] = {}
                for btn in flat_buttons:
                    option_to_btn[btn.text] = btn
                    if action.text in btn.text:
                        if btn.callback_data:
                            cb_str = btn.callback_data.decode() if isinstance(btn.callback_data, bytes) else btn.callback_data
                            if cb_str in self._clicked_callbacks:
                                self.log(f"按钮 {btn.text} 的 callback_data={cb_str} 已经点击过，跳过", level="DEBUG")
                                return True
                            self._clicked_callbacks[cb_str] = current_time

                        self.log(f"找到匹配按钮: {btn.text} | callback_data={btn.callback_data} | url={btn.url}")
                        try:
                            if btn.callback_data:
                                await self.request_callback_answer(
                                    self.app,
                                    message.chat.id,
                                    message.id,
                                    btn.callback_data,
                                )
                            elif btn.url:
                                self.log(f"该按钮是 URL 按钮，无法通过 callback_data 点击: {btn.url}")
                                # 如果是启动机器人的 deep link URL，可以尝试解析并发 start 消息
                                if "start=" in btn.url:
                                    bot_username = btn.url.split("?")[0].split("/")[-1]
                                    payload = btn.url.split("start=")[-1]
                                    self.log(f"解析到 Deep Link，尝试向 {bot_username} 发送 /start {payload}")
                                    await self.app.send_message(bot_username, f"/start {payload}")
                        except errors.MessageDeleted:
                            self.log("尝试点击时消息已被删除", level="DEBUG")
                        except Exception as e:
                            self.log(f"点击动作发生异常: {e}", level="WARNING")
                        return True
        return False

    async def on_message(self, client, message: Message):
        # 清理过期的已处理消息记录 (保留 1 小时内的记录)
        current_time = time.time()
        expired_handled = [k for k, v in self._handled_messages.items() if current_time - v > 3600]
        for k in expired_handled:
            del self._handled_messages[k]

        for match_cfg in self.config.match_cfgs:
            if not getattr(match_cfg, 'enabled', True):
                continue
                
            # 增加对 Chat ID 和 Thread ID 的预校验，减少不相关的调试日志输出
            if not match_cfg.match_chat(message.chat):
                continue
            if not match_cfg.match_thread(message):
                continue
                
            if not match_cfg.is_in_time_window():
                continue

            # 仅在话题匹配时输出调试日志，解决日志中大量 Topic: None 的混淆
            safe_text = str(message.text or "")
            self.log(f"DEBUG: 收到匹配话题的消息 - Chat: {message.chat.id}, Topic: {message.message_thread_id}, From: {message.from_user.id if message.from_user else 'None'}, Text: {safe_text[:50]}...")

            if not match_cfg.match(message):
                continue

            handled_key = f"{message.chat.id}_{message.id}_{id(match_cfg)}"
            if handled_key in self._handled_messages:
                # 已经处理过同一个消息的这条规则，防止因为编辑消息导致重复触发
                continue
            self._handled_messages[handled_key] = current_time

            self.log(f"匹配到监控项：{match_cfg}")
            
            if match_cfg.amount_search_regex and match_cfg.min_amount is not None:
                amount_m = re.search(match_cfg.amount_search_regex, message.text)
                if amount_m:
                    try:
                        amount = float(amount_m.group(1))
                        if amount <= match_cfg.min_amount:
                            self.log(f"红包金额 {amount} 不大于限定的最小金额 {match_cfg.min_amount}，跳过~", level="INFO")
                            continue
                    except ValueError:
                        self.log(f"无法将提取到的金额转换为数字: {amount_m.group(1)}", level="WARNING")
            
            if match_cfg.delay > 0:
                self.log(f"延迟 {match_cfg.delay} 秒后回复...")
                await asyncio.sleep(match_cfg.delay)
                # 延迟后二次校验消息状态，防止红包在延迟期间被抢完
                try:
                    refreshed_msg = await self.app.get_messages(message.chat.id, message.id)
                    if refreshed_msg and refreshed_msg.text:
                        if "已被抢完" in refreshed_msg.text or "已过期" in refreshed_msg.text:
                            self.log("发包前急刹车：检测到红包已被抢完或已过期，放弃发口令。", level="INFO")
                            continue
                except Exception as e:
                    self.log(f"发包前二次校验失败({e})，继续尝试发包。", level="DEBUG")
            await self.forward_to_external(match_cfg, message)
            try:
                send_text = await self.get_send_text(match_cfg, message)
                if not send_text:
                    self.log("发送内容为空", level="WARNING")
                else:
                    if getattr(match_cfg, 'ai_phishing_check', False):
                        from tg_signer.ai_tools import OpenAIConfigManager, AITools
                        cfg = OpenAIConfigManager(self.workdir).load_config()
                        if cfg:
                            ai_tools = AITools(cfg)
                            prompt = match_cfg.ai_phishing_prompt or "你是一个抢红包机器人的口令安全审核员。为了不暴露自己是自动程序而非真人，你必须严格审核目标口令的内容。如果口令含有暴露机器身份的词（如“我是人机”、“我是脚本”、“我是外挂”、“我是狗”）或任何谐音梗（如“我是人鸡”、“我是人基”等），或者是引诱转账骗局、侮辱性陷阱（如“我是傻逼”），请你严格过滤并只回复纯文本「PASS_PHISHING」。如果它是正常语句或群友常用的安全口令，请你直接原封不动地回复原口令本身，绝对不要添加任何标点符号、引号或解释说明。"
                            self.log("正在使用 AI 审查口令语义...")
                            try:
                                ai_result = await ai_tools.get_reply(prompt, send_text)
                                if "PASS_PHISHING" in ai_result:
                                    self.log(f"AI 拦截了涉嫌钓鱼/侮辱的口令: {send_text}", level="WARNING")
                                    continue
                            except Exception as e:
                                self.log(f"AI 防钓鱼检测失败或超时 ({e})，按照安全策略拦截口令: {send_text}", level="WARNING")
                                continue

                    forward_to_chat_id = match_cfg.forward_to_chat_id or message.chat.id
                    # 确定目标话题：优先用 forward_to_thread_id，其次继承来源消息中的话题
                    forward_thread_id = match_cfg.forward_to_thread_id
                    if forward_thread_id is None and not match_cfg.forward_to_chat_id:
                        # 回复到原始聊天，继承来源话题
                        forward_thread_id = message.message_thread_id
                    send_times = max(1, match_cfg.send_times)
                    self.log(f"发送文本：{send_text}至{forward_to_chat_id}" +
                             (f"（话题 {forward_thread_id}）" if forward_thread_id is not None else "") +
                             (f"，共发送 {send_times} 次" if send_times > 1 else ""))
                    for i in range(send_times):
                        if i > 0 and match_cfg.send_interval > 0:
                            await asyncio.sleep(match_cfg.send_interval)
                        await self.send_message(
                            forward_to_chat_id,
                            send_text,
                            delete_after=match_cfg.delete_after,
                            message_thread_id=forward_thread_id,
                        )

                if match_cfg.click_inline_keyboard_button:
                    if message.text and ("已被抢完" in message.text or "已过期" in message.text):
                        self.log("消息中包含「已被抢完」或「已过期」，跳过点击尝试", level="DEBUG")
                    else:
                        action = ClickKeyboardByTextAction(text=match_cfg.click_inline_keyboard_button)
                        clicked = await self._click_keyboard_by_text(action, message)
                        if not clicked:
                            self.log(f"未能找到包含 {match_cfg.click_inline_keyboard_button} 的按钮", level="WARNING")

                if match_cfg.push_via_server_chan:
                    server_chan_send_key = (
                        match_cfg.server_chan_send_key
                        or os.environ.get("SERVER_CHAN_SEND_KEY")
                    )
                    if not server_chan_send_key:
                        self.log("未配置Server酱的SendKey", level="WARNING")
                    else:
                        await sc_send(
                            server_chan_send_key,
                            f"匹配到监控项：{match_cfg.chat_id}",
                            f"消息内容为:\n\n{message.text}",
                        )
            except IndexError as e:
                logger.exception(e)

    async def on_edited_message(self, client, message: Message):
        if message.from_user:
            user_name = message.from_user.username or message.from_user.id
        elif message.sender_chat:
            user_name = message.sender_chat.title or message.sender_chat.id
        else:
            user_name = "Unknown"
        self.log(f"收到来自「{user_name}」对消息的更新")
        await self.on_message(client, message)

    async def get_send_text(self, match_cfg: MatchConfig, message: Message) -> str:
        send_text = match_cfg.get_send_text(message.text)
        if match_cfg.ai_reply and match_cfg.ai_prompt:
            send_text = await self.get_ai_tools().get_reply(
                match_cfg.ai_prompt,
                message.text,
            )
        return send_text

    async def run(self, num_of_dialogs=20):
        if self.user is None:
            await self.login(num_of_dialogs, print_chat=True)

        cfg = self.load_config(self.cfg_cls)
        if cfg.requires_ai:
            self.ensure_ai_cfg()

        # 增加判空过滤器，防止 monkey patch 返回 None 时导致的 filters.chat 崩溃
        self.app.add_handler(
            MessageHandler(self.on_message, filters.create(lambda _, __, m: m is not None) & filters.chat(cfg.chat_ids)),
            group=1
        )
        self.app.add_handler(
            EditedMessageHandler(self.on_edited_message),
            group=1
        )


        while True:
            try:
                async with self.app:
                    self.log("开始监控...")
                    for chat_id in cfg.chat_ids:
                        try:
                            await self.app.resolve_peer(chat_id)
                        except Exception:
                            pass
                    await idle()
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as e:
                # 针对 TimeoutError 或网络闪断增加自动重连
                wait_time = 10 + random.random() * 5
                self.log(f"监控连接异常中断: {e}，将在 {wait_time:.1f} 秒后尝试重新连接...", level="WARNING")
                await asyncio.sleep(wait_time)


class _UDPProtocol(asyncio.DatagramProtocol):
    """内部使用的UDP协议处理类"""

    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        pass  # 不需要处理接收的数据

    def error_received(self, exc):
        print(f"UDP error received: {exc}")
