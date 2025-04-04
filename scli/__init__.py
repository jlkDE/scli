#!/usr/bin/env python3

import argparse
import atexit
import base64
import bisect
import collections
import hashlib
import importlib
import json
import logging
import os
import pprint
import re
import resource
import shlex
import shutil
import signal as signal_ipc
import subprocess
import sys
import tempfile
import textwrap
import urllib
from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

import urwid
from urwid.widget.constants import BOX_SYMBOLS, BAR_SYMBOLS

try:
    from urwid_readline import ReadlineEdit
    Edit = ReadlineEdit
except ImportError:
    Edit = urwid.Edit


# #############################################################################
# constants
# #############################################################################


DATA_DIR = Path(os.getenv('XDG_DATA_HOME', '~/.local/share')).expanduser()
CFG_DIR = Path(os.getenv('XDG_CONFIG_HOME', '~/.config')).expanduser()

SIGNALCLI_DIR = DATA_DIR / 'signal-cli'
SIGNALCLI_DATA_DIR = SIGNALCLI_DIR / 'data'
SIGNALCLI_ATTACHMENT_DIR = SIGNALCLI_DIR / 'attachments'
SIGNALCLI_AVATARS_DIR = SIGNALCLI_DIR / 'avatars'
SIGNALCLI_STICKERS_DIR = SIGNALCLI_DIR / 'stickers'

SCLI_DATA_DIR = DATA_DIR / 'scli'
SCLI_ATTACHMENT_DIR = SCLI_DATA_DIR / 'attachments'
SCLI_HISTORY_FILE = SCLI_DATA_DIR / 'history'
SCLI_CFG_FILE = CFG_DIR / 'sclirc'
SCLI_LOG_FILE = SCLI_DATA_DIR / 'log'

SCLI_EXEC_DIR = Path(__file__).resolve().parent
SCLI_README_FILE = SCLI_EXEC_DIR / 'README.md'


# #############################################################################
# utility
# #############################################################################


def noop(*_args, **_kwargs):
    pass


def get_nested(dct, *keys, default=None):
    for key in keys:
        try:
            dct = dct[key]
        except (KeyError, TypeError, IndexError):
            return default
    return dct


def intersperse(value, container):
    it = iter(container)
    with suppress(StopIteration):
        yield next(it)
    for item in it:
        yield value
        yield item


def get_urls(txt):
    return re.findall(r'(https?://[^\s]+)', txt)


def proc_run(cmd, rmap=None, background=False, **subprocess_kwargs):
    if rmap:
        optionals = rmap.pop("_optionals", ())
        for key, val in rmap.items():
            if key not in cmd and key not in optionals:
                raise ValueError(f'Command string `{cmd}` should contain a replacement placeholder `{key}` (e.g. `some-cmd "{key}"`). See `--help`.')
            cmd = cmd.replace(key, val)

    if not subprocess_kwargs.get('shell'):
        cmd = shlex.split(cmd)
    logger.debug('proc_run: `%s`', cmd)

    if background:
        for arg in ('stdin', 'stdout', 'stderr'):
            subprocess_kwargs.setdefault(arg, subprocess.DEVNULL)
        return subprocess.Popen(cmd, **subprocess_kwargs)

    subprocess_kwargs.setdefault('text', True)
    proc = subprocess.run(cmd, **subprocess_kwargs)

    if proc.returncode != 0:
        logger.error(
                'proc_run: %s: exit code: %d, stderr: %s',
                proc.args,
                proc.returncode,
                proc.stderr
                )
    elif proc.stdout:
        logger.debug('proc_run: %s', proc.stdout)

    return proc


def get_version():
    """Get this program's version.

    Based on either `git describe`, or, if not available (e.g. for a release downloaded without the `.git` dir), use VERSION file populated during the creation of the release.
    Does not output the leading `v` if it's present in git tag's name.
    """

    git_dir = SCLI_EXEC_DIR / '.git'
    git_cmd = ['git', '--git-dir', git_dir, 'describe']
    with suppress(FileNotFoundError, subprocess.CalledProcessError):
        proc = subprocess.run(git_cmd, capture_output=True, check=True, text=True)
        return proc.stdout.strip('v\n')

    version_file_path = SCLI_EXEC_DIR / 'VERSION'
    try:
        with open(version_file_path, encoding='utf-8') as f:
            version_str = f.readline()
    except OSError:
        return git_hash_file(__file__)[:8]  # Short SHA
    if not version_str.startswith('v'):
        # '$Format:...' - not a `git archive` (e.g. a manually dl'ed blob)
        # '%(..)' - `git archive` if git < 2.32
        return git_hash_file(__file__)[:8]  # Short SHA
    return version_str[1:]  # `git-describe`-like string


def get_python_version():
    # Equivalent of platform.python_version()
    return '.'.join(str(d) for d in sys.version_info[:3])


def git_hash_file(path, block_size=2**16):
    """git-hash-object for a file.

    Method description in <https://git-scm.com/book/en/v2/Git-Internals-Git-Objects#_object_storage>.

    To find commits referencing an object's hash, use `git log --find-object=<hash>` (<https://stackoverflow.com/a/48590251>).
    """
    size = os.stat(path).st_size
    header = f"blob {size}\0".encode()
    hash_obj = hashlib.sha1()
    hash_obj.update(header)
    with open(path, 'rb') as fo:
        # In Python 3.11, can use hashlib.file_digest()
        # In Python 3.8, can use `while block := fo.read(…)`
        for block in iter(lambda: fo.read(block_size), b''):
            hash_obj.update(block)
    return hash_obj.hexdigest()


def prog_version_str():
    return f"scli {get_version()}"


def get_default_editor():
    for env_var in ('VISUAL', 'EDITOR'):
        ret = os.getenv(env_var)
        if ret is not None:
            return ret
    for exe in ('sensible-editor', 'editor', 'nano', 'emacs', 'vi'):
        ret = shutil.which(exe)
        if ret is not None:
            return ret
    return ret


def get_default_pager():
    with suppress(KeyError):
        return os.environ['PAGER']
    for exe in ('pager', 'less', 'more'):
        ret = shutil.which(exe)
        if ret is not None:
            return ret
    return None


PHONE_NUM_REGEX = re.compile('^\\+[1-9][0-9]{6,14}$')
# https://github.com/signalapp/libsignal-service-java/blob/master/java/src/main/java/org/whispersystems/signalservice/api/util/PhoneNumberFormatter.java
def is_number(number):
    return bool(PHONE_NUM_REGEX.match(number))


def is_path(path):
    return path.startswith(("/", "~/", "./"))


PATH_RE = re.compile(
    r"""
        # Matches a path-like string, with whitespaces escaped or with the whole path in quotes.
        (
            (
                \\\ |           # escaped whitespace OR ..
                [^'" ]          # .. not a quote or space
            )+
        )                       # Path with escaped whitespace ..
        |                       # .. OR ..
        (                       # .. path in quotes.
            (?P<quote>['"])     # a quote char; name the capture
            .+?                 # anything, non-greedily
            (?P=quote)          # matching quote
        )
        """,
    re.VERBOSE,
)

def partition_escaped(string):
    """Split `string` in two using ' ' as a separator, and accounting for escaped or quoted space characters.

    Similar to `str.partition(' ')`, except a *two*-tuple is returned (omitting the space separator character itself), and the first item may contain space if it is escaped with a backslash or is inside quotation marks.
    """
    string = string.strip()
    if not string:
        return ('', '')
    re_match = PATH_RE.match(string)
    if not re_match:
        return (string, '')
    match_str = re_match.group()
    if re_match.group(1):  # unquoted match_str
        match_str = match_str.replace(r'\ ', ' ')
    else:  # match_str in quotes
        match_str = match_str.strip('\'"')
    rest = string[re_match.end() :].lstrip()
    return (match_str, rest)


def get_current_timestamp_ms():
    return int(datetime.now().timestamp() * 1000)


def utc2local(utc_dt):
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)


def strftimestamp(timestamp, strformat='%H:%M:%S (%Y-%m-%d)'):
    try:
        date = datetime.utcfromtimestamp(timestamp)
    except ValueError:
        date = datetime.utcfromtimestamp(timestamp / 1000)
    return utc2local(date).strftime(strformat)


def strip_non_printable_chars(string):
    if string.isprintable():
        return string
    return ''.join((c for c in string if c.isprintable()))


# #############################################################################
# signal utility
# #############################################################################


def get_contact_id(contact_dict):
    return contact_dict.get('number') or contact_dict.get('groupId')


def is_contact_group(contact_dict):
    return 'groupId' in contact_dict


def is_group_v2(group_dict):
    gid = group_dict['groupId']
    return len(gid) == 44


def get_envelope_data_val(envelope, *keys, default=None, return_tuple=False):
    data_message_ret = get_nested(envelope, 'dataMessage', *keys, default=default)
    sync_message_ret = get_nested(envelope, 'syncMessage', 'sentMessage', *keys, default=default)
    if return_tuple:
        return (data_message_ret, sync_message_ret)
    else:
        return data_message_ret or sync_message_ret


def is_envelope_outgoing(envelope):
    return (
            'target' in envelope
            or get_nested(envelope, 'syncMessage', 'sentMessage') is not None
            or get_nested(envelope, 'callMessage', 'answerMessage') is not None
            )


def is_envelope_group_message(envelope):
    return (
            get_envelope_data_val(envelope, 'groupInfo') is not None
            or ('target' in envelope and not is_number(envelope['target']))
            or get_nested(envelope, 'typingMessage', 'groupId') is not None
    )


def get_envelope_msg(envelope):
    # If the `message` field is absent from the envelope: return None. If it is present but contains no text (since signal-cli v0.6.8, this is represented as `'message': null`): return ''. Otherwise: return the `message` field's value.
    for msg in get_envelope_data_val(envelope, 'message', default=0, return_tuple=True):
        if msg is None:
            return ''
        elif msg != 0:
            return msg
    return None


def get_envelope_time(envelope):
    return (
        envelope['timestamp']
        or get_envelope_data_val(envelope, 'timestamp')
    )


def get_envelope_contact_id(envelope):
    return (
        envelope.get('target')
        or get_envelope_data_val(envelope, 'groupInfo', 'groupId')
        or get_nested(envelope, 'syncMessage', 'sentMessage', 'destination')
        or get_nested(envelope, 'typingMessage', 'groupId')
        or envelope.get('sourceNumber')
        or envelope['source']
    )


def get_envelope_sender_id(envelope):
    return envelope['source']


def get_envelope_quote(envelope):
    return get_envelope_data_val(envelope, 'quote')


def get_envelope_reaction(envelope):
    return get_envelope_data_val(envelope, 'reaction')


def get_envelope_mentions(envelope):
    return get_envelope_data_val(envelope, 'mentions')


def get_envelope_remote_delete(envelope):
    return get_envelope_data_val(envelope, 'remoteDelete')


def get_envelope_sticker(envelope):
    return get_envelope_data_val(envelope, 'sticker')


def get_envelope_attachments(envelope):
    return get_envelope_data_val(envelope, 'attachments')


def get_attachment_name(attachment):
    if isinstance(attachment, dict):
        filename = attachment['filename']
        return filename if filename else attachment['contentType']
    else:
        return Path(attachment).name


def get_attachment_path(attachment):
    try:
        aid = attachment['id']
    except TypeError:
        return attachment
    received_attachment = SIGNALCLI_ATTACHMENT_DIR / aid
    return str(received_attachment)


def get_sticker_file_path(sticker):
    dir_name = sticker['packId']
    file_name = str(sticker['stickerId'])
    return str(SIGNALCLI_STICKERS_DIR / dir_name / file_name)


def b64_to_bytearray(group_id):
    return ','.join(str(i) for i in base64.b64decode(group_id.encode()))


def b64_to_hex_str(group_id):
    return base64.b64decode(group_id.encode()).hex()


def hex_str_to_b64(hex_str):
    return base64.b64encode(bytes.fromhex(hex_str)).decode()

# #############################################################################
# clipboard
# #############################################################################


class ClipGetBase(ABC):

    @abstractmethod
    def files_list(self):
        """Return a list of files in clipboard."""

    def _proc_run(self, *args, **kwargs):
        """proc_run() with capture_output by default"""
        kwargs.setdefault("capture_output", True)
        return proc_run(*args, **kwargs)


class ClipGetCmd(ClipGetBase):

    def __init__(self, get_cmd):
        self._get_cmd = get_cmd

    def files_list(self):
        return self._proc_run(self._get_cmd).stdout.splitlines()


class ClipGetBlank(ClipGetBase):
    def files_list(self):
        return []


class ClipGetTargets(ClipGetBase):

    _MIME_TARGETS = (
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "audio/aac",
            "audio/mpeg",
            "video/mp4",
            )

    _WRITE_FILE_PREFIX = "clipb_"

    def __init__(
            self,
            get_target_cmd,
            get_targets_list_cmd,
            write_dir,
            ):
        self._get_target_cmd = get_target_cmd
        self._get_targets_list_cmd = get_targets_list_cmd
        self._write_dir = write_dir

    def _list_available_targets(self):
        return self._proc_run(
                self._get_targets_list_cmd,
                ).stdout.splitlines()

    def _get_target_val(self, target_name, **subprocess_kwargs):
        return self._proc_run(
                f"{self._get_target_cmd} {target_name}",
                **subprocess_kwargs,
                ).stdout

    @staticmethod
    def _parse_file_uri(file_uri):
        return urllib.parse.unquote(
                file_uri[len("file://"):]
                )

    def _write_target_content(self, target):
        suffix = target.partition('/')[-1]
        datetime_str = datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%fZ')
        file_path = Path(
                self._write_dir,
                f"{self._WRITE_FILE_PREFIX}{datetime_str}.{suffix}",
                )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'wb') as outf:
            self._get_target_val(
                    target,
                    text=False,
                    capture_output=False,
                    stdout=outf,
                    )
        return str(file_path)

    def files_list(self):
        targets_avail = self._list_available_targets()
        if "text/uri-list" in targets_avail:
            uri_list = self._get_target_val("text/uri-list")
            return [self._parse_file_uri(line) for line in uri_list.splitlines()]
        for target in self._MIME_TARGETS:
            if target in targets_avail:
                return [self._write_target_content(target)]
        return []


class ClipPut:

    def __init__(self, put_cmd):
        self._put_cmd = put_cmd

    def put(self, text):
        proc_run(
                self._put_cmd,
                input=text,
                )


class ClipPutBlank:
    def put(self, text):
        pass


class Clip:

    def __init__(self):
        if cfg.clipboard_put_command:
            put_cmd = cfg.clipboard_put_command
        else:
            if shutil.which("wl-copy"):
                put_cmd = "wl-copy"
            elif shutil.which("xclip"):
                put_cmd = "xclip -i -sel c"
            else:
                put_cmd = None
                logger.warning("No clipboard copy command found; disabling copying to clipboard.")
        self._clip_put = ClipPut(put_cmd) if put_cmd else ClipPutBlank()

        if cfg.clipboard_get_command:
            self._clip_get = ClipGetCmd(cfg.clipboard_get_command)
        else:
            if cfg.save_history:
                write_dir = SCLI_ATTACHMENT_DIR
                Path(write_dir).mkdir(parents=True, exist_ok=True)
            else:
                write_dir = tempfile.mkdtemp(prefix="scli_")
                atexit.register(
                        shutil.rmtree,
                        write_dir,
                        )
            if shutil.which("wl-paste"):
                self._clip_get = ClipGetTargets(
                        get_target_cmd="wl-paste -t",
                        get_targets_list_cmd="wl-paste -l",
                        write_dir=write_dir,
                        )
            elif shutil.which("xclip"):
                self._clip_get = ClipGetTargets(
                        get_target_cmd="xclip -o -sel c -t",
                        get_targets_list_cmd="xclip -o -sel c -t TARGETS",
                        write_dir=write_dir,
                        )
            else:
                self._clip_get = ClipGetBlank()
                logger.warning("No clipboard paste command found; disabling querying clipboard for files.")

    def files_list(self):
        return self._clip_get.files_list()

    def put(self, text):
        return self._clip_put.put(text)


# #############################################################################
# AsyncProc & Daemon
# #############################################################################


class AsyncProc:
    def __init__(self, main_loop):
        # The `main_loop` is an object like `urwid.MainLoop`, that implements `watch_pipe()` and `set_alarm_in()` methods.
        self.main_loop = main_loop

    def _on_proc_started(self, proc): pass
    def _on_proc_done(self, proc): pass

    def run(self, args, shell, callback, callback_args, callback_kwargs):
        """ Run the command composed of `args` in the background (asynchronously); run the `callback` function when it finishes """

        def watchpipe_handler(line):
            # This function is run when the shell process returns (finishes execution).
            # The `line` printed to watch pipe is of the form "b'<PID> <RETURN_CODE>\n'"
            _proc_pid, return_code = [int(i) for i in line.decode().split()]
            proc.wait()  # reap the child process, to prevent zombies

            proc.returncode = return_code   # overwrite the 'wrapper' command return code (always 0) with the actual command return code
            proc.output = proc.stderr.read().rstrip('\n')   # stderr stream is not seekable, so can be read only once
            proc.stderr.close()

            if return_code != 0:
                logger.error(
                        'proc: cmd:`%s`; return_code:%d; output:"%s"',
                        proc.args,
                        return_code,
                        proc.output,
                        )

            if callback is not None:
                callback(proc, *callback_args, **callback_kwargs)
            self._on_proc_done(proc)

            os.close(watchpipe_fd)  # Close the write end of watch pipe.
            return False    # Close the read end of watch pipe and remove the watch from event_loop.

        watchpipe_fd = self.main_loop.watch_pipe(watchpipe_handler)

        # If the command is run with Popen(.., shell=True), shlex.quote is needed to escape special chars in args.
        sh_command = " ".join(
                [shlex.quote(arg) for arg in args] if not shell else ['{', args, ';', '}']
                )
        # Redirect all the process's output to stderr, and write the process PID and exit status to the watch pipe.
        sh_command += " 1>&2; echo $$ $?"

        proc = subprocess.Popen(
                sh_command,
                shell=True,
                stdout=watchpipe_fd,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                )
        atexit.register(proc.kill)   # prevent orphaned processes surviving after the main program is stopped
        self._on_proc_started(proc)
        return proc


class AsyncQueued(AsyncProc):

    _MAX_BACKGROUND_PROCS_DEFAULT = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._curr_running = set()
        self._run_queue = collections.deque()
        try:
            self._max_background_procs = self._background_procs_resource(self._MAX_BACKGROUND_PROCS_DEFAULT)
        except OSError:
            self._max_background_procs = self._MAX_BACKGROUND_PROCS_DEFAULT

    @staticmethod
    def _background_procs_resource(default):
        nprocs = min(
                default,
                resource.getrlimit(resource.RLIMIT_NPROC)[0],
                )
        try:
            n_curr_fds = len(os.listdir('/proc/self/fd')) * 3 # x3 to account for signal-cli's added FDs after it starts
        except OSError:
            n_curr_fds = 32
        fd_limits = [nprocs*3+n_curr_fds]  # each proc opens 3 FDs
        for res_name in ("RLIMIT_NOFILE", "RLIMIT_OFILE"):
            with suppress(AttributeError, OSError, ValueError):
                res = getattr(resource, res_name)
                fd_limits.append(resource.getrlimit(res)[0])
        return (min(fd_limits) - n_curr_fds) // 3

    @property
    def _max_procs_reached(self):
        return len(self._curr_running) == self._max_background_procs

    def _add_run_to_queue(self, run_args):
        self._run_queue.append(run_args)

    def run(self, args, callback=None, *callback_args, shell=False, **callback_kwargs):
        run_args = {k: v for k, v in locals().items() if k not in ('self', '__class__')}
        if not self._max_procs_reached:
            return super().run(**run_args)
        else:
            return self._add_run_to_queue(run_args)

    def _on_proc_started(self, proc):
        super()._on_proc_started(proc)
        self._curr_running.add(proc)

    def _pop_run(self, proc):
        self._curr_running.remove(proc)
        try:
            return self._run_queue.popleft()
        except IndexError:
            return None

    def _on_proc_done(self, proc):
        super()._on_proc_done(proc)
        run_args_next = self._pop_run(proc)
        if run_args_next is not None:
            started_proc = super().run(**run_args_next)
        else:
            started_proc = None
        return (run_args_next, started_proc)


class AsyncContext(AsyncQueued):

    class ContextItem:
        __slots__ = (
                'procs',
                'callback',
                'callback_kwargs',
                'proc_callback',
                'proc_callback_kwargs',
                'buffered_runs',
                )

        def __init__(self, callback, callback_kwargs, proc_callback, proc_callback_kwargs):
            self.procs = set()
            self.callback = callback
            self.callback_kwargs = callback_kwargs
            self.proc_callback = proc_callback
            self.proc_callback_kwargs = proc_callback_kwargs
            self.buffered_runs = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._context_items = collections.deque()
        self._accepting_new_procs_for_item = False

    def _new_item(self, callback, callback_kwargs, proc_callback, proc_callback_kwargs):
        if callback is None and proc_callback is None:
            return
        self._context_items.append(
                self.ContextItem(callback, callback_kwargs, proc_callback, proc_callback_kwargs)
                )
        self._accepting_new_procs_for_item = True

    @property
    def _curr_item(self):
        return self._context_items[-1]

    def _on_proc_started(self, proc):
        super()._on_proc_started(proc)
        if not self._accepting_new_procs_for_item:
            return
        self._curr_item.procs.add(proc)

    def _finalize_item(self):
        self._accepting_new_procs_for_item = False
        if not self._context_items:
            return
        curr_item = self._curr_item
        if not (curr_item.procs or curr_item.buffered_runs):
            # No background procs have been started
            self._pop_callback()

    def _pop_callback(self, item=None):
        if item is None:
            item = self._context_items.pop()
        else:
            self._context_items.remove(item)
        if item.callback is not None:
            item.callback(**item.callback_kwargs)

    @contextmanager
    def callback_finally(
            self,
            callback=None,
            proc_callback=None,
            proc_callback_kwargs=None,
            **callback_kwargs
            ):
        """Execute callback function after all background processes started inside this context have finished.

        Optionally, run `proc_callback` after every background processes that exits.
        """

        proc_callback_kwargs = proc_callback_kwargs or {}
        try:
            yield self._new_item(
                    callback,
                    callback_kwargs,
                    proc_callback,
                    proc_callback_kwargs,
                    )
        finally:
            self._finalize_item()

    @staticmethod
    def _run_id(run_params):
        return id(run_params)

    def _add_buffered_run(self, run_params):
        if not self._accepting_new_procs_for_item:
            return
        self._curr_item.buffered_runs.add(self._run_id(run_params))

    def _add_run_to_queue(self, run_args):
        super()._add_run_to_queue(run_args)
        self._add_buffered_run(run_args)

    def _remove_buffered_run(self, run_params, started_proc):
        if run_params is None:
            return
        run_id = self._run_id(run_params)
        for item in self._context_items:
            try:
                item.buffered_runs.remove(run_id)
            except KeyError:
                continue
            else:
                item.procs.add(started_proc)
                    # There should be no race condition if the proc has already finished: the python code is executed in a single thread, and this is always run before the proc's return is processed.
                return

    def _on_proc_done(self, proc):
        self._remove_buffered_run(*super()._on_proc_done(proc))
        for item in self._context_items:
            try:
                item.procs.remove(proc)
            except KeyError:
                continue
            if item.proc_callback is not None:
                item.proc_callback(proc, **item.proc_callback_kwargs)
            if not (item.procs or item.buffered_runs or self._accepting_new_procs_for_item):
                self._pop_callback(item)
            return


class Daemon(AsyncContext):
    def __init__(self, main_loop, username):
        super().__init__(main_loop)
        self._username = username
        self._buffer = ''
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self._logger_level_re = re.compile(r"^(2.*? \[.*\] )?(?P<lev_name>[A-Z]+) .*$")
        self._msg_processing_paused = True
            # Paused initially, to prevent a race condition betw registering the dbus service and getting a message on stdout (e.g. while polling in _run_when_dbus_service_started)
        self.callbacks = {
                cb_name: noop for cb_name in [
                    'daemon_started',
                    'daemon_log',
                    'receive_message',
                    'receive_sync_message',
                    'receive_receipt',
                    'receive_reaction',
                    'receive_sticker',
                    'sending_message',
                    'sending_reaction',
                    'sending_done',
                    'sending_reaction_done',
                    'contact_typing',
                    'call_message',
                    'contacts_sync',
                    'remote_delete',
                    'sending_remote_delete_done',
                    'untrusted_identity_err',
                    'user_unregistered_err',
                ]
            }

    def start(self):
        stdout_fd = self.main_loop.watch_pipe(self._daemon_stdout_handler)
        stderr_fd = self.main_loop.watch_pipe(self._daemon_stderr_handler)
        try:
            proc = proc_run(
                    cfg.daemon_command,
                    {'%u': self._username, '_optionals': ['%u']},
                    background=True,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    #text=True,  # urwid returns bytes-objects anyway; see comment in _daemon_stdout_handler
                    )
        except FileNotFoundError:
            sys.exit(
                    f"ERROR: could not find `{cfg.daemon_command.split()[0]}` executable. "
                    "Make sure it is on system path."
                    )
        return proc

    def _daemon_stdout_handler(self, output):
        output = self._buffer + output.decode()
            # The `output` (supplied by urwid) is a `bytes` object, even when the `subprocess` is launched with `text=True`.
        if self._msg_processing_paused:
            self._buffer = output
            return True
        lines = output.split('\n')  # Different from splitlines(): adds a final '' element after '\n'
        self._buffer = lines.pop()

        for line in lines:
            if not line or line.isspace():
                continue
            try:
                json_data = json.loads(line)
                envelope = json_data['envelope']
            except (json.JSONDecodeError, KeyError) as err:
                logger.error('Could not parse daemon output: %s', line)
                logger.exception(err)
                return True
            logger.debug("Daemon: json_data = \n%s", pprint.pformat(json_data))
            error_data = None
            for error_key in ('error', 'exception'):
                try:
                    error_data = json_data[error_key]
                except KeyError:
                    continue
                self._error_data_handler(error_data, envelope)
            if error_data is None:
                self._envelope_handler(envelope)
        return True

    def _daemon_stderr_handler(self, output):
        lines = output.decode().strip()
        if not lines:
            return True
        if self._msg_processing_paused:
            # Using it as a proxy for "signal-cli dbus service not yet running".
            if any(s in lines for s in (
                #"Exported dbus object: /org/asamk/Signal",  # signal-cli v0.9.2 or earlier
                "DBus daemon running",  # signal-cli v0.12.8 or earlier
                "Started DBus server",
                )):
                self._run_when_dbus_service_started(
                        self.callbacks['daemon_started']
                        )
            elif "in use by another instance" in lines:
                self.callbacks['daemon_log']('another_instance_running', lines)
            elif "Config file lock acquired" in lines:
                self.callbacks['daemon_log']('another_instance_stopped', lines)
        for line in lines.splitlines():
            log_lev = getattr(
                    logging,
                    self._daemon_output_log_level(line) or "INFO",
                    logging.INFO,
                    )
            self.logger.log(log_lev, line)
            if line.startswith("ERROR") and not self.is_dbus_service_running:
                self.callbacks['daemon_log']('daemon_stopped', line)
        return True

    def _envelope_handler(self, envelope):
        envelope['_received_timestamp'] = get_current_timestamp_ms()
        if get_envelope_msg(envelope) or get_envelope_attachments(envelope):
            if get_nested(envelope, 'syncMessage', 'sentMessage') is not None:
                self.callbacks['receive_sync_message'](envelope)
            else:
                self.callbacks['receive_message'](envelope)
        elif envelope.get('receiptMessage') is not None:
            # In signal-cli >=0.7.3, above check can be replaced with just
            #   'receiptMessage' in envelope
            # Keeping `is not None` for compatiability with envelopes in history from older signal-cli versions.
            self.callbacks['receive_receipt'](envelope)
        elif 'typingMessage' in envelope:
            self.callbacks['contact_typing'](envelope)
        elif get_envelope_reaction(envelope):
            self.callbacks['receive_reaction'](envelope)
        elif envelope.get('callMessage') is not None:
            self.callbacks['call_message'](envelope)
        elif get_nested(envelope, 'syncMessage', 'type') in ('CONTACTS_SYNC', 'GROUPS_SYNC'):
            self.callbacks['contacts_sync']()
        elif get_envelope_data_val(envelope, 'groupInfo', 'type') == 'UPDATE':
            self.callbacks['contacts_sync']()
        elif get_envelope_remote_delete(envelope):
            self.callbacks['remote_delete'](envelope)
        elif get_envelope_sticker(envelope):
            self.callbacks['receive_sticker'](envelope)
        else:
            logger.info('No action for received envelope: %s', pprint.pformat(envelope))

    def _error_data_handler(self, error_data, envelope):
        logger.error("Daemon: error = \n%s", pprint.pformat(error_data))
        if error_data.get('type') == 'UntrustedIdentityException':
            self.callbacks['untrusted_identity_err'](envelope)

    def _daemon_output_log_level(self, line):
        match = self._logger_level_re.match(line)
        return match.group("lev_name") if match else None

    def pause_message_processing(self):
        self._msg_processing_paused = True

    def unpause_message_processing(self):
        self._msg_processing_paused = False
        self._daemon_stdout_handler(b'')

    def _dbus_send(self, args, *proc_args, async_proc=True, **proc_kwargs):
        args = [
                'dbus-send',
                '--session',
                '--type=method_call',
                '--print-reply=literal',
                *args
                ]
        if async_proc:
            proc = self.run(args, *proc_args, **proc_kwargs)
        else:
            proc = subprocess.run(args, *proc_args, **proc_kwargs)
        return proc

    def _dbus_send_signal_cli(self, args, *proc_args, **proc_kwargs):
        """ Send a command to signal-cli daemon through dbus """
        args = [
                '--dest=org.asamk.Signal',
                '/org/asamk/Signal',
                *args
                ]
        return self._dbus_send(args, *proc_args, **proc_kwargs)

    def _send_message_dbus_cmd(self, message, attachments, recipient, is_group=False, *proc_args, **proc_kwargs):
        args = [
                ('org.asamk.Signal.sendMessage'
                    if not is_group else
                    'org.asamk.Signal.sendGroupMessage'),
                'string:' + message,
                'array:string:' + ','.join(attachments),
                ('string:' + recipient
                    if not is_group else
                    'array:byte:' + b64_to_bytearray(recipient))
                ]

        self._dbus_send_signal_cli(args, *proc_args, **proc_kwargs)

    def _send_reaction_dbus_cmd(self, emoji, remove, target_author, target_sent_timestamp, recipient, is_group=False, *proc_args, **proc_kwargs):
        dbus_args = [
                ('org.asamk.Signal.sendMessageReaction'
                    if not is_group else
                    'org.asamk.Signal.sendGroupMessageReaction'),
                "string:" + emoji,
                "boolean:" + str(remove).lower(),
                "string:" + target_author,
                "int64:" + str(target_sent_timestamp),
                ('string:' + recipient
                    if not is_group else
                    'array:byte:' + b64_to_bytearray(recipient))
                ]
        self._dbus_send_signal_cli(dbus_args, *proc_args, **proc_kwargs)

    def _send_remote_delete_dbus_cmd(self, target_sent_timestamp, recipient, is_group=False, *proc_args, **proc_kwargs):
        dbus_args = [
                ('org.asamk.Signal.sendRemoteDeleteMessage'
                    if not is_group else
                    'org.asamk.Signal.sendGroupRemoteDeleteMessage'),
                "int64:" + str(target_sent_timestamp),
                ('string:' + recipient
                    if not is_group else
                    'array:byte:' + b64_to_bytearray(recipient))
                ]
        self._dbus_send_signal_cli(dbus_args, *proc_args, **proc_kwargs)

    def _parse_send_proc_output(self, proc, envelope, callback_name):
        if proc.returncode != 0:
            if any(s in proc.output for s in (
                    "UntrustedIdentity",
                    "Untrusted Identity",
                    )):
                self.callbacks['untrusted_identity_err'](envelope)
            elif "Unregistered user" in proc.output:
                # Related signal-cli issues: #348, #828.
                if not is_envelope_group_message(envelope):
                    self.callbacks['user_unregistered_err'](envelope)
                else:
                    # Ad-hoc parsing of signal-cli's stderr output.
                    timestamp_adj = int(proc.output.splitlines()[0].rsplit(': ', 1)[-1])
                    logger.warning("some group members have uninstalled signal: %s", proc.output)
                    self.callbacks[callback_name](envelope, 'ignore_receipts', timestamp_adj)
                    return
            self.callbacks[callback_name](envelope, 'send_failed')
            return

        # Set envelope timestamp to that returned by signal-cli
        try:
            timestamp_adj = int(proc.output.rsplit(maxsplit=1)[1])
        except (IndexError, AttributeError) as err:
            logger.error("send_message: Failed to get adjusted envelope timestamp")
            logger.exception(err)
            self.callbacks[callback_name](envelope)
        else:
            self.callbacks[callback_name](envelope, 'sent', timestamp_adj)

    def send_message(self, contact_id, message="", attachments=None):
        is_group = not is_number(contact_id)

        if attachments is None:
            attachments = []
        if not all(os.path.exists(attch) for attch in attachments):
            logger.error('send_message: Attached file(s) do not exist: %s', attachments)
            return

        timestamp = get_current_timestamp_ms()
        envelope = {
            'source': self._username,
            'target': contact_id,
            'timestamp': timestamp,
            'dataMessage': {
                'message': message,
                'attachments': attachments,
                'timestamp': timestamp,
                },
        }

        def after_send_proc_returns(proc):
            # Check if send command succeeded
            self._parse_send_proc_output(proc, envelope, 'sending_done')

        self._send_message_dbus_cmd(
                message,
                attachments,
                contact_id,
                is_group,
                callback=after_send_proc_returns,
                )

        logger.info('send_message: %s', envelope)
        self.callbacks['sending_message'](envelope)

    def send_reaction(self, contact_id, emoji, orig_author, orig_ts, remove=False):
        is_group = not is_number(contact_id)
        timestamp = get_current_timestamp_ms()
        envelope = {
                'source': self._username,
                'target': contact_id,
                'timestamp': timestamp,
                'dataMessage': {
                    'message': None,
                    'timestamp': timestamp,
                    'reaction': {
                        'emoji': emoji,
                        'isRemove': remove,
                        'targetAuthor': orig_author,
                        'targetAuthorNumber': orig_author,
                        'targetSentTimestamp': orig_ts,
                        },
                    },
                }
        if is_group:
            envelope['dataMessage']['groupInfo'] = {
                    'groupId': contact_id,
                    }

        def after_send_proc_returns(proc):
            self._parse_send_proc_output(proc, envelope, 'sending_reaction_done')

        self._send_reaction_dbus_cmd(
                emoji,
                remove,
                orig_author,
                orig_ts,
                contact_id,
                is_group,
                callback=after_send_proc_returns
                )
        logger.info('send_reaction: %s', envelope)
        self.callbacks['sending_reaction'](envelope)

    def send_remote_delete(self, contact_id, orig_ts):
        is_group = not is_number(contact_id)
        timestamp = get_current_timestamp_ms()
        envelope = {
                'source': self._username,
                'target': contact_id,
                'timestamp': timestamp,
                'dataMessage': {
                    'message': None,
                    'timestamp': timestamp,
                    'remoteDelete': {
                        'timestamp': orig_ts,
                        },
                    },
                }
        if is_group:
            envelope['dataMessage']['groupInfo'] = {
                    'groupId': contact_id,
                    }
        self._send_remote_delete_dbus_cmd(
                orig_ts,
                contact_id,
                is_group,
                callback=lambda proc: self._parse_send_proc_output(proc, envelope, 'sending_remote_delete_done')
                )
        self.callbacks['remote_delete'](envelope)

    def rename_contact(self, contact_id, new_name, is_group=False, *proc_args, **proc_kwargs):
        """Rename a contact or group.

        If a contact does not exist, it will be created. Changes to groups are sent to the server, changes to individual contacts are local.
        """

        if not is_group:
            args = [
                    "org.asamk.Signal.setContactName",
                    "string:" + contact_id,
                    "string:" + new_name,
                    ]
        else:
            args = [
                    "org.asamk.Signal.updateGroup",
                    "array:byte:" + b64_to_bytearray(contact_id),
                    "string:" + new_name,
                    "array:string:" + '',   # members
                    "string:" + ''         # avatar
                    ]
        self._dbus_send_signal_cli(args, *proc_args, **proc_kwargs)

    def _get_group_name(self, group_id, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            name = proc.output.strip() or group_id[:10] + '[..]'
            callback(name, *cb_args, **cb_kwargs)
        args = [
                "org.asamk.Signal.getGroupName",
                "array:byte:" + b64_to_bytearray(group_id)
                ]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    def _get_group_members(self, group_id, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            # Ad hoc parsing of `dbus-send` output
            members_ids = set(proc.output[10:-1].split())
            callback(members_ids, *cb_args, **cb_kwargs)
        args = [
                "org.asamk.Signal.getGroupMembers",
                "array:byte:" + b64_to_bytearray(group_id)
                ]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    def get_groups_ids(self, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            groups_ids = set()
            bytearray_line_continued = False
            for line in proc.output.splitlines():
                # Ad hoc parsing of `dbus-send` output
                if line.endswith('array of bytes ['):
                    gid_bytes_array = ''
                    bytearray_line_continued = True
                elif bytearray_line_continued:
                    if line.startswith("         ]"):
                        bytearray_line_continued = False
                        groups_ids.add(hex_str_to_b64(gid_bytes_array))
                    else:
                        gid_bytes_array += line.strip()
            callback(groups_ids, *cb_args, **cb_kwargs)
        args = ["org.asamk.Signal.listGroups"]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    def populate_groups_dict(self, groups_ids, callback, **cb_kwargs):
        groups_dict = {}
        with self.callback_finally(callback, groups_dict=groups_dict, **cb_kwargs):
            for group_id in groups_ids:
                group = groups_dict[group_id] = {}
                group['groupId'] = group_id
                self._get_group_name(
                        group_id,
                        callback=lambda name, group=group: group.update({'name': name})
                        )
                self._get_group_members(
                        group_id,
                        callback=lambda members_ids, group=group: group.update({'members_ids': members_ids})
                        )

    def _get_contacts_numbers(self, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            try:
                # Ad-hoc parsing of `dbus-send` output
                numbers = proc.output.splitlines()[1][:-1].split()
            except IndexError:
                numbers = []
            callback(numbers, *cb_args, **cb_kwargs)
        args = ["org.asamk.Signal.listNumbers"]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    def _get_contact_name(self, phone_num, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            name = proc.output.strip()
            callback(phone_num, name, *cb_args, **cb_kwargs)
        args = [
                "org.asamk.Signal.getContactName",
                "string:" + phone_num
                ]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    def get_indiv_contacts(self, callback, *cb_args, **cb_kwargs):
        def on_got_numbers(numbers):
            with self.callback_finally(
                    callback=callback,
                    contacts_dict=contacts_dict,
                    *cb_args,
                    **cb_kwargs,
                    ):
                for num in numbers:
                    self._get_contact_name(num, callback=on_got_name)
        def on_got_name(phone_num, name):
            contacts_dict[phone_num] = {}
            contacts_dict[phone_num]["name"] = name
            contacts_dict[phone_num]["number"] = phone_num
        contacts_dict = {}
        self._get_contacts_numbers(callback=on_got_numbers)

    def get_signal_cli_version(self, callback, *cb_args, **cb_kwargs):
        def proc_callback(proc):
            version_num = proc.output.strip()
            version_string = "signal-cli " + version_num
            callback(version_string, *cb_args, **cb_kwargs)
        args = ["org.asamk.Signal.version"]
        self._dbus_send_signal_cli(args, callback=proc_callback)

    @property
    def is_dbus_service_running(self):
        args = [
                '--dest=org.freedesktop.DBus',
                '/org/freedesktop/DBus',
                'org.freedesktop.DBus.ListNames'
                ]
        proc = self._dbus_send(args, async_proc=False, capture_output=True, text=True, check=True)
        signal_cli_str = "org.asamk.Signal"
        return signal_cli_str in proc.stdout

    def _run_when_dbus_service_started(self, callback):
        poll_freq = 1       # seconds between polls
        def set_alarm(main_loop, _user_data=None):
            if self.is_dbus_service_running:
                callback()
            else:
                main_loop.set_alarm_in(poll_freq, set_alarm)
        set_alarm(self.main_loop)


# #############################################################################
# signal-cli data
# #############################################################################


class SignalData:
    def __init__(self, username):
        self._username = username
        self._file_path = self._get_account_file_path()
        self._data = self._read_data_file()

    @staticmethod
    def parse_accounts_json():
        accounts_json_path = SIGNALCLI_DATA_DIR / "accounts.json"
        try:
            with open(accounts_json_path, encoding="utf-8") as f:
                json_data = json.load(f)
        except FileNotFoundError:
            return []
        return json_data["accounts"]

    def _get_account_file_path(self):
        accounts = self.parse_accounts_json()
        for acc in accounts:
            if acc["number"] == self._username:
                return SIGNALCLI_DATA_DIR / acc["path"]
        raise FileNotFoundError(self._username + " does not exist!")

    def _read_data_file(self):
        with open(self._file_path, encoding="utf-8") as f:
            return json.load(f)

    @property
    def own_num(self):
        return self._username

    @property
    def is_linked_device(self):
        # The primary device should have a `deviceId == 1`
        return self._data['deviceId'] != 1


class Contact:

    # A `Contact` can be either an individual contact or a group.
    # This class uses the _record dict with contact's details, which is what is obtained from contactsStore and groupsStore in signal-cli data file's json structure.

    def __init__(self, record):
        self._record = record
        self.avatar = self._get_avatar_file_path()
        if self.is_group:
            self.members_ids = record.get('members_ids') or set()
            self.member_contacts = set()

    def __getattr__(self, attr):
        # A helper function to access values in contact's dict `record`.
        return self._record.get(attr)

    def update_record(self, update_dict):
        self._record.update(update_dict)

    def _get_avatar_file_path(self):
        # Might be implemented in the future by signal-cli: https://github.com/AsamK/signal-cli/issues/869
        def get_path(file_prefix, contact_id):
            path = SIGNALCLI_AVATARS_DIR / f'{file_prefix}-{contact_id}'
            return str(path) if path.exists() else None
        if self.is_group:
            return get_path('group', self.id.replace("/", "_"))
        for file_prefix in ('profile', 'contact'):
            path = get_path(file_prefix, self.id)
            if path is not None:
                return path
        return None

    @property
    def is_group(self):
        return is_contact_group(self._record)

    @property
    def is_group_v2(self):
        return is_group_v2(self._record)

    @property
    def id(self):
        return get_contact_id(self._record)

    @property
    def name_or_id(self):
        return self.name or self.id

    def serialize(self):
        return self._record


class Contacts:
    def __init__(self, sigdata, contacts_cache=None):
        self.sigdata = sigdata
        self.indivs = set()
        self.groups = set()
        self.map = {}
        self.update(contacts_cache, clear=True)

    def _clear(self):
        self.indivs = set()
        self.groups = set()
        self.map = {}

    def update(self, contacts_dict=None, clear=False):
        if clear:
            self._clear()
        if not contacts_dict:
            return
        for cid, contact_dict in contacts_dict.items():
            contact = Contact(contact_dict)
            self.map[cid] = contact
            if is_contact_group(contact_dict):
                self.groups.add(contact)
            else:
                self.indivs.add(contact)

    def set_groups_membership(self):
        for group in self.groups:
            group.members_ids.discard(self.sigdata.own_num)
            group.member_contacts = self._get_group_members(group)
                # Naming: was (historically), group.members == group._record['members'] (from signal-cli data)

    def _get_group_members(self, group):
        members = set()
        for mid in group.members_ids:
            mem = self.map.get(mid)
            if mem is None:
                # Some members of a group might not be in my `contacts`, so they have no Contact obj associated with them.
                mem = Contact({"number": mid})
            members.add(mem)
        return members

    def get_by_id(self, contact_id):
        return self.map.get(contact_id)

    def serialize(self):
        return {c.id: c.serialize() for c in self.map.values()}

# #############################################################################
# chats data
# #############################################################################


class Message:

    _get_delivery_status = noop
    _get_contact = noop

    @classmethod
    def set_class_functions(cls, get_delivery_status, get_contact):
        cls._get_delivery_status = get_delivery_status
        cls._get_contact = get_contact

    __slots__ = ("envelope", "reactions", "remote_delete")

    def __init__(self, envelope):
        self.envelope = envelope

    def __eq__(self, other_msg):
        return self.envelope == other_msg.envelope

    def __lt__(self, other_msg):
        return self.local_timestamp < other_msg.local_timestamp

    def __le__(self, other_msg):
        return self.local_timestamp <= other_msg.local_timestamp

    @property
    def timestamp(self):
        return get_envelope_time(self.envelope)

    @timestamp.setter
    def timestamp(self, ts_new):
        # NOTE: For Message in Chat, use Chat.adjust_timestamp(), rather then this setter directly, to ensure that Chat remains sorted.
        self.envelope['timestamp'] = self.envelope['dataMessage']['timestamp'] = ts_new

    @property
    def local_timestamp(self):
        return self.envelope.get('_received_timestamp') or self.timestamp

    @property
    def text(self):
        if self.mentions:
            return self.text_w_mentions()
        else:
            return get_envelope_msg(self.envelope)

    @property
    def attachments(self):
        return get_envelope_attachments(self.envelope)

    @property
    def mentions(self):
        return get_envelope_mentions(self.envelope)

    @property
    def delivery_status(self):
        if is_envelope_outgoing(self.envelope):
            return self._get_delivery_status(self.timestamp).str
        else:
            return 'received_by_me'

    @property
    def delivery_status_detailed(self):
        return self._get_delivery_status(self.timestamp)

    @property
    def contact_id(self):
        return get_envelope_contact_id(self.envelope)

    @property
    def sender_num(self):
        return get_envelope_sender_id(self.envelope)

    @property
    def sender(self):
        return self._get_contact(self.sender_num)

    def add_reaction(self, envelope):
        self.reactions = getattr(self, 'reactions', {})  # pylint: disable=attribute-defined-outside-init
            # Don't want to add `reactions` attribute to every Message instance; only to those that actually have reactions.
        self.reactions[get_envelope_sender_id(envelope)] = envelope

    @classmethod
    def text_w_mentions_generic(cls, text, mentions, bracket_char=''):
        # See also: What is the Mention's "length" parameter?
        # https://github.com/AsamK/signal-cli/discussions/409
        ret = ''
        pos = 0
        for mention in mentions:
            contact_num = mention['name']
            contact = cls._get_contact(contact_num)
            contact_name = contact.name_or_id if contact else contact_num
            start = mention['start']
            ret = ''.join((
                ret,
                text[pos:start],
                bracket_char,
                "@", contact_name,
                bracket_char,
                ))
            pos = start + 1
        ret += text[pos:]
        return ret

    def text_w_mentions(self, bracket_char=''):
        text = get_envelope_msg(self.envelope)
        return self.text_w_mentions_generic(text, self.mentions, bracket_char)

    @property
    def not_repliable(self):
        envelope = self.envelope
        return (
                'typingMessage' in envelope
                or envelope.get('callMessage') is not None
                or getattr(self, 'remote_delete', None)
                )


class ReorderedTimestamps(list):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reordered_timestamps = {}

    def _is_neighbd_monotonic(self, index):
        try:
            return self[index-1] <= self[index] <= self[index+1]
            # Works for index=(0, len(self)), i.e. not 0 or -1. Not worth it to add checks for those cases.
        except IndexError:
            return True
            # `self[index]` is either the last or the only message in Chat. In the former case, if it is out of order, the first comparison will return False, and IndexError will not be raised.

    def _add_reordered_neighbors(self, index):
        # When calling this method, ensure that len(self) > 1 and index == -1 for the last element (not len(self)-1).
        if index == -1:
            pre_post_pairs = ((self[-2], self[-1]), )
        elif index == 0:
            pre_post_pairs = ((self[0], self[1]), )
        else:
            pre_post_pairs = (
                    (self[index-1], self[index]),
                    (self[index], self[index+1]),
                    )
        for pre_post in pre_post_pairs:
            for tlr in (True, False):
                local, received = pre_post if tlr else reversed(pre_post)
                received_ts = received.envelope.get('_received_timestamp')
                if received_ts is None:
                    continue
                original_ts = received.timestamp
                left_right = (original_ts, received_ts)
                left, right = left_right if tlr else reversed(left_right)
                if left <= local.local_timestamp <= right:
                    self._reordered_timestamps[
                            (received.sender_num, original_ts)
                            ] = received_ts
                            # Including sender_num in the dict's key to prevent collisions in group chats between same-timestamp, different-sender messages.

    def _delete_from_reordered(self, msg, index):
        # This method is assumed to be run *after* the msg itself is already deleted.
        self._reordered_timestamps.pop((msg.sender_num, msg.timestamp), None)
        edge = True
        if index == len(self):
            # Assuming the supplied index != -1. However _add_reordered_neighbors() requires -1.
            index = -1
        elif index:
            index -= 1
            edge = False
        modified = False
        for ioffset in range(1) if edge else range(-1, 1):
            with suppress(IndexError, KeyError):
                neighb = self[index+ioffset]
                del self._reordered_timestamps[(neighb.sender_num, neighb.timestamp)]
                modified = True
        if modified:
            self._add_reordered_neighbors(index)


class Chat(ReorderedTimestamps, urwid.MonitoredList):
    # An `urwid.MonitoredList` is a subclass of a regular `list`, that modifies the "mutating" (modifying `self`) methods, so that they call the `self._modified()` method at the end.
        # The `self._modified()` method is set to simply do `pass`, until a callback is assigned to it in ListWalker's __init__.

    def index(self, msg):
        """More efficient way to locate an object in the sorted list than just using super().index() method.

        Since Chat should always be sorted, a member object can be located faster using bisect."""

        try:
            msg_last = self[-1]
        except IndexError as exc:
            # Return "message-not-found" when chat history is blank.
            raise ValueError from exc
        if msg_last == msg:
            # First check the last msg before doing bisect_left, as bisect starts in the middle. See also comment in self.add()
            return len(self) - 1
        index = bisect.bisect_left(self, msg)
        if index != len(self) and self[index] == msg:
            return index
        raise ValueError

    def index_ts(self, timestamp, sender_num=None):
        """Return an index of a message in Chat with a given timestamp, from a given phone number"""

        def match_test(msg):
            return (
                    msg.timestamp == timestamp
                    and
                    (msg.sender_num == sender_num or sender_num is None)
                    )

        try:
            msg = self[-1]
        except IndexError as exc:
            # Return "message-not-found" when chat history is blank.
            raise ValueError from exc
        if match_test(msg):
            # First check the last msg before doing bisect_left, as bisect starts in the middle. See also comment in self.add()
            return len(self) - 1
        dummy_message = Message({'timestamp':
            self._reordered_timestamps.get((sender_num, timestamp))
            or timestamp
            })
        index = bisect.bisect_left(self, dummy_message)
        if index != len(self):
            for ind in (i for r in ((index, index-1), range(index+1, len(self))) for i in r):
                # This generator expression is a "re-implementation" of itertools.chain().
                    # The indecies are ordered with the more likely matches tested first.
                    # The range(..) checks for msgs with the same timestamp (e.g. msgs in a group chat from different senders).
                    # index-1 might be a match if it is not in _reordered_timestamps and its _received_timestamp < timestamp.
                msg = self[ind]
                if match_test(msg):
                    return ind
                if msg.timestamp > timestamp and ind > index:
                    break
        raise ValueError

    def get_index_for_envelope(self, envelope):
        dummy_message = Message(envelope)
        index = self.index(dummy_message)
        return index

    def get_msg_for_envelope(self, envelope):
        index = self.get_index_for_envelope(envelope)
        return self[index]

    def get_msg_for_timestamp(self, timestamp, sender_num=None):
        ind = self.index_ts(timestamp, sender_num)
        return self[ind]

    def add(self, msg):
        index = -1
            # Not using len(self)-1 because after self.append() it will shift.
        try:
            msg_last = self[index]
        except IndexError:
            # The chat is empty
            self.append(msg)
            return
        if msg_last <= msg:
            # Check first if the message should be appended at the end of Chat container.
                # This is the case for most of the messages. The exceptions might occur if the system's clock has been moved back.
                # `bisect` starts searching for the place for new item from the middle of the container, which takes more steps.
            self.append(msg)
        else:
            index = bisect.bisect(self, msg)
            self.insert(index, msg)
        self._add_reordered_neighbors(index)

    def delete(self, msg, index=None):
        # The `index` is optional, but if known, will save cpu cycles for finding the message in chat.
        try:
            if index is None:
                index = self.index(msg)
            del self[index]
        except (ValueError, IndexError) as err:
            logger.warning("Chat.delete(): message not found; envelope = %s", msg.envelope)
            logger.exception(err)
            raise ValueError from err
        self._delete_from_reordered(msg, index)

    def adjust_timestamp(self, msg, timestamp_adj, index=None):
        """Adjust message's timestamp, ensuring that Chat remains sorted"""
        msg.timestamp = timestamp_adj

        # Ensure that Chat remains sorted
            # This should rarely be necessary, as signal-cli's timestamp adjustments are small enough (~50ms) to not modify the messages' order.
        index = index or self.index(msg)
        if not self._is_neighbd_monotonic(index):
            logger.debug("Chat: moving msg to maintain sorted history: %s", timestamp_adj)
            self.delete(msg, index)
            self.add(msg)


class Chats:
    def __init__(self):
        self._dict = collections.defaultdict(Chat)

    def __getitem__(self, contact_id):
        return self._dict[contact_id]

    def get_chat_for_envelope(self, envelope):
        return self._dict[get_envelope_contact_id(envelope)]

    def get_chat_index_for_envelope(self, envelope):
        try:
            chat = self.get_chat_for_envelope(envelope)
            index = chat.get_index_for_envelope(envelope)
            return chat, index
        except (KeyError, ValueError, IndexError) as err:
            logger.error("get_msg_for_envelope(): envelope = %s", envelope)
            logger.exception(err)
            raise ValueError from err

    def get_msg_for_envelope(self, envelope):
        chat, index = self.get_chat_index_for_envelope(envelope)
        return chat[index]

    def get_msg_for_timestamp(self, envelope, timestamp, sender_num=None):
        chat = self.get_chat_for_envelope(envelope)
        return chat.get_msg_for_timestamp(timestamp, sender_num)

    def add_envelope(self, envelope):
        msg = Message(envelope)
        chat = self.get_chat_for_envelope(envelope)
        chat.add(msg)
        return msg

    def add_reaction_envelope(self, envelope):
        reaction = get_envelope_reaction(envelope)
        try:
            msg = self.get_msg_for_timestamp(
                    envelope,
                    timestamp=reaction['targetSentTimestamp'],
                    sender_num=reaction['targetAuthor']
                    )
        except ValueError:
            logger.error("Message not found for reaction: %s", pprint.pformat(envelope))
            return None
        msg.add_reaction(envelope)
        return msg

    def add_remote_delete_envelope(self, envelope):
        try:
            msg = self.get_msg_for_timestamp(
                    envelope,
                    timestamp=get_envelope_remote_delete(envelope)['timestamp'],
                    sender_num=get_envelope_sender_id(envelope)
                    )
        except ValueError:
            logger.error("Message not found for remote delete envelope: %s", envelope)
            return None
        msg.remote_delete = envelope
        return msg

    def delete_message(self, msg, index=None):
        chat = self.get_chat_for_envelope(msg.envelope)
        chat.delete(msg, index)

    def serialize(self):
        envelopes = []
        for chat in self._dict.values():
            for msg in chat:
                envelope = msg.envelope
                if (
                        "typingMessage" in envelope
                        or
                        envelope.get("_artificialEnvelope") == "untrustedIdentity"
                        ):
                    continue
                envelopes.append(envelope)
                with suppress(AttributeError):
                    envelopes.extend(msg.reactions.values())
                with suppress(AttributeError):
                    # Currently, the "deleted" messages are saved in the history file.
                    envelopes.append(msg.remote_delete)
        return envelopes


class UnreadCounts(collections.defaultdict):
    def __init__(self, *args, **kwargs):
        super().__init__(int, *args, **kwargs)

    @property
    def total(self):
        return sum(self.values())

    def serialize(self):
        return {contact_id: count for contact_id, count in self.items() if count != 0}


class DeliveryStatus:

    DelivReadConts = collections.namedtuple('DelivReadConts', ['delivered', 'read'])

    class DetailedStatus:

        __slots__ = ("str", "when", "grp_memb_remain_un")

        def __init__(self, status='', when=0, grp_memb_remain_un=None):
            self.str = status
            self.when = when
            if grp_memb_remain_un:
                self.grp_memb_remain_un = DeliveryStatus.DelivReadConts(
                    *(
                        set(contacts) if contacts else set()
                        for contacts in grp_memb_remain_un
                    )
                )

        def set_grp_memb_status(self, grp_member, status):
            try:
                grp_memb_remain_un = self.grp_memb_remain_un
            except AttributeError:
                return None
            grp_memb_remaining = getattr(grp_memb_remain_un, status)
            try:
                grp_memb_remaining.remove(grp_member)
            except (KeyError, AttributeError):
                # This happens when 'read' receipt arrives before 'delivered', or after getting multiple copies of the same receipt message.
                grp_memb_remaining = grp_memb_remain_un.delivered
                try:
                    grp_memb_remaining.remove(grp_member)
                except (KeyError, AttributeError):
                    return None
                if not grp_memb_remain_un.delivered and grp_memb_remain_un.read:
                    return 'delivered'

            if status == 'delivered':
                remaining_unread = grp_memb_remain_un.read
                remaining_unread.add(grp_member)
                if grp_memb_remaining:
                    return None
                return status

            if any(grp_memb_remain_un):
                return None
            del self.grp_memb_remain_un
            return status

        def serialize(self):
            ret = []
            for attr in self.__slots__:
                val = getattr(self, attr, None)
                ret.append(val)

            # Skip empty values at the end
            for ind, val in enumerate(reversed(ret)):
                if val:
                    if ind != 0:
                        ret = ret[:-ind]
                    break
            else:
                ret = []

            return ret

    def _make_markup_map():     # pylint: disable=no-method-argument
        status_text = {
                # Order matters: 'higher' status can't be 're-set' to a 'lower' one.
                '':                 '<<',
                'received_by_me':   '>>',
                'sending':          '',
                'send_failed':      '✖',
                'sent':             '✓',
                'delivered':        '✓✓',
                'read':             '✓✓',
                'ignore_receipts':  '✓',
                }
        max_len = max((len(text) for text in status_text.values()))
        markup_map = {}
        for status, text in status_text.items():
            markup_map[status] = (
                    ('bold', text)
                    if status not in ('read', 'ignore_receipts')
                    else ('strikethrough', text)
                    )
        return (markup_map, max_len)

    MARKUP_MAP, MARKUP_WIDTH = _make_markup_map()
    MAX_GROUP_SIZE = 15

    def __init__(self):
        self._status_map = {}
        self._buffered = {}

        self._status_order = {key: ind for ind, key in enumerate(self.MARKUP_MAP)}

        self.on_status_changed = noop

    def get_detailed(self, timestamp):
        return self._status_map.get(timestamp, self.DetailedStatus())

    def get_str(self, timestamp):
        return self.get_detailed(timestamp).str

    def on_receive_receipt(self, envelope):
        receipt_contact = get_envelope_sender_id(envelope)
        receipt_message = envelope['receiptMessage']
        if receipt_message['isDelivery']:
            status = 'delivered'
        elif receipt_message['isRead']:
            status = 'read'
        elif receipt_message['isViewed']:
            return
        else:
            logger.error('on_receive_receipt: unknown receipt type in envelope %s', envelope)
            return
        timestamps = receipt_message['timestamps']
        when = receipt_message['when']
        for timestamp in timestamps:
            if timestamp not in self._status_map:
                # Receipt is received before 'sent' status set (e.g. because receipt received before a `sync` message for a message sent from another device)
                self._buffer_receipt(timestamp, status, receipt_contact)
            else:
                self._set(timestamp, status, when, receipt_contact)

    def on_sending_message(self, envelope, group_members=None):
        timestamp = get_envelope_time(envelope)
        self._set(timestamp, 'sending')
        if group_members is not None:
            self._set_group_members(timestamp, group_members)

    def on_sending_done(self, envelope, status='sent', timestamp_adj=None):
        timestamp = get_envelope_time(envelope)
        if timestamp not in self._status_map:
            logger.error("DeliveryStatus: on_sending_done(): no corresponding timestamp in _status_map for envelope = %s", envelope)
            return
        self._set(timestamp, status)
        if status == 'send_failed':
            return
        if timestamp_adj is not None:
            self._adjust_timestamp(timestamp, timestamp_adj)

    def _adjust_timestamp(self, timestamp_orig, timestamp_adj):
        self._status_map[timestamp_adj] = self._status_map.pop(timestamp_orig)

    def _set(self, timestamp, status, when=None, receipt_contact=None):
        curr_status_detailed = self._status_map.setdefault(
                timestamp, self.DetailedStatus()
                )
        curr_status = curr_status_detailed.str

        if self._status_order[status] <= self._status_order[curr_status]:
            return

        is_group = getattr(curr_status_detailed, 'grp_memb_remain_un', False)
        if is_group and receipt_contact is not None:
            status = curr_status_detailed.set_grp_memb_status(receipt_contact, status)
            if status is None:
                return

        logger.info("Setting status = `%s` for timestamp = %s", status, timestamp)
        curr_status_detailed.str = status
        if when is not None:
            curr_status_detailed.when = when
        self.on_status_changed(timestamp, status)

    def _set_group_members(self, timestamp, group_members):
        status_detailed = self._status_map[timestamp]

        if len(group_members) > self.MAX_GROUP_SIZE:
            self._set(timestamp, 'ignore_receipts')
            return

        status_detailed.grp_memb_remain_un = self.DelivReadConts(set(group_members), set())

    def _buffer_receipt(self, timestamp, status, contact):
        logger.debug("DeliveryStatus: buffering timestamp = %s", timestamp)
        buffered = self._buffered.setdefault(
                timestamp,
                self.DelivReadConts(
                    set(), set()
                    )
                )
        buffered_contacts = getattr(buffered, status)
        buffered_contacts.add(contact)

    def process_buffered_receipts(self, timestamp):
        buffered = self._buffered.get(timestamp)
        if buffered is None:
            return
        logger.debug("Processing buffered receipts: timestamp = %s, self._buffered = %s", timestamp, self._buffered)
        for status in buffered._fields:
            buffered_contacts = getattr(buffered, status) or []
            for contact in buffered_contacts:
                self._set(timestamp, status, receipt_contact=contact)
        del self._buffered[timestamp]

    def delete(self, timestamp):
        with suppress(KeyError):
            del self._status_map[timestamp]

    def dump(self):
        ret = {}
        for timestamp, status_detailed in self._status_map.items():
            status_serialized = status_detailed.serialize()
            if status_serialized:
                ret[timestamp] = status_serialized
        return ret

    def load(self, status_map):
        for timestamp, status_detailed in status_map.items():
            self._status_map[int(timestamp)] = self.DetailedStatus(*status_detailed)


class TypingIndicators:
    def __init__(self, chats):
        self._chats = chats
        self._map = {}
        self.set_alarm_in = self.remove_alarm = noop

    def on_typing_message(self, envelope):
        sender_num = get_envelope_sender_id(envelope)
        typing_event = get_nested(envelope, 'typingMessage', 'action')
        self.remove(sender_num)
        if typing_event == 'STARTED':
            self._add(sender_num, envelope)
        elif typing_event != 'STOPPED':
            logger.warning("on_typing_message: unknown `action` type in %s", envelope)

    def _add(self, sender_num, envelope):
        msg = self._chats.add_envelope(envelope)
        alarm = self.set_alarm_in(10, lambda *_: self.remove(sender_num))
        self._map[sender_num] = (msg, alarm)

    def remove(self, sender_num):
        try:
            msg, alarm = self._map.pop(sender_num)
        except KeyError:
            return
        self.remove_alarm(alarm)
        try:
            self._chats.delete_message(msg)
        except ValueError:
            logger.warning("TypingIndicators: remove: index not found for envelope = %s", msg.envelope)


class ChatsData:
    def __init__(self, history_file):
        self.chats = Chats()
        self.unread_counts = UnreadCounts()
        self.delivery_status = DeliveryStatus()
        self.typing_indicators = TypingIndicators(self.chats)
        self._history = history_file
        self.current_contact = None
        self.contacts_cache = None

        if self._history:
            self._load_history()
            atexit.register(self._save_history)

    @property
    def current_chat(self):
        if self.current_contact:
            return self.chats[self.current_contact.id]
        return None

    def _save_history(self):
        envelopes = self.chats.serialize()
        unread_counts = self.unread_counts.serialize()
        delivery_status = self.delivery_status.dump()
        items = {
                'version': 5,
                'envelopes': envelopes,
                'unread_counts': unread_counts,
                'delivery_status': delivery_status,
                'contacts_cache': self.contacts_cache,
                }

        class JSONSetEncoder(json.JSONEncoder):
            # Using a custom json encoder to encode `set`s from `DeliveryStatus` group_members.
            def default(self, o):
                try:
                    return json.JSONEncoder.default(self, o)
                except TypeError:
                    if isinstance(o, set):
                        return tuple(o)
                    raise

        Path(self._history).parent.mkdir(parents=True, exist_ok=True)
        with open(self._history, 'w', encoding="utf-8") as history_fileobj:
            json.dump(items, history_fileobj, ensure_ascii=False, cls=JSONSetEncoder, indent=2)

    def _load_history(self):
        history_backup_filename = self._history + '.bak'
        for history_filename in (self._history, history_backup_filename):
            try:
                with open(history_filename, 'r', encoding="utf-8") as history_fileobj:
                    history = json.load(history_fileobj)
            except (FileNotFoundError, json.JSONDecodeError) as err:
                if isinstance(err, json.JSONDecodeError):
                    logger.error("History file corrupted, attempting to read from backup.")
                continue
            else:
                break
        else:
            # This happens on e.g. the first run, before the history file exists.
            logger.warning("Could not read history from file: %s", self._history)
            return
        os.replace(history_filename, history_backup_filename)
            # If both `history` and `history.bak` are missing, the line above (amounting to `mv history.bak history.bak`) does not throw an error.

        self.delivery_status.load(history.get('delivery_status', {}))

        for envelope in history['envelopes']:
            if get_envelope_reaction(envelope):
                self.chats.add_reaction_envelope(envelope)
            elif get_envelope_remote_delete(envelope):
                self.chats.add_remote_delete_envelope(envelope)
            else:
                self.chats.add_envelope(envelope)

        self.unread_counts = UnreadCounts(history.get('unread_counts', {}))
        self.contacts_cache = history.get('contacts_cache', {})


# #############################################################################
# urwid palette
# #############################################################################


PALETTE = [
    ('bold', 'bold', ''),
    ('italic', 'italics', ''),
    ('bolditalic', 'bold,italics', ''),
    ('strikethrough', 'strikethrough', ''),
]

REVERSED_FOCUS_MAP = {
    None: 'reversed',
}


def _fill_palette():
    palette_reversed = []
    for item in PALETTE:
        name, fg = item[0:2]
        name_rev = '_'.join(('reversed', name))
        fg_rev = ','.join(('standout', fg))
        palette_reversed.append((name_rev, fg_rev, ''))
        REVERSED_FOCUS_MAP[name] = name_rev
    PALETTE.extend(palette_reversed)
    PALETTE.append(('reversed', 'standout', ''))
    PALETTE.append(('line_focused', 'dark blue', ''))


_fill_palette()


class Color:

    SIGNAL_COLORS_PALETTE = [
        ('pink',        'dark magenta', '', None,   '#f08',   None),
        ('red',         'dark red',     '', None,   '#f00',   None),
        ('orange',      'brown',        '', None,   '#f60',   None),
        ('purple',      'dark magenta', '', None,   '#a0f',   None),
        ('indigo',      'dark blue',    '', None,   '#60f',   None),
        ('blue_grey',   'brown',        '', None,   '#680',   None),
        ('ultramarine', 'dark blue',    '', None,   '#06f',   None),
        ('blue',        'dark cyan',    '', None,   '#06a',   None),
        ('teal',        'dark cyan',    '', None,   '#086',   None),
        ('green',       'dark green',   '', None,   '#0a0',   None),
        ('light_green', 'dark green',   '', None,   '#0d0',   None),
        ('brown',       'brown',        '', None,   '#880',   None),
        ('grey',        'light gray',   '', None,   'g52',    None),
    ]

        # The colors are defined in ..?
            # Signal-Android/app/src/main/java/org/thoughtcrime/securesms/contacts/avatars/ContactColorsLegacy.java
            # Signal-Android/app/src/main/res/values/material_colors.xml
        # Using `dark ...` colors, because many terminals show `light ...` as `bold`:
            # "Some terminals also will display bright colors in a bold font even if you don’t specify bold."
            # https://urwid.readthedocs.io/en/latest/manual/displayattributes.html#bold-underline-standout

    HIGH_COLOR_RE = re.compile(r"""
            \#[0-9A-Fa-f]{3}
            |
            g\#[0-9A-Fa-f]{2}
            |
            g[0-9]{1,3}
            |
            h[0-9]{1,3}
            """, re.VERBOSE)
        # https://urwid.readthedocs.io/en/latest/reference/attrspec.html#urwid.AttrSpec

    def __init__(self, args_color):
        self._args_color = args_color
        self.high_color_mode = False
        self._colors = self._set_color_palette()

    def _exit(self):
        sys.exit("ERROR: could not parse the `color` argument: " + repr(self._args_color))

    def _is_high_color(self, color_str):
        # Test if `color_str` is a "high-color" (256 colors) value
        return self.HIGH_COLOR_RE.fullmatch(color_str)

    def _add_palette_entry(self, name, val):
        if self._is_high_color(val):
            PALETTE.append((name, '', '', None, val, None))
            self.high_color_mode = True
        else:
            PALETTE.append((name, val, ''))

    def _set_color_palette(self):
        if self._args_color == 'high':
            self.high_color_mode = True

        if self._args_color is True or self._args_color == 'high':
            PALETTE.extend(self.SIGNAL_COLORS_PALETTE)
            return self._args_color

        try:
            color_spec = json.loads(self._args_color)
        except (TypeError, json.decoder.JSONDecodeError):
            self._exit()

        if isinstance(color_spec, list) and len(color_spec) == 2:
            for sent_or_recv, col in zip(
                    ('sent_color', 'recv_color'),
                    color_spec,
                    ):
                self._add_palette_entry(sent_or_recv, col)
            return color_spec
        elif isinstance(color_spec, dict):
            PALETTE.extend(self.SIGNAL_COLORS_PALETTE)
            # Adding a tuple to PALETTE that already has a tuple with the same "name" (i.e. the first item in tuple) overrides the old tuple.
            override_dict = {}
            for key, val in color_spec.items():
                self._add_palette_entry(key, val)
                if is_number(key):
                    override_dict[key] = key    # sic
            return override_dict
        else:
            return self._exit()   # `return` just to make pylint happy

    def for_message(self, msg):
        with suppress(TypeError, KeyError):
            return self._colors[msg.sender_num]
        if isinstance(self._colors, list):
            if is_envelope_outgoing(msg.envelope):
                return 'sent_color'
            else:
                return 'recv_color'
        if is_envelope_outgoing(msg.envelope):
            return 'default'
        try:
            return msg.sender.color
        except (TypeError, AttributeError):
            # In case `sender` is not in `Contacts`
            return 'default'


# #############################################################################
# ui utility
# #############################################################################


HORIZ_LINE_DIV_SYM = '—' # em dash


def markup_to_text(markup):
    # This is useful when we have only the markup; if we have the urwid.Text instance, can use its `.text` property instead.
    # Not currently used anywhere.
    if isinstance(markup, str):
        return markup
    elif isinstance(markup, tuple):
        return markup[1]
    else:
        return ''.join([markup_to_text(t) for t in markup])


def get_text_markup(text_widget):
    """Get urwid.Text widget text, in markup format.

    Like urwid.Text.get_text(), but returns a text markup that can be passed on to urwid.Text.set_text() or to urwid.Text() for creating a new text object"""

    text, display_attributes = text_widget.get_text()
    if not display_attributes:
        return text
    markup = []
    run_len_pos = 0
    for attr, attr_run_len in display_attributes:
        attr_run_end = run_len_pos + attr_run_len
        markup.append((attr, text[run_len_pos:attr_run_end]))
        run_len_pos = attr_run_end
    if run_len_pos != len(text):
        markup.append(text[run_len_pos:])
    return markup


def listbox_set_body(listbox, body_new):
    # Can't just do `listbox.body = body_new`:
    # https://github.com/urwid/urwid/issues/428
    # pylint: disable=protected-access
    if body_new is listbox.body:
        return
    urwid.disconnect_signal(listbox.body, "modified", listbox._invalidate)
    listbox.body = body_new
    urwid.connect_signal(listbox.body, "modified", listbox._invalidate)


class LineBoxRoundCorners(urwid.LineBox):
    def __init__(self, *args, **kwargs):
        super().__init__(
                *args,
                **kwargs,
                tlcorner=BOX_SYMBOLS.LIGHT.TOP_LEFT_ROUNDED,
                trcorner=BOX_SYMBOLS.LIGHT.TOP_RIGHT_ROUNDED,
                blcorner=BOX_SYMBOLS.LIGHT.BOTTOM_LEFT_ROUNDED,
                brcorner=BOX_SYMBOLS.LIGHT.BOTTOM_RIGHT_ROUNDED,
                )


class LineBoxWBottomEl(urwid.LineBox):

    def __init__(self, *args, bottom_el=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._bottom_el = bottom_el
        if bottom_el is not None:
            self.add_bottom_element(bottom_el)

    def add_bottom_element(self, el):
        bl = self._w.contents[-1][0].contents  # Get bottom line widgets (Columns' contents)
        bl.insert(1, bl[1]) # Duplicate Divider element
        bl.insert(2, (      # Instert `el` in the middle
            el,
            urwid.Columns.options(width_type='pack'),
            ))
        if el.selectable():
            self._w.focus_position = 2  # Focus linebox's bottom line
            self._w.focus.focus_position = 2 # Focus bottom_el (the middle element)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if (
            self._bottom_el is not None
            and
            self._bottom_el.selectable()
            and
            key in KEY_BINDINGS['focus_next_area'] | KEY_BINDINGS['focus_prev_area']
        ):
            self._w.focus_position = (
                    self._w.focus_position - 1
                    ) or 2      # Cycle between body and footer elements
            return None
        return key


class LineBoxCombined(LineBoxRoundCorners, LineBoxWBottomEl):
    pass


class LineBoxHighlight(urwid.WidgetWrap):
    def __init__(self, w, title='', **kwargs):
        box_w = urwid.AttrMap(
                LineBoxCombined(
                    urwid.AttrMap(w, ''),  # need to set a "default" attribute, to not color all the contents in `w`
                    title_align='center',
                    title=title,
                    **kwargs
                    ),
                None,
                focus_map='line_focused',
                )
        super().__init__(box_w)


class PopUpBox(urwid.WidgetWrap):

    signals = ['closed']

    def __init__(self, widget, title='', buttons=True, shadow_len=0):
        self._buttons = buttons
        with suppress(NameError):
            urwid.connect_signal(widget, 'closed', self._emit, user_args=['closed'])

        if buttons:
            def handle_click(_button):
                self._emit('closed')
            btn_close = ButtonBox('Close', on_press=handle_click)
            box_w = LineBoxHighlight(widget, title, bottom_el=btn_close)
        else:
            box_w = LineBoxHighlight(widget, title)

        if shadow_len:
            ### Shadow effect. (Based on urwid/examples/dialog.py)
            box_w = urwid.Columns([
                    box_w,
                    ('fixed', shadow_len, urwid.AttrWrap(
                        urwid.Filler(
                            urwid.Text(('default', ' '*shadow_len)),
                            "top"
                            ),
                        'reversed'))
                    ])
            box_w = urwid.Frame(
                    box_w,
                    footer = urwid.Padding(
                        urwid.AttrMap(
                            urwid.Divider(
                                BAR_SYMBOLS.VERTICAL[4]
                                ),
                            "reversed",
                            ),
                        left=shadow_len
                        )
                    )

        super().__init__(box_w)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if key in KEY_BINDINGS['close_popup']:
            self._emit('closed')
        else:
            return key
        return None


class FocusableText(urwid.WidgetWrap):
    def __init__(self, markup, attr_map=None, **kwargs):
        self._text_w = urwid.Text(markup, **kwargs)
        w = urwid.AttrMap(self._text_w, attr_map, focus_map=REVERSED_FOCUS_MAP)

        super().__init__(w)

    def selectable(self):
        # Setting class variable `_selectable = True` does not work. Probably gets overwritten by the base class constructor.
        return True

    def keypress(self, _size, key):
        # When reimplementing selectable(), have to redefine keypress() too.
        # https://urwid.readthedocs.io/en/latest/reference/widget.html#urwid.Widget.selectable
        return key

    def __getattr__(self, attr):
        return getattr(self._text_w, attr)


class ButtonBox(urwid.WidgetWrap):

    signals = ["click"]

    def __init__(self, label, on_press=None, user_data=None, align='center', decoration='[]'):
        text_w = FocusableText(label, align=align)
        if decoration == 'box':
            w = LineBoxRoundCorners(text_w)
        elif decoration is not None:
            w = urwid.Columns(
                [
                    (len(decoration[0]), urwid.Text(decoration[0])),
                    ("pack", text_w),
                    (len(decoration[1]), urwid.Text(decoration[1])),
                    ],
                )
        else:
            w = text_w
        super().__init__(w)
        if on_press:
            urwid.connect_signal(self, "click", on_press, user_data)

    def keypress(self, size, key):
        if self._command_map[key] != urwid.ACTIVATE:
            return key
        self._emit("click")
        return None

    def mouse_event(self, _size, event, button, _x, _y, _focus):
        if button != 1 or not urwid.util.is_mouse_press(event):
            return False
        self._emit("click")
        return True


class LazyEvalListWalker(urwid.ListWalker):

    """A ListWalker that creates widgets only as they come into view.

    This ListWalker subclass saves resources by deferring widgets creation until they are actually visible. For large `contents` list, most of the items might not be viewed in a typical usage.

    "If you need to display a large number of widgets you should implement your own list walker that manages creating widgets as they are requested and destroying them later to avoid excessive memory use."
    https://urwid.readthedocs.io/en/latest/manual/widgets.html#list-walkers
    """

    def __init__(self, contents, eval_func, init_focus_pos=0):
        if not getattr(contents, '__getitem__', None):
            raise urwid.ListWalkerError("ListWalker expecting list like object, got: %r" % (contents,))
        self._init_focus_pos = init_focus_pos
        self._eval_func = eval_func
        self.contents = contents
        super().__init__()  # Not really needed, just here to make pylint happy.

    @property
    def contents(self):
        return self._contents

    @contents.setter
    def contents(self, contents_new):
        self._remove_contents_modified_callback()
        self._contents = contents_new
        self._set_contents_modified_callback(self._modified)

        if self._init_focus_pos < 0:
            self.focus = max(0, len(self.contents) + self._init_focus_pos)
        else:
            self.focus = self._init_focus_pos

        self._modified()

    def _set_contents_modified_callback(self, callback):
        try:
            self.contents.set_modified_callback(callback)
        except AttributeError:
            logger.warning(
                    "Changes to object will not be automatically updated: %s",
                    textwrap.shorten(str(self.contents), 150),
                    )

    def _remove_contents_modified_callback(self):
        with suppress(AttributeError):
            self.contents.set_modified_callback(noop)

    def _modified(self):
        if self.focus >= len(self.contents):
            # Making sure that if after some items are removed from `contents` it becomes shorter then the current `focus` position, we don't crash.
            self.focus = max(0, len(self.contents) - 1)
        super()._modified()

    def __getitem__(self, position):
        item = self.contents[position]
        widget = self._eval_func(item, position)
        return widget

    def next_position(self, position):
        if position >= len(self.contents) - 1:
            raise IndexError
        return position + 1

    def prev_position(self, position):
        if position <= 0:
            raise IndexError
        return position - 1

    def set_focus(self, position):
        if position < 0 or position >= len(self.contents):
            raise IndexError
        self.focus = position
        self._modified()

    def positions(self, reverse=False):
        ret = range(len(self.contents))
        if reverse:
            ret = reversed(ret)
        return ret


class ViBindingsMixin(urwid.Widget):

    _KEY_MAP = {
            'h': urwid.CURSOR_LEFT,
            'j': urwid.CURSOR_DOWN,
            'k': urwid.CURSOR_UP,
            'l': urwid.CURSOR_RIGHT,
            'g': urwid.CURSOR_MAX_LEFT,  # 'home'
            'G': urwid.CURSOR_MAX_RIGHT, # 'end'
            'ctrl p':   urwid.CURSOR_UP,
            'ctrl n':   urwid.CURSOR_DOWN,
            'ctrl b':   urwid.CURSOR_PAGE_UP,
            'ctrl f':   urwid.CURSOR_PAGE_DOWN,
            }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, action in self._KEY_MAP.items():
            # urwid's CommandMap does not have an `update()` method.
            self._command_map[key] = action

    def render(self, *arsg, **kwargs):
        raise NotImplementedError


class ListBoxPlus(urwid.ListBox, ViBindingsMixin):

    """ListBox plus a few useful features.

    - Vim bindings for common motions: j, k, g, G, ctrl+n/p.
    - Filter visible contents to the items passing test by a given function.
    - Updates to new `contents` are displayed automatically. Fixes an urwid bug (see listbox_set_body function).
    """

    def __init__(self, body=None):
        if body is None:
            body = []
        super().__init__(body)
        self._contents_pre_filter = self.contents

    def _get_contents(self):
        try:
            return self.body.contents
        except AttributeError:
            return self.body

    def _set_contents(self, contents_new):
        # This method does not change the self._contents_pre_filter, unlike self._set_contents_pre_filter()
        try:
            self.body.contents = contents_new
        except AttributeError:
            listbox_set_body(self, contents_new)

    def _set_contents_pre_filter(self, contents_new):
        if type(contents_new) is list:      # pylint: disable=unidiomatic-typecheck
            # If contents_new is a `list` (not one of the `ListWalker`s), make the new body the same type as the original (e.g. SimpleListWalker)
            # Shouldn't use `if isinstance(contents_new, list)` test: a ListWalker returns `True` for it too.
            contents_new = type(self.contents)(contents_new)
        self._set_contents(contents_new)
        self._contents_pre_filter = self.contents

    contents = property(_get_contents, _set_contents_pre_filter)
        # Would be nice to override the base class's `body` property, so that this class can be easily replaced by any other `ListWalker`s.
            # However, overriding a property which is used in superclass's __init__ seems problematic. Need a way to delay the assignment of property. Maybe something like this is necessary:
            # https://code.activestate.com/recipes/408713-late-binding-properties-allowing-subclasses-to-ove/

    def try_set_focus(self, index, valign=None):
        index_orig_arg = index
        if index < 0:
            index = len(self.contents) + index
        try:
            self.focus_position = index
        except IndexError:
            return
        if index_orig_arg == -1 and valign is None:
            valign = 'bottom'
        if valign is not None:
            self.set_focus_valign(valign)

    def filter_contents(self, test_function, scope=None):
        """Remove widgets not passing `test_function`.

        Retain only the items in `self.contents` that return `True` when passed as arguments to `test_function`. Pre-filtered `contents` is stored before filtering and can be restored by running `filter_contents` again with `test_function=None`.
        The `scope` argument specifies the itarable to apply the filter to. By default, the scope is all the pre-filtered items. Passing `scope=self.contents' can be useful to further filter an already filtered contents.
        """

        # Note that if `contents` is modified directly elsewhere in the code while a filter is on, this modification applies only to the filtered contents. So, for instance the code for adding a new MessageWidget to ChatView shouldn't do `self.contents.append()`, but rather `current_chat.append()` (after doing `_set_contents_pre_filter(current_chat)`). That way the new msg will show up after the filter is removed.
        # Alternatively, can do `self._contents_pre_filter.append()`. That should work fine either with filter on or off.

        if scope is None:
            scope = self._contents_pre_filter
        if test_function is None:
            self._set_contents(scope)
        else:
            contents_type = type(self.contents)
            matching_widgets = contents_type([w for w in scope if test_function(w)])
            self._set_contents(matching_widgets)

    @property
    def is_filter_on(self):
        return self.contents is not self._contents_pre_filter

    def move_item(self, w, pos, pos_in_prefilter=None):
        def try_move(seq, w, pos):
            try:
                ind = seq.index(w)
            except ValueError:
                # Widget might be absent from `body` e.g. while doing a search on contacts, or if the contact is 'new' (i.e. not in Contacts yet)
                return
            if ind == pos:
                return
            seq.insert(pos, seq.pop(ind))

        try_move(self.contents, w, pos)

        if self.is_filter_on:
            if pos_in_prefilter is None:
                pos_in_prefilter = pos
            try_move(self._contents_pre_filter, w, pos_in_prefilter)


# #############################################################################
# contacts widgets
# #############################################################################


class ContactWidget(FocusableText):

    SEND_FAILED_MARKUP = '✖'
    NOTE_TO_SELF_MARKUP = ('italic', ' (Self)')
    GROUP_MARKUP = ('italic', ' [GRP]')
    HIGHLIGHT_MARKUP_ATTR = 'bold'

    def __init__(self, contact):
        self.contact = contact
        self._fail_mark_set = False
        self._highlight = False
        self._unread_count = 0
        self._name_markup = self._get_name_markup()
        super().__init__(self._name_markup)

    def _get_name_markup(self):
        markup = []
        name = self.contact.name_or_id
        markup.append(name)
        if self.contact.is_group and not cfg.partition_contacts:
            markup.append(self.GROUP_MARKUP)
        elif self.contact.id == cfg.username:
            markup.append(self.NOTE_TO_SELF_MARKUP)
        return markup

    def _update_markup(self):
        markup = []
        if self._fail_mark_set:
            markup.extend([self.SEND_FAILED_MARKUP, " "])
        if self._unread_count:
            markup.extend([('bold', f"({self._unread_count})"), " "])
                # Moving the " " into the ('bold', ..) element removes the italic in [GRP] when contact selected and unread count shown.
        if self._highlight:
            markup.append((self.HIGHLIGHT_MARKUP_ATTR, self._name_markup))
        else:
            markup.extend(self._name_markup)
        self.set_text(markup)

    @property
    def unread_count(self):
        return self._unread_count

    @unread_count.setter
    def unread_count(self, count):
        if count == self._unread_count:
            return
        self._unread_count = count
        self._update_markup()

    @property
    def fail_mark_set(self):
        return self._fail_mark_set

    @fail_mark_set.setter
    def fail_mark_set(self, true_false):
        if self._fail_mark_set == true_false:
            return
        self._fail_mark_set = true_false
        self._update_markup()

    @property
    def highlight(self):
        return self._highlight

    @highlight.setter
    def highlight(self, new_val):
        if self._highlight == new_val:
            return
        self._highlight = new_val
        self._update_markup()


class PartitionedContactsListWalker(urwid.SimpleListWalker):
    """Ensure that when `partition_contacts == True` only the ContactWidget objects can be in focus (not the headers or divider widgets).

    If there are no ContactWidget objects it will focus on the last widget in `self.contents`.
    """

    def set_focus(self, position):
        # Overriding the base class's function to make sure only ContactWidget type objects may be in focus.
        # When the widget at `position` is not a ContactWidget, try the ones below it until we find one or reach the end.
        for pos in range(position, len(self)):
            w = self[pos]
            if type(w) is ContactWidget:      # pylint: disable=unidiomatic-typecheck
                # Check that widget is of exactly ContactWidget type, not one of its base classes.
                return super().set_focus(pos)
        return None

    def set_modified_callback(self, callback):
        # Abstract method, inherited from urwid.MonitoredList; has to be overriden in the concrete class.
        # See base class's docs: urwid.SimpleListWalker.set_modified_callback
        raise NotImplementedError(
                'Use connect_signal(list_walker, "modified", ...) instead.'
                )


class ContactsListWidget(ListBoxPlus):
    signals = ['contact_selected']

    def __init__(self, contacts, chats_data):
        super().__init__(
                urwid.SimpleListWalker([])
                if not cfg.partition_contacts else
                PartitionedContactsListWalker([])
                )
        self._contacts = contacts
        self._chats_data = chats_data
        self._contact_widgets_map = {}
        self.update()

    def _get_sorted_contacts(self):
        def sorter(contact):
            contact_name = contact.name_or_id
            if cfg.contacts_sort_alpha:
                return contact_name.casefold()
            try:
                chat = self._chats_data.chats[contact.id]
                last_msg = chat[-1]
            except (KeyError, IndexError):
                return (0, contact_name.casefold())
            return (-last_msg.local_timestamp, contact_name.casefold())

        if not cfg.partition_contacts:
            return sorted(self._contacts.map.values(), key=sorter)
        else:
            grps = sorted(self._contacts.groups, key=sorter)
            cnts = sorted(self._contacts.indivs, key=sorter)
            return (grps, cnts)

    def update(self):
        sorted_contacts = self._get_sorted_contacts()
        if not cfg.partition_contacts:
            self.contents = [ContactWidget(contact) for contact in sorted_contacts]
            self._contact_widgets_map = {w.contact.id: w for w in self.contents}
        else:
            group_contact_widgets = [ContactWidget(contact) for contact in sorted_contacts[0]]
            indiv_contact_widgets = [ContactWidget(contact) for contact in sorted_contacts[1]]
            div_w = urwid.Divider(HORIZ_LINE_DIV_SYM)
            group_cont_section_title = urwid.Text(('bold', '~~ Groups ~~'), align='center')
            indiv_cont_section_title = urwid.Text(('bold', '~~ Contacts ~~'), align='center')
            widgets = (
                    [group_cont_section_title, div_w]
                    + group_contact_widgets
                    + [div_w, indiv_cont_section_title, div_w]
                    + indiv_contact_widgets
                    )
            self._indiv_header_w = indiv_cont_section_title  # Used in _move_contact_top() for getting its index position
            self.contents = widgets
            self._contact_widgets_map = {w.contact.id: w for w in group_contact_widgets + indiv_contact_widgets}
        self._set_all_ws_unread_counts()
        with suppress(AttributeError): # If current_contact is None
            self._get_current_contact_widget().highlight = True
        self.try_set_focus(0)

    def _set_all_ws_unread_counts(self):
        for contact_id, contact_widget in self._contact_widgets_map.items():
            unread_count = self._chats_data.unread_counts.get(contact_id, 0)
            if unread_count:
                contact_widget.unread_count = unread_count

    def update_contact_unread_count(self, contact_id):
        contact_widget = self._contact_widgets_map.get(contact_id)
        if contact_widget is not None:
            # The widget is None if received a msg from a 'new' contact (one not in the read signal-cli's data file)
            contact_widget.unread_count = self._chats_data.unread_counts[contact_id]

    def on_new_message(self, msg):
        contact_widget = self._contact_widgets_map.get(msg.contact_id)
        if not cfg.contacts_sort_alpha and contact_widget is not None:
            self._move_contact_top(contact_widget)

    def on_sending_done(self, envelope, status='sent', _timestamp_adj=None):
        # Show a "send failed" symbol next to the contact, but not if it's the "current" contact (whose chat is opened).
        if status != 'send_failed':
            return
        current_contact = self._chats_data.current_contact
        if current_contact is None:
            # If contacts' update happens while sending, and current_contact no longer in contacts.
            return
        envelope_contact_id = get_envelope_contact_id(envelope)
        if current_contact.id == envelope_contact_id:
            return
        contact_widget = self._contact_widgets_map[envelope_contact_id]
        contact_widget.fail_mark_set = True

    def _move_contact_top(self, w):
        pos_in_prefilter = None
        if not cfg.partition_contacts:
            pos_new = 0
        else:
            if w.contact.is_group:
                pos_new = 2
            elif not self.is_filter_on:
                pos_new = len(self._contacts.groups) + 5  # 2 for "Groups" header and 3 for "Contacts"
            else:
                pos_new = self.contents.index(self._indiv_header_w) + 2
                pos_in_prefilter = len(self._contacts.groups) + 5
        self.move_item(w, pos_new, pos_in_prefilter)
        self.try_set_focus(pos_new)

    def _get_current_contact_widget(self):
        current_contact = self._chats_data.current_contact
        if current_contact is None:
            return None
        return self._contact_widgets_map[current_contact.id]

    def _get_focused_contact_widget(self):
        focused_contact_w = self.focus
        # NOTE: self.focus can be None e.g. when searching through contacts returns no results.
        if cfg.partition_contacts and not isinstance(focused_contact_w, ContactWidget):
            # Widget in focus is urwid.Text (a header) or urwid.Divider. They are normally not supposed to get the focus, but sometimes may: e.g. after pressing `home`, or after doing a search with `/`, or when there are no other widgets (e.g. no search results).
            return None
        return focused_contact_w

    def _unhighlight_current_contact_widget(self):
        # Remove highlighting from the "current" (not for long) contact's widget.
        with suppress(AttributeError): # If current_contact is None
            self._get_current_contact_widget().highlight = False

    def _select_focused_contact(self, focus_widget=None):
        # The `focus_widget` parameter is passed through from caller to emit_signal. It specifies whether the focus should be set on `input`, `chat` or `contacts` widgets after switching to a new contact.
        focused_contact_w = self._get_focused_contact_widget()
        if focused_contact_w is None:
            return
        contact = focused_contact_w.contact
        focused_contact_w.fail_mark_set = False
        self._unhighlight_current_contact_widget()
        urwid.emit_signal(self, 'contact_selected', contact, focus_widget)
        focused_contact_w.highlight = True

    def select_next_contact(self, reverse=False):
        current_contact = self._chats_data.current_contact
        if current_contact == self.focus.contact or current_contact is None:
            curr_position = self.focus_position
        else:
            contact_w = self._contact_widgets_map[current_contact.id]
            curr_position = self.contents.index(contact_w)
        try:
            focus_position_new = (
                    self.body.next_position(curr_position)
                    if not reverse else
                    self.body.prev_position(curr_position)
                    )
        except IndexError:
            return
        #focus_position_new = self.focus_position - int((reverse - 0.5) * 2)    # Alternative way of obtaining the new position
        if (cfg.partition_contacts
                and reverse
                and not isinstance(self.contents[focus_position_new], ContactWidget)
                and focus_position_new != 1):
            # Jumping over the `~~ Contacts ~~` header when going up.
            focus_position_new -= 3
        try:
            self.set_focus(focus_position_new, coming_from='below' if reverse else 'above')
        except IndexError:
            return
        self._select_focused_contact()

    def _increment_focused_unread_count(self):
        # NOTE: Does not increment unread count in the status line. However it will be updated after switching to another contact.
        focused_contact_w = self._get_focused_contact_widget()
        if focused_contact_w is None:
            return
        contact_id = focused_contact_w.contact.id
        self._chats_data.unread_counts[contact_id] += 1
        self.update_contact_unread_count(contact_id)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if key in KEY_BINDINGS['enter']:
            self._select_focused_contact(focus_widget='input')
        elif key in KEY_BINDINGS['open_contact_chat']:
            self._select_focused_contact()
        elif key in KEY_BINDINGS['mark_unread']:
            self._increment_focused_unread_count()
        else:
            return key
        return None


class ContactsWindow(urwid.Frame):
    def __init__(self, contacts, chats_data):
        self.contacts_list_w = ContactsListWidget(contacts, chats_data)
        self._wsearch = BracketedPasteEdit(('bold', KEY_BINDINGS.search_sym))

        urwid.connect_signal(self._wsearch, 'postchange', self._on_search_text_changed)

        super().__init__(self.contacts_list_w, footer=None)

        if not cfg.partition_contacts:
            self.header = urwid.Pile([
                urwid.Text(('bold', 'Contacts'), align='center'),
                urwid.Divider(HORIZ_LINE_DIV_SYM)
                ])

    def _start_search(self):
        self.footer = self._wsearch
        self.focus_position = 'footer'

    def _remove_search(self):
        self._wsearch.set_edit_text('')
        self.focus_position = 'body'
        self.footer = None

    def _on_search_text_changed(self, input_w, _old_text):
        def match_test(contact_w):
            try:
                contact = contact_w.contact
            except AttributeError:
                # Keep the `partition_contacts` headers / dividers
                return True
            return (
                    txt.casefold() in contact.name_or_id.casefold()
                    or
                    not contact.is_group and txt in contact.id
                    )
        txt = input_w.get_edit_text()
        match_test = None if not txt else match_test
        self.contacts_list_w.filter_contents(match_test)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if key in KEY_BINDINGS['search_input']:
            self._start_search()
        elif key in KEY_BINDINGS['enter'] and self.focus_position == 'footer':
            self.focus_position = 'body'
            self.contacts_list_w.try_set_focus(0)
        elif key in KEY_BINDINGS['clear']:
            self._remove_search()
        else:
            return key
        return None


# #############################################################################
# input line
# #############################################################################


class CommandsHistory:
    def __init__(self):
        self._history = []
        self._index = 0
        self._stashed_input = None

    def prev(self, curr_input):
        if (curr_input != self._stashed_input
                and self._history
                and curr_input != self._history[self._index]):
            # This check fixes the following unexpected behavior:
            # Type `:whatev`, press `up` a few times, then delete the input with e.g. `backspace`. Next time the history will be looked up from where it's been left this time.
            self._index = 0
        if self._index == 0:
            self._stashed_input = curr_input
        self._index -= 1
        try:
            return self._history[self._index]
        except IndexError:
            self._index += 1
            return curr_input

    def next(self, curr_input):
        if self._index == 0:
            return curr_input
        self._index += 1
        if self._index == 0:
            return self._stashed_input
        return self._history[self._index]

    def add(self, cmd):
        self._history.append(cmd)
        self._index = 0


class BracketedPasteEdit(Edit):
    def __init__(self, *args, multiline=False, **kwargs):
        super().__init__(*args, multiline=True, **kwargs)
        # Using `multiline=True` in super() and then passing on 'enter' keypress to it. A nicer alternative would be to pass '\n', but Edit does not handle it.
        self._multiline_arg = multiline
        self._paste_mode_on = False

    def keypress(self, size, key):
        if key == 'begin paste':
            self._paste_mode_on = True
        elif key == 'end paste':
            self._paste_mode_on = False
        elif key in KEY_BINDINGS['enter'] and not (self._multiline_arg or self._paste_mode_on):
            return key
        elif key in KEY_BINDINGS['input_newline']:
            # Allow inserting new lines with Alt+Enter. This is not a part of "bracketed paste mode" functionality.
            return super().keypress(size, 'enter')
        else:
            return super().keypress(size, key)
        return None


class InputLine(BracketedPasteEdit):
    def __init__(self, **kwargs):
        self._cmd_history = CommandsHistory()
        self.cmds = None
        self._prompt = ('bold', '> ')
        super().__init__(self._prompt, **kwargs)

    def set_cmds(self, cmds):
        self.cmds = cmds

    def _set_edit_text_move_cursor(self, txt, cursor_pos=-1):
        """Edit.set_edit_text() + Edit.set_edit_pos()

        Like Edit.insert_text(), but istead of adding to the current edit_text, replace it with the provided argument.
        """
        self.set_edit_text(txt)
        if cursor_pos == -1:
            cursor_pos = len(txt)
        self.set_edit_pos(cursor_pos)

    def auto_complete_commands(self, txt):
        # See also: there is an autocomplete in rr-/urwid_readline
        splitted_txt = txt.split(' ')
        if len(splitted_txt) > 1:
            path, message = partition_escaped(' '.join(splitted_txt[1:]))

            # Check we are trying to complete a path
            if message or not is_path(path):
                return

            fullpath = os.path.expanduser(path)
            dirname = os.path.dirname(fullpath)
            if not os.path.isdir(dirname):
                return

            possible_paths = [x for x in os.listdir(dirname) if os.path.join(dirname, x).startswith(fullpath)]
            commonprefix = os.path.commonprefix(possible_paths)

            action_request.set_status_line(
                    textwrap.shorten(
                        ' | '.join(sorted(possible_paths)),
                        width=240,
                        ))

            completion = ''
            if commonprefix != '':
                completion = os.path.join(os.path.dirname(path), commonprefix)
            if os.path.isdir(os.path.expanduser(completion)) and not completion.endswith('/'):
                completion = completion + '/'
            if ' ' in completion:
                completion = '"' + completion + '"'

            if completion != '':
                self._set_edit_text_move_cursor(splitted_txt[0] + ' ' + completion)
        else:
            all_commands = [
                cmd
                for cmd in [tupl[0][0] for tupl in self.cmds.cmd_mapping]
                if cmd.lower().startswith(txt[1:].lower())
            ]
            commonprefix = os.path.commonprefix(all_commands)

            action_request.set_status_line('{' + ' | '.join(sorted(all_commands)) + '}')

            if len(all_commands) == 1:
                self._set_edit_text_move_cursor(':' + all_commands[0] + ' ')
            elif commonprefix != '':
                self._set_edit_text_move_cursor(':' + commonprefix)

    def _keypress_cmd_mode(self, key, key_orig, txt):
        # Called when `txt.startswith(':')`
        if key in KEY_BINDINGS['enter']:
            if txt.strip() == KEY_BINDINGS.cmd_sym:
                action_request.set_status_line(f'Command missing after `{KEY_BINDINGS.cmd_sym}`')
                return None
            cmd, *args = txt[1:].split(maxsplit=1)
            self.cmds.exec(cmd, *args)
            self._cmd_history.add(txt)
            self.set_edit_text('')
            self.set_caption(self._prompt)
        elif key in KEY_BINDINGS['auto_complete'] and not self.get_edit_text().endswith(' '):
            self.auto_complete_commands(txt)
        elif key_orig in ('up', 'ctrl p'):
            # Since BracketedPasteEdit is based on Edit(multiline=True), the up / down / ctrl+p/n are consumed by the superclass, so need to check `key_orig`, before `super` method call.
            prev_cmd = self._cmd_history.prev(txt)
            self._set_edit_text_move_cursor(prev_cmd)
        elif key_orig in ('down', 'ctrl n'):
            next_cmd = self._cmd_history.next(txt)
            self._set_edit_text_move_cursor(next_cmd)
        else:
            return key
        return None

    def keypress(self, size, key):
        key_orig = key
        key = super().keypress(size, key)
        txt = self.get_edit_text()

        if not txt or txt.isspace():
            self.set_caption(self._prompt)  # restore normal prompt
            return key
        if txt.startswith((KEY_BINDINGS.search_sym, KEY_BINDINGS.cmd_sym)):
            self.set_caption('')  # set "prompt" to '/' or ':'
            if key in KEY_BINDINGS['clear']:
                self.set_edit_text('')
                self.set_caption(self._prompt)
                return None
        else:
            self.set_caption(self._prompt)
        # Bind readline equivalents
        if key in KEY_BINDINGS['readline_word_left']:
            return super().keypress(size, 'meta b')
        if key in KEY_BINDINGS['readline_word_right']:
            return super().keypress(size, 'meta f')
        #if key == 'ctrl backspace':
            # uwrid registers 'ctrl backspace' as just 'backspace'.. Use 'ctrl w' or 'meta backspace' instead.
            #return super().keypress(size, 'ctrl w')
        # /end: Bind readline equivalents
        if txt.startswith(KEY_BINDINGS.cmd_sym):
            return self._keypress_cmd_mode(key, key_orig, txt)
        elif key in KEY_BINDINGS['enter']:
            if txt.startswith(KEY_BINDINGS.search_sym):
                return key
            action_request.send_message_curr_contact(txt)
            self.set_edit_text('')
        else:
            return key
        return None


# #############################################################################
# conversation widgets
# #############################################################################


class MessageReactionsWidget(urwid.WidgetWrap):

    def __init__(self, emojis_markup, align):
        self._align = align
        method = self._init_row_w if not cfg.show_inline else self._init_col_w
        self._text_w, display_w = method(emojis_markup)
        super().__init__(display_w)

    def _init_row_w(self, emojis_markup):
        text_w = urwid.Text(emojis_markup, align=self._align)
        react_pad_w = urwid.Padding(text_w, self._align, width=cfg.wrap_at)
        react_sym_markup = '╰╴' if self._align == 'left' else '╶╯'
        react_sym_w = urwid.Text(
                react_sym_markup,
                align='right' if self._align == 'left' else 'left',
                )
        cols = [
                (DeliveryStatus.MARKUP_WIDTH + len(react_sym_markup), react_sym_w),
                react_pad_w,
                ]
        if self._align == 'right':
            cols.reverse()
        react_cols_w = urwid.Columns(cols)
        return text_w, react_cols_w

    def _init_col_w(self, emojis_markup):
        text_w = urwid.Text(self._col_w_markup(emojis_markup))
        return text_w, text_w

    def _col_w_markup(self, emojis_markup):
        sep = BOX_SYMBOLS.LIGHT.VERTICAL_4_DASH  # '┊'
        return (
                [*emojis_markup, sep]
                if self._align == 'right' else
                [sep, *emojis_markup]
                )

    def update(self, emojis_markup):
        if not cfg.show_inline:
            self._text_w.set_text(emojis_markup)
        else:
            self._text_w.set_text(self._col_w_markup(emojis_markup))


class MessageWidget(urwid.WidgetWrap):

    MAX_ATTACHS_SHOW = 4

    TYPING_INDICATOR_MARKUP = '...'
    REMOTE_DELETE_MARKUP = ('italic', '[deleted]')
    STICKER_MARKUP = ('italic', '[sticker]')

    FORMAT_MAP = {'_': 'italic', '*': 'bold', '~': 'strikethrough'}
    MENTION_BRACKET_CHAR = chr(31)  # arbitrary non-printable char
    FORMAT_MENTION = {MENTION_BRACKET_CHAR: 'italic'}
    FORMATTING_RE = None

    MARKUP_ELS_SEPARATOR = '\n'
    SENDER_NAME_COL_WIDTH = 12
    MSG_MARKUP_PRE = ()

    @classmethod
    def set_formatting_consants(cls, use_formatting):
        if use_formatting:
            cls.FORMAT_MAP.update(cls.FORMAT_MENTION)
        else:
            cls.FORMAT_MAP = cls.FORMAT_MENTION
        cls.FORMATTING_RE = re.compile(
            # Match text like "_italicised_", where "_" is a char in FORMAT_MAP
            rf"""
                (
                    [{''.join(cls.FORMAT_MAP.keys())}]
                )
                #.+?        # bad with doubled format chars, e.g. ~~this~~
                #[^\1]+     # can't use backreferences in character class
                (?:
                    (?!\1). # consume a char and check it's not a format char
                )+
                \1
            """,
            re.VERBOSE)
        if cfg.show_inline == 'columns':
            if not cfg.one_sided:
                cls.SENDER_NAME_COL_WIDTH = 'pack'
        else:
            cls._get_inline_columns = lambda self: []
            cls.MSG_MARKUP_PRE = (
                cls._get_time_markup,
                cls._get_sender_markup,
                )
            if cfg.show_inline == 'wrap':
                cls.MARKUP_ELS_SEPARATOR = ' '

    def __init__(self, msg):
        self.msg = msg
        self.align = (
                'left'
                if (not is_envelope_outgoing(self.msg.envelope)
                    or cfg.one_sided)
                else 'right'
                )
        msg_markup = self._get_message_markup()
        self._text_w = FocusableText(msg_markup or '', align=self.align)
            # urwid.Text throws an error if given an empty list for markup. Not sure `msg_markup` can ever end up being empty though.
        msg_pad_w = urwid.Padding(self._text_w, self.align, width=cfg.wrap_at)
        status_markup = self._get_status_markup()
        self._status_w = urwid.Text(status_markup, self.align)
        status_w_valign = 'top' if self.align == 'left' else 'bottom'
        status_filler_w = urwid.Filler(self._status_w, status_w_valign)
        cols = [(DeliveryStatus.MARKUP_WIDTH, status_filler_w), *self._get_inline_columns(), msg_pad_w]
        box_columns = [0]
        if self.align == 'right':
            cols.reverse()
            box_columns = [len(cols)-1]
        self._columns_w = urwid.Columns(cols, dividechars=1, box_columns=box_columns)
        self._color = None if not cfg.color else cfg.color.for_message(msg)
        display_w = urwid.AttrMap(self._columns_w, self._color, focus_map=REVERSED_FOCUS_MAP)
        super().__init__(display_w)
        self._reactions_w = None
        self.update_reactions_w()
        if cfg.show_message_padding:
            self._add_pile_row(urwid.Divider(cfg.show_message_padding))

    def _get_inline_columns(self):
        return [
                (width,
                    urwid.Text(markup)
                    if width == 'pack' else
                    urwid.Padding(
                        urwid.Text(markup, wrap='clip'),
                        align='right',
                        width='pack',
                        )
                    )
                for width, markup in (
                    ('pack', self._get_time_markup()),
                    (self.SENDER_NAME_COL_WIDTH, self._get_sender_markup()),
                ) if markup is not None
                ]

    def _get_message_markup(self):
        markups = []
        if self.MSG_MARKUP_PRE:
            # Show sender's name and message timesamp on the same line if both --show-names --show-message-time are true.
            markups_list = [f(self) for f in self.MSG_MARKUP_PRE]
            for elm in markups_list if self.align == 'left' else reversed(markups_list):
                if elm:
                    markups.extend((*elm, ' '))
            markups = [markups]
        if 'typingMessage' in self.msg.envelope:
            markups.append(
                    [self.TYPING_INDICATOR_MARKUP],
                    )
        elif self.msg.envelope.get('callMessage') is not None:
            markups.append(
                    self._get_call_message_markup(),
                    )
        elif getattr(self.msg, 'remote_delete', None):
            markups.append(
                    [self.REMOTE_DELETE_MARKUP],
                    )
        elif get_envelope_sticker(self.msg.envelope) is not None:
            markups.append(
                    [self.STICKER_MARKUP],
                    )
        else:
            markups.extend([
                    self._get_quote_markup(),
                    self._get_text_markup(),
                    self._get_attachments_markup(),
                    ])
        ret = []
        for markup in markups:
            if markup:
                if ret:
                    ret.append(self.MARKUP_ELS_SEPARATOR)
                ret.extend(markup)
        return ret

    @classmethod
    def _get_text_markup_generic(cls, text, mentions):
        if not text:
            return None
        if not (cfg.use_formatting or mentions):
            return [text]
        if mentions:
            text = Message.text_w_mentions_generic(
                    text,
                    mentions,
                    bracket_char=cls.MENTION_BRACKET_CHAR,
                    )
        ret = []
        pos = 0
        for match in cls.FORMATTING_RE.finditer(text):
            if pos != match.start():
                # Do not add empty strings. Urwid breaks on markup like:
                # [.., ('bold', 'txt1'), '', ('bold', 'txt2'), ...]
                ret.append(text[pos : match.start()])
            ret.append((cls.FORMAT_MAP[match[1]], match.group()[1:-1]))
            pos = match.end()
        if pos != len(text):
            ret.append(text[pos:])
        return ret

    def _get_text_markup(self):
        return self._get_text_markup_generic(
                get_envelope_msg(self.msg.envelope),
                self.msg.mentions,
                )

    @classmethod
    def _get_attachments_markup_generic(cls, attachments):
        if not attachments:
            return None
        attach_list = [get_attachment_name(attach) for attach in attachments]
        if len(attachments) > cls.MAX_ATTACHS_SHOW:
            attach_list = attach_list[: cls.MAX_ATTACHS_SHOW]
            attach_list.append(f'... ({len(attachments)-cls.MAX_ATTACHS_SHOW} more)')
        attach_txt = ', '.join(attach_list)
        return ['[attached: ', ('italic', attach_txt), ']']

    def _get_attachments_markup(self):
        return self._get_attachments_markup_generic(self.msg.attachments)

    def _get_time_markup(self):
        if not cfg.show_message_time:
            return None
        time_markup = strftimestamp(self.msg.timestamp, cfg.show_message_time)
        return [('italic', time_markup)]

    def _get_sender_markup(self):
        envelope = self.msg.envelope
        is_group = is_envelope_group_message(envelope)
        if not (is_group or cfg.show_names):
            return None
        if is_envelope_outgoing(envelope):
            if not (cfg.show_names or cfg.show_inline=='columns' and cfg.one_sided):
                return None
            sender_name = 'Me'
        else:
            sender_name = action_request.get_contact_name(self.msg.sender_num)
        return [('bolditalic', sender_name)]

    def _get_quote_markup(self):
        quote = get_envelope_quote(self.msg.envelope)
        if not quote:
            return None
        try:
            quote_author_num = quote['author']
            quote_text = textwrap.shorten(quote['text'], 70)
            quote_attachments = quote['attachments']
        except KeyError:
            logger.error("Failed to extract a quote from %s", self.msg.envelope)
            return None

        if cfg.show_inline:
            ret = ['|> ']
            sep = ' '
        elif self.align == 'left':
            ret = ['| ']
            sep = self.MARKUP_ELS_SEPARATOR + '| '
        else:
            ret = []
            sep = ' |' + self.MARKUP_ELS_SEPARATOR

        text_markup = self._get_text_markup_generic(
                text=quote_text,
                mentions=quote.get('mentions'),
                ) or []
        for index, markup_element in enumerate(text_markup):
            try:
                text_markup[index] = markup_element.replace('\n', sep)
            except AttributeError:
                continue

        ret.append((
            'bolditalic',
            action_request.get_contact_name(quote_author_num),
            ))
        for m in (
                text_markup,
                self._get_attachments_markup_generic(quote_attachments),
                ):
            if m:
                ret.append(sep)
                ret.extend((
                    ('italic', el) for el in m
                    ))
        if self.align == 'right' and not cfg.show_inline == 'columns' or cfg.show_inline == 'wrap':
            ret.append(' |')

        return ret

    def _get_call_message_markup(self):
        call_message = self.msg.envelope['callMessage']
        if 'offerMessage' in call_message:
            return ['📞 ', ('italic', 'Incoming call')]
        elif 'answerMessage' in call_message:
            return [('italic', 'Calling'), ' 📞']
        elif get_nested(call_message, 'hangupMessage', 'type') == 'NORMAL':
            # For accepted calls, `type: "ACCEPTED"`
            return ['📞 ', ('italic', 'Hung up')]
        return None

    def _get_status_markup(self):
        return DeliveryStatus.MARKUP_MAP[self.msg.delivery_status]

    def update_status(self):
        status_markup_new = self._get_status_markup()
        self._status_w.set_text(status_markup_new)

    def reload_markup(self):
        msg_markup = self._get_message_markup()
        self._text_w.set_text(msg_markup or '')

    def highlight(self):
        self._w.set_attr_map(REVERSED_FOCUS_MAP)

    def unhighlight(self):
        self._w.set_attr_map({None: self._color})

    def _add_pile_row(self, row_w):
        o_w = self._w.original_widget
        if isinstance(o_w, urwid.Pile):
            o_w.contents.append((row_w, o_w.options()))
        else:
            self._w.original_widget = urwid.Pile([o_w, row_w])

    def _remove_pile_row(self, row_w):
        o_w = self._w.original_widget
        if not isinstance(o_w, urwid.Pile):
            return
        try:
            o_w.contents.remove((row_w, o_w.options()))
        except ValueError:
            return
        if len(o_w.contents) == 1:
            self._w.original_widget = o_w.contents[0][0]

    def _insert_column(self, pos, w, col_opts=('pack',)):
        if pos == 'last':
            pos = len(self._columns_w.contents)
        self._columns_w.contents.insert(pos, (w, self._columns_w.options(*col_opts)))

    def _remove_column(self, w=None, pos=None):
        cols = self._columns_w.contents
        if w is not None:
            for pos, (widget, _opts) in enumerate(cols):  # pylint: disable=redefined-argument-from-local
                if widget is w:
                    break
            else:
                raise ValueError(f"Column with widget {w} not found")
        del cols[pos]

    def update_reactions_w(self):
        try:
            reactions = self.msg.reactions
        except AttributeError:
            return
        emojis_markup = []
        for envelope in reactions.values():
            reaction = get_envelope_reaction(envelope)
            if not reaction.get('isRemove'):
                emojis_markup.append(reaction['emoji'])
        if not emojis_markup:
            self._remove_reactions_w()
            return
        try:
            self._reactions_w.update(emojis_markup)
        except AttributeError:
            self._add_reactions_w(emojis_markup)

    def _add_reactions_w(self, emojis_markup):
        self._reactions_w = MessageReactionsWidget(emojis_markup, self.align)
        if not cfg.show_inline:
            self._add_pile_row(self._reactions_w)
        else:
            pos = 'last' if self.align == 'left' else 0
            self._insert_column(pos, self._reactions_w, ('weight', 0.5))

    def _remove_reactions_w(self):
        if self._reactions_w is None:
            return
        call_method = self._remove_column if cfg.show_inline else self._remove_pile_row
        call_method(self._reactions_w)
        self._reactions_w = None


class MessageWidgetsCache:
    """Create and cache widgets for LazyEvalMessageListWalker"""

    def __init__(self):
        self._cache = {}
        #self._cache = weakref.WeakValueDictionary()
            # Using a weak reference dictionary would save memory, but at the cost of using cpu to (re)create MessageWidget objects after switching back and forth between the chats.

    def get(self, msg, _position=None):
        key = self._hash(msg)
        try:
            # Not using
            #   return self._cache.setdefault(key, MessageWidget(msg))
            # insted of this try..except, because it would (re)create a new MessageWidget(msg) obj every time, even if it's already in the cache.
            w = self._cache[key]
        except KeyError:
            w = MessageWidget(msg)
            self._cache[key] = w
        return w

    @staticmethod
    def _hash(msg):
        return hash((msg.sender_num, msg.timestamp))

    def on_delivery_status_changed(self, timestamp, _status):
        key = hash((cfg.username, timestamp))
        try:
            msg_w = self._cache[key]
        except KeyError:
            # This is not necessarily an error:
                # Happens when the msg's delivery status is set before the message widget is created. For instance, when status = sending, or before the chat is opened and the widgets for it are created.
            return
        msg_w.update_status()

    def adjust_timestamp(self, msg, timestamp_adj):
        """Save memory by purging entry with old timestamp from cache.

        Also, saves cpu by not re-creating new MessageWidgets.
        """
        # This method is not be needed if self._cache is a weakref dictionary.
        key = self._hash(msg)
        key_adj = hash((msg.sender_num, timestamp_adj))
        with suppress(KeyError):
            # Theoretically, it's possible to get a race condition here if signal-cli returns adjusted timestamp before the msg with un-adjusted timestamp is added to the _cache.
            self._cache[key_adj] = self._cache.pop(key)


class LazyEvalMessageListWalker(LazyEvalListWalker):
    def __init__(self, contents, init_focus_pos=-1):
        self.msg_ws_cache = MessageWidgetsCache()
        super().__init__(contents, self.msg_ws_cache.get, init_focus_pos)


class ChatView(ListBoxPlus):

    signals = ['pick_reaction']

    def __init__(self):
        lw = LazyEvalMessageListWalker(urwid.MonitoredList())
        super().__init__(lw)

    def _update_search_results(self, txt, old_txt=''):
        if not txt:
            return
        scope = self.contents if old_txt in txt else None
            # Incremental search: only search through the current search results, rather then the whole chat.
        def test_match(msg):
            if not msg.text:
                return None
            return txt.casefold() in msg.text.casefold()
        self.filter_contents(test_match, scope)
        self.try_set_focus(-1)

    def _reset_search(self, keep_curr_focused=False):
        """Restore the pre-search contents.

        If keep_curr_focused is false, the focus is restored to the widget that was in focus before the search was started.
        Otherwise, place the focus on the same message that was in focus before the search is removed.
        """
        curr_focused_msg_w = self.focus
        self.filter_contents(None)
        if keep_curr_focused:
            focus_position = self.contents.index(curr_focused_msg_w.msg)
            self.try_set_focus(focus_position)

    def on_input_line_change(self, input_line_w, old_text):
        txt = input_line_w.get_edit_text()
        if txt.startswith(KEY_BINDINGS.search_sym):
            self._update_search_results(txt[1:], old_text[1:])
        elif self.is_filter_on:
            self._reset_search()

    def _delete_message(self, message_widget):
        index = self.focus_position if not self.is_filter_on else None
        action_request.delete_message_prompt(message_widget, index)
        if self.is_filter_on:
            del self.contents[self.focus_position]

    def _resend_message(self, msg):
        focus_position = self.focus_position  # Saving it because it'll shift after resend_message().
        index = focus_position if not self.is_filter_on else None
        try:
            action_request.resend_message(msg, index)
        except TypeError:
            return
        if self.is_filter_on:
            del self.contents[focus_position]
            self.contents.append(self._contents_pre_filter[-1])
                # The `_contents_pre_filter` for this class always points to the `current_chat` list. So after `resend()` action, its last element is the new message.
            self.try_set_focus(-1)

    def _focus_quoted_msg_w(self, envelope):
        quote = get_envelope_quote(envelope)
        if quote is None:
            return
        try:
            quoted_msg_index = self.contents.index_ts(
                    quote['id'],    # timestamp of orig message
                    quote['author'],
                    )
        except (KeyError, ValueError):
            return
        self.try_set_focus(quoted_msg_index)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        message_widget = self.focus
        if message_widget is None:
            return key
        envelope = message_widget.msg.envelope

        if key in KEY_BINDINGS['select_message']:
            if self.is_filter_on:
                self._reset_search(keep_curr_focused=True)
            elif get_envelope_msg(envelope) is not None:
                ret = action_request.open_attach(envelope) or action_request.open_urls(envelope)
                if not ret and get_envelope_quote(envelope):
                    self._focus_quoted_msg_w(envelope)
        elif key in KEY_BINDINGS['open_link_attach']:
            _ = action_request.open_urls(envelope) or action_request.open_attach(envelope)
        elif key in KEY_BINDINGS['copy_contents']:
            txt = get_envelope_msg(envelope)
            if not txt:
                attachments = get_envelope_attachments(envelope)
                if attachments is not None:
                    txt = ' '.join(get_attachment_path(attach) for attach in attachments)
            action_request.copy_to_clipb(txt)
        elif key in KEY_BINDINGS['delete_msg_local']:
            self._delete_message(message_widget)
        elif key in KEY_BINDINGS['delete_msg_remote']:
            action_request.send_remote_delete_prompt(message_widget)
        elif key in KEY_BINDINGS['resend_message'] and is_envelope_outgoing(envelope):
            self._resend_message(message_widget.msg)
        #elif key in KEY_BINDINGS['quote_reply'] and not message_widget.msg.not_repliable:
            ## Replying / quoting not supported by signal-cli
            ## https://github.com/AsamK/signal-cli/issues/213
            #pass
        elif key in KEY_BINDINGS['reaction_emoji_picker'] and not message_widget.msg.not_repliable:
            urwid.emit_signal(self, 'pick_reaction', size, self.calculate_visible(size, True))
        else:
            return key
        return None


class ChatWindow(urwid.Frame):
    def __init__(self):
        self._title_widget = urwid.Text('', align='center')
        self.input_line_w = InputLine()
        self.chat_view = ChatView()
        title_w_div = urwid.Pile([self._title_widget, urwid.Divider(HORIZ_LINE_DIV_SYM)])
        input_w_div = urwid.Pile([urwid.Divider(HORIZ_LINE_DIV_SYM), self.input_line_w])
        self._focusable_widgets = {'chat': 'body', 'input': 'footer'}
        super().__init__(self.chat_view, header=title_w_div, footer=input_w_div)
        urwid.connect_signal(self.input_line_w, 'postchange', self.chat_view.on_input_line_change)

    @property
    def focus_widget_name(self):
        for widget_name, focus_pos in self._focusable_widgets.items():
            if focus_pos == self.focus_position:
                return widget_name
        return None

    @focus_widget_name.setter
    def focus_widget_name(self, widget_name):
        self.focus_position = self._focusable_widgets[widget_name]

    def set_title(self, contact):
        name = contact.name_or_id
        markup = [('bold', name)]
        if not contact.is_group:
            num = contact.number
            if name != num:
                markup.extend([' (', num, ')'])
        else:
            memb_names = [memb.name_or_id for memb in contact.member_contacts]
            markup.append(' (')
            markup.append(textwrap.shorten(', '.join(memb_names), 80))
            markup.append(', ' if memb_names else 'only: ')
            markup.extend([('italic', 'You'), ')'])
        self._title_widget.set_text(markup)

    def on_contact_selected(self, contact):
        self.set_title(contact)
        self.chat_view.try_set_focus(-1)

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if not self.input_line_w.edit_text.startswith(KEY_BINDINGS.search_sym):
            return key
        if key in KEY_BINDINGS['clear']:
            return self.input_line_w.keypress(size, key)
        if key in KEY_BINDINGS['enter'] and self.focus_widget_name == 'input':
            if not self.chat_view.is_filter_on:
                # This clause is used for re-doing a search on a new chat contents after swtiching to a new contact.
                urwid.emit_signal(self.input_line_w, 'postchange', self.input_line_w, KEY_BINDINGS.search_sym)
            if self.chat_view.contents:
                self.focus_widget_name = 'chat'
            return None
        else:
            return key
        return None


# #############################################################################
# MainWindow
# #############################################################################


class StatusLine(urwid.WidgetWrap):
    def __init__(self, unread_count=0):
        self._text = urwid.Text('')
        self._unreads_widget = urwid.Text([
            "Unread messages count: ",
            ('bold', f"{unread_count}"),
            ])
        self._status_cols = urwid.Columns([self._text, ('pack', self._unreads_widget)], dividechars=1)
        self._prompt = None
        self._prompt_response_callback = None
        placeholder = urwid.WidgetPlaceholder(self._status_cols)
        super().__init__(placeholder)

    def set_text(self, new_text, append=False):
        if append:
            curr_markup = get_text_markup(self._text)
            if curr_markup:
                new_text = [curr_markup, '\n', new_text]    # urwid.Text does not mind nested lists
        self._text.set_text(new_text)

    def set_unread_count(self, count):
        txt = str(count) if count else str()
        self._unreads_widget.set_text(('bold', txt))

    def show_prompt(self, text, callback):
        self._prompt_response_callback = callback
        self._prompt = urwid.Edit(caption=text)
        self._w.original_widget = self._prompt

    def keypress(self, size, key):
        # Keypresses are passed to this widget only when it has focus, which only happens when the prompt is on.
        key = super().keypress(size, key)
        if key in KEY_BINDINGS['enter']:
            self._prompt_response_callback(self._prompt.edit_text)
        elif key in KEY_BINDINGS['clear']:
            self._prompt_response_callback(None)
        else:
            return key
        self._w.original_widget = self._status_cols
        return None


class MessageInfo(ListBoxPlus):

    class OpenPath(FocusableText):
        """Open-able text: file or URL"""

        def __init__(self, text, *args, fpath=None, **kwargs):
            super().__init__(text, *args, **kwargs)
            self.fpath = fpath

        def get_path(self):
            return self.fpath or self.text

        def open_path(self):
            if self.fpath:
                return action_request.open_file(self.fpath)
            return action_request.open_url(self.text)

    def __init__(self, msg):
        self._msg = msg

        name_w = self._prop_val_w(
                'Sender',
                action_request.get_contact_name(msg.sender_num),
                )
        num_w = self._prop_val_w('Number', msg.sender_num)
        date = strftimestamp(msg.timestamp)
        date_w = self._prop_val_w('Date', date)
        items = [name_w, num_w, date_w]

        if msg.local_timestamp is not msg.timestamp:
            received_timestamp = strftimestamp(msg.local_timestamp)
            items.append(
                    self._prop_val_w('Received', received_timestamp)
                    )

        if msg.text:
            txt_w = self._prop_val_w('Message', msg.text)
            items.append(txt_w)

        delivery_status_w = self._get_delivery_status_w()
        if delivery_status_w:
            items.append(delivery_status_w)

        items.append(urwid.Divider())

        if msg.text:
            urls = get_urls(msg.text)
            if urls:
                items.extend(self._get_urls_ws(urls))

        if msg.attachments:
            items.extend(self._get_attachments_ws(msg.attachments))

        sticker = get_envelope_sticker(msg.envelope)
        if sticker:
            items.append(self._get_sticker_w(sticker))

        reactions = getattr(msg, 'reactions', None)
        if reactions is not None:
            items.extend(self._get_reactions_ws(reactions))

        if 'debug' in cfg.log_level:
            items.extend(self._get_debug_info())

        super().__init__(items)

    @staticmethod
    def _prop_val_w(prop_name, prop_val):
        padding_width = 8
        prop_name_str = prop_name.ljust(padding_width) + ': '
        return FocusableText([
            ('bold', prop_name_str),
            prop_val
            ])

    def _get_delivery_status_w(self):
        status_detailed = self._msg.delivery_status_detailed
        status_str = status_detailed.str
        if not status_str:
            return None
        when_str = strftimestamp(status_detailed.when, strformat='%H:%M:%S %Y-%m-%d')
        status_when = f' ({when_str})' if status_detailed.when else ''
        return self._prop_val_w('Status', status_str + status_when)

    def _get_urls_ws(self, urls):
        header_w = urwid.Text([('bold', 'Links')], align='center')
        ret = [header_w]
        for url in urls:
            url_w = self.OpenPath(url)
            ret.append(url_w)
        return ret

    def _get_attachments_ws(self, attachments):
        header_w = urwid.Text(('bold', 'Attachments'), align='center')
        ret = [header_w]
        for atch in attachments:
            atch_w = self.OpenPath(
                    text=get_attachment_name(atch),
                    fpath=get_attachment_path(atch)
                    )
            ret.append(atch_w)
        return ret

    def _get_sticker_w(self, sticker):
        file_path = get_sticker_file_path(sticker)
        sticker_w = self.OpenPath(
                text=get_text_markup(self._prop_val_w('Sticker', file_path)),
                fpath=file_path,
                )
        return sticker_w

    @staticmethod
    def _get_reactions_ws(reactions):
        heading_w = urwid.Text([('bold', 'Reactions')], align='center')
        ret = [heading_w]
        for sender_num, envelope in reactions.items():
            sender_name = action_request.get_contact_name(sender_num)
            reaction = get_envelope_reaction(envelope)
            if reaction.get('isRemove'):
                continue
            ret.append(FocusableText([
                sender_name,
                ': ',
                reaction['emoji'],
                ' (',
                strftimestamp(get_envelope_time(envelope)),
                ')',
                ]))
        if ret == [heading_w]:
            return []
        return ret

    def _get_debug_info(self):
        ret = [
                urwid.Divider(),
                urwid.Text(('bold', 'Debug info'), align='center'),
                urwid.Text('Envelope', align='center'),
                FocusableText(pprint.pformat(self._msg.envelope, width=-1)),
                ]
        return ret

    def keypress(self, size, key):
        key = super().keypress(size, key)
        item = self.body[self.focus_position]
        if key in KEY_BINDINGS['copy_contents']:
            try:
                action_request.copy_to_clipb(item.get_path())
            except AttributeError:
                markup = get_text_markup(item)
                if len(markup) == 2 and isinstance(markup[0], tuple):
                    # The line is `Property : Value` type
                    action_request.copy_to_clipb(markup[1])
                else:
                    action_request.copy_to_clipb(item.text)
        elif key in KEY_BINDINGS['enter'] | KEY_BINDINGS['open_link_attach']:
            with suppress(AttributeError):
                item.open_path()
        else:
            return key
        return None


class HelpDialog(urwid.WidgetWrap):
    def __init__(self):
        _close_keys = " or ".join((
                k.capitalize() for k in KEY_BINDINGS['close_popup'].keys
                ))
        items = [urwid.Text(
                "Use "
                "↑ and ↓ to navigate, "
                "Enter to show, "
                f"{_close_keys} to exit.",
                align="center")]
        buttons = [
                ButtonBox(
                    label, on_press,
                    ) for label, on_press in (
            ("Key bindings",
                lambda _: action_request.open_in_pager(
                    KEY_BINDINGS.help_format(
                        action_request.get_terminal_size()[0]
                        )
                    )
                ),
            (":Commands",
                lambda _: action_request.open_in_pager_commands_help()
                ),
            ("README",
                lambda _: action_request.open_file_in_pager(SCLI_README_FILE)
                ),
            )]
        items += (urwid.Padding(
                    btn,
                    align='center',
                    width="clip",
                    ) for btn in buttons)
        items = list(intersperse(
                urwid.Divider(),
                items,
                ))
        w = ListBoxPlus(items)
        super().__init__(w)


class ReactionPicker(urwid.WidgetWrap, ViBindingsMixin):
    signals = ['closed']

    _emoji_regex = re.compile(
            r'[^\w\s!"#$%&\'()*+,-./:;<=>?@[\]\\^_`{|}~]'  # from `string.punctuation`
            )
            # Currently doing a simple test that the input text is a single non-word or punctuation char.

    class EditEmoji(urwid.Edit):
        signals = ['return']

        def keypress(self, size, key):
            if key in {'j', 'k'} | KEY_BINDINGS['close_popup'].keys:
                return key
            elif key in KEY_BINDINGS['enter']:
                urwid.emit_signal(self, 'return', self, self.edit_text)
                return None
            return super().keypress(size, key)

    def __init__(self, msg):
        self._msg = msg
        grid_items = [
                ButtonBox(
                    label=emoji,
                    on_press=self._reaction_picked,
                    user_data=emoji,
                    decoration=None,
                    ) for emoji in (
                    '💗',
                    #'❤️', # shows up displaced to the right
                    '👍',
                    '👎',
                    '😂',
                    '😮',
                    '😥',
                    )
                ]
        custom_emoji_input_w = self.EditEmoji(wrap='clip')
        self._custom_emoji_text_w = urwid.Text('>', align='right')
        grid_items.append(self._custom_emoji_text_w)
        grid_items.append(custom_emoji_input_w)
        grid_w = urwid.GridFlow(
                grid_items,
                cell_width=4,
                h_sep=1,
                v_sep=1,
                align='center',
                )
        fill_w = urwid.Filler(grid_w)
        urwid.connect_signal(
                custom_emoji_input_w,
                'return',
                self._reaction_picked
                )
        super().__init__(fill_w)

    def _is_single_emoji(self, text):
        return self._emoji_regex.fullmatch(text)
        # A check to reject sending non-emoji reactions.
            # They are delivered successfully, but are not displayed by the official clients (see signal-cli#834).
            # The current simple regex test is meant to notify a user of accidentally entered non-emoji text. It does not catch all possible strings that are not displayed on official clients. It also rejects some emoji that normally *would* be displayed, e.g. combined ones, like 👨‍👩‍👦‍👦 that report len!=1.

    def _reaction_picked(self, _widget, emoji):
        if self._is_single_emoji(emoji) or not emoji:
            self._emit('closed')
            action_request.send_reaction(self._msg, emoji)
        else:
            self._custom_emoji_text_w.set_text('❌👉')


class PopUpPlaceholder(urwid.WidgetPlaceholder):
    def __init__(self, w):
        super().__init__(w)
        self._orig_w = w
            # Urwid's terminology here might be confusing: "WidgetPlaceholder.original_widget" means "currently displayed widget", not the one it is originally initialized with.
        self._help_w = HelpDialog()

    def _show_pop_up(
            self,
            widget,
            title='',
            buttons=True,
            shadow_len=0,
            remove_callback=None,
            **overlay_params
            ):
        pop_up_box = PopUpBox(widget, title, buttons, shadow_len)
        urwid.connect_signal(
                pop_up_box,
                'closed',
                self._remove_pop_up,
                user_args=[remove_callback]
                )
        overlay_args = {
                'align': 'center',
                'valign': 'middle',
                'width': ('relative', 85),
                'height': ('relative', 65),
                }
        overlay_args.update(overlay_params)

        pop_up_overlay = urwid.Overlay(
            pop_up_box,
            self._orig_w,
            **overlay_args,
        )
        self.original_widget = pop_up_overlay

    def _remove_pop_up(self, remove_callback, *_sender_ws):
        if remove_callback is not None:
            remove_callback()
        self.original_widget = self._orig_w

    @property
    def _is_popup_shown(self):
        return self.original_widget is not self._orig_w

    def show_help(self):
        self._show_pop_up(
                self._help_w,
                title='Help',
                buttons=False,
                shadow_len=1,
                width=43,
                height=11,
                )

    def show_message_info(self, message_widget):
        message_widget.highlight()
        info = MessageInfo(message_widget.msg)
        pad = urwid.Padding(
                info,
                align='right',
                width=('relative', 88),
                right=4,
                )
        fill = urwid.Filler(pad, height=('relative', 100), top=1, bottom=1)
        self._show_pop_up(
                fill,
                title='Message info',
                shadow_len=1,
                remove_callback=message_widget.unhighlight,
                height=12,
                )

    def show_reaction_picker(self, frame_top_bottom_method, size, visible):
        input_w_rows = frame_top_bottom_method(size, focus=True)[0][1]
        focus_offset = visible[0][0]
        bottom_offset = input_w_rows + (size[1] - focus_offset) + 1
        msg_widget = visible[0][1]
        msg_widget.highlight()
        self._show_pop_up(
                ReactionPicker(msg_widget.msg),
                buttons=False,
                shadow_len=0,
                remove_callback=msg_widget.unhighlight,
                align=msg_widget.align,
                left=2,
                right=2,
                valign='bottom',
                bottom = bottom_offset,
                width=20,
                height=7,
                )

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if self._is_popup_shown:
            # When popup is shown, do not pass keys to other widgets until it's closed
            return None
        return key


class MainWindow(urwid.WidgetWrap):
    def __init__(self, contacts, chats_data):
        self._chats_data = chats_data
        self.contacts_w = ContactsWindow(contacts, self._chats_data)
        self._paste_mode = False

        self.chat_w = ChatWindow()
        contacts_box = LineBoxHighlight(self.contacts_w)
        self._chat_win_box = LineBoxHighlight(self.chat_w)
        self._popup_ph = PopUpPlaceholder(self._chat_win_box)
        cols = [('weight', 1, contacts_box), ('weight', 3, self._popup_ph)]
        self._columns = urwid.Columns(cols)
        self._contacts_column = self._columns.contents[0]

        total_unread_count = self._chats_data.unread_counts.total
        self.status_line = StatusLine(total_unread_count)

        urwid.connect_signal(
                self.chat_w.chat_view,
                'pick_reaction',
                self._popup_ph.show_reaction_picker,
                user_args=[self.chat_w.frame_top_bottom]
                )

        w = urwid.Frame(self._columns, footer=self.status_line)
        super().__init__(w)

    @property
    def contacts_hidden(self):
        return self._contacts_column not in self._columns.contents

    @contacts_hidden.setter
    def contacts_hidden(self, yes_no):
        if yes_no and not self.contacts_hidden:
            self._columns.contents.remove(self._contacts_column)
        elif not yes_no and self.contacts_hidden:
            self._columns.contents.insert(0, self._contacts_column)

    @property
    def focus_widget_name(self):
        if self.contacts_hidden or self._columns.focus_position == 1:
            return self.chat_w.focus_widget_name
        return 'contacts'

    @focus_widget_name.setter
    def focus_widget_name(self, widget_name):
        if widget_name == 'contacts':
            self.contacts_hidden = False
            self._columns.focus_position = 0
        else:
            if cfg.contacts_autohide and not self.contacts_hidden:
                self.contacts_hidden = True
            self._columns.focus_position = 0 if self.contacts_hidden else 1
            self.chat_w.focus_widget_name = widget_name

    def _focus_next(self, reverse=False):
        wnames = ['contacts', 'chat', 'input']
        curr_wname = self.focus_widget_name
        if not self.chat_w.chat_view.contents and not curr_wname == 'chat':
            # If there are no messages in current chat (either because no chat selected, or searching has filtered out all results), don't focus it.
            wnames.remove('chat')
        curr_focus_pos = wnames.index(curr_wname)
        incr = -1 if reverse else 1
        next_wname = wnames[(curr_focus_pos + incr) % len(wnames)]
        self.focus_widget_name = next_wname

    def update_unread_count(self, contact_id):
        self.contacts_w.contacts_list_w.update_contact_unread_count(contact_id)
        self.status_line.set_unread_count(self._chats_data.unread_counts.total)

    def on_contact_selected(self, contact, focus_widget):
        self.status_line.set_text('')
            # NOTE: for now not checking what's currently in the status line, just remove whatever text was there.
        self.update_unread_count(contact.id)
        self.chat_w.on_contact_selected(contact)
        if focus_widget:
            self.focus_widget_name = focus_widget

    def prompt_on_status_line(self, text, callback, callback_finally=None):
        def callback_wrapper(prompt_response):
            if prompt_response is not None:
                callback(prompt_response)
            if callback_finally is not None:
                callback_finally()
            self._w.focus_position = 'body'
        self.status_line.show_prompt(text, callback_wrapper)
        self._w.focus_position = 'footer'

    def prompt_on_status_line_yn(self, text, callback, default_response='y', callback_finally=None):
        text += " [y/n]: ".replace(
                default_response.lower(),
                default_response.upper()
                )
        def callback_wrapper(prompt_response):
            if not prompt_response:
                prompt_response = default_response
            if prompt_response.lower() in ('y', 'yes'):
                callback()
        self.prompt_on_status_line(text, callback_wrapper, callback_finally)

    def show_help(self):
        self._popup_ph.show_help()
        self.focus_widget_name = 'chat'

    def keypress(self, size, key):
        if self._paste_mode:
            if key == 'end paste':
                self._paste_mode = False
                return None
            return key
        if key in KEY_BINDINGS['clear']:
            action_request.set_status_line('')
        key = super().keypress(size, key)
        if key == 'begin paste':
            self._paste_mode = True
        if key in KEY_BINDINGS['focus_next_area']:
            self._focus_next()
        elif key in KEY_BINDINGS['focus_prev_area']:
            self._focus_next(reverse=True)
        elif key in KEY_BINDINGS['cmd_entry']:
            self.focus_widget_name = 'input'
            self.keypress(size, key)
        elif key in KEY_BINDINGS['search_input'] and self.focus_widget_name == 'chat':
            self.focus_widget_name = 'input'
            self.keypress(size, key)
        elif key in KEY_BINDINGS['open_next_chat']:
            self.contacts_w.contacts_list_w.select_next_contact()
        elif key in KEY_BINDINGS['open_prev_chat']:
            self.contacts_w.contacts_list_w.select_next_contact(reverse=True)
        elif key in KEY_BINDINGS['show_key_bindings']:
            action_request.open_in_pager(KEY_BINDINGS.help_format(
                action_request.get_terminal_size()[0]
                ))
        elif key in KEY_BINDINGS['show_help']:
            self.show_help()
        elif key in KEY_BINDINGS['show_message_info']:
            if self.focus_widget_name != 'chat':
                return key
            message_widget = self.chat_w.chat_view.focus
            if message_widget is None:
                return key
            self._popup_ph.show_message_info(message_widget)
        else:
            return key
        return None


class UrwidUI:
    def __init__(self, contacts, chats_data):
        self.main_w = MainWindow(contacts, chats_data)
            # FYI: to later get the topmost widget, can also use `urwid_main_loop.widget`
        self.loop = urwid.MainLoop(self.main_w, palette=PALETTE)
        if cfg.color and cfg.color.high_color_mode:
            self.loop.screen.set_terminal_properties(256)
        MessageWidget.set_formatting_consants(cfg.use_formatting)

        # Shortcuts for deeply nested attributes
        self.contacts = self.main_w.contacts_w.contacts_list_w
        self.chat = self.main_w.chat_w.chat_view
        self.input = self.main_w.chat_w.input_line_w
        self.msg_ws_cache = self.chat.body.msg_ws_cache


# #############################################################################
# commands
# #############################################################################


class Commands:
    def __init__(self, actions):
        self._actions = actions
        self.cmd_mapping = [
            (
                ['attach', 'a'],
                self._actions.attach,
                "Send file attachment `:attach FILE [FILE…] [MESSAGE]`",
                ),
            (
                ['edit', 'e'],
                self._actions.external_edit,
                "Edit message in external $EDITOR `:edit [FILENAME] [MESSAGE]`",
                ),
            (
                ['read', 'r'],
                self._actions.read,
                "Send message with the contents of FILE or output of COMMAND `:read FILE` | `:read !COMMAND`",
                ),
            (
                ['attachClip', 'c'],
                self._actions.attach_clip,
                "Send files in clipboard as message attachments",
                ),
            (
                ['openAttach', 'o'],
                self._actions.open_last_attach,
                "Open attachment from the latest message with an attach in the current chat",
                ),
            (
                ['openUrl', 'u'],
                self._actions.open_last_url,
                "Open URL from the latest message in the current chat with a URL in message text",
                ),
            (
                ['toggleNotifications', 'n'],
                self._actions.toggle_notifications,
                "Toggle desktop notifications on or off",
                ),
            (
                ['toggleAutohide', 'h'],
                self._actions.toggle_autohide,
                "Hide contacts pane when not in focus",
                ),
            (
                ['toggleContactsSort', 's'],
                self._actions.toggle_sort_contacts,
                "Switch between sorting contacts alphabetically or by the most recent message",
                ),
            (
                ['reload'],
                self._actions.reload,
                "Manual refresh of contacts data",
                ),
            (
                ['renameContact'],
                self._actions.rename_contact,
                "Rename a contact `:renameContact [ID] NEW_NAME`",
                ),
            (
                ['addContact'],
                self._actions.add_contact,
                "Add a new contact `:addContact NUMBER [NAME]`",
                ),
            (
                ['help'],
                self._actions.show_help,
                "Show help `:help [keys|commands]`",
                ),
            (
                ['quit', 'q'],
                self._actions.quit,
                "Exit the program",
                ),
        ]
        self._map = {cmd.lower(): fn for cmds, fn, _help in self.cmd_mapping for cmd in cmds}

    def exec(self, cmd, *args):
        fn = self._map.get(cmd.lower())
        if fn is None:
            self._actions.set_status_line(f"Command `{cmd}` not found")
            return None
        if not self._actions.check_cmd_for_current_contact(fn):
            self._actions.set_status_line(f":{cmd} Error: no contact currently selected")
            return None
        try:
            return fn(*args)
        except TypeError as err:
            # Handle only the exceptions produced by giving the wrong number of arguments to `fn()`, not any exceptions produced inside executing `fn()` (i.e. deeper in the stack trace)
            if err.__traceback__.tb_next is not None:
                raise
            if re.search(r"missing \d+ required positional argument", str(err)):
                self._actions.set_status_line(f':{cmd} missing arguments')
                return None
            elif re.search(r"takes \d+ positional arguments? but \d+ (was|were) given", str(err)):
                self._actions.set_status_line(f':{cmd} extra arguments')
                return None
            else:
                raise

    def help_format(self, term_ncols=None):
        cmd_col_width = 30
        if term_ncols is None:
            return '\n'.join((
                f"{', '.join((f':{cmd}' for cmd in cmd_list)):{cmd_col_width}}"
                f"{help_str}"
                for cmd_list, _fn, help_str in self.cmd_mapping
                ))
        return '\n'.join((
            str(urwid.Columns([
                (
                    cmd_col_width,
                    urwid.Text(
                        ', '.join((f':{cmd}' for cmd in cmd_list))
                        ),
                    ),
                urwid.Text(help_str),
                ]).render((term_ncols, )))
            for cmd_list, _fn, help_str in self.cmd_mapping
            ))


class Actions:
    def __init__(self, daemon, contacts, chats_data, urwid_ui):
        self._daemon = daemon
        self._contacts = contacts
        self._chats_data = chats_data
        self._urwid_ui = urwid_ui
        self._clip = Clip()

    def reload(self, callback=None, **callback_kwargs):
        if self._daemon.is_dbus_service_running:
            self.update_contacts_async(callback, **callback_kwargs)
        else:
            self.set_status_line("reload error: signal-cli daemon is not running")

    def _update_contacts_ui(self):
        self._urwid_ui.contacts.update()
        # Updating the title text in chat widget:
        try:
            current_contact = self._contacts.map[self._chats_data.current_contact.id]
                # Need to re-obtain the contact object, since the one in _chats_data now points to an outdated object
        except (AttributeError, KeyError):
            return
        self._urwid_ui.main_w.chat_w.set_title(current_contact)

    def set_status_line(self, text, append=False):
        self._urwid_ui.main_w.status_line.set_text(text, append)

    def proc_run(self, *args, **kwargs):
        """Wrapper that logs and swallows the exceptions"""
        try:
            return proc_run(*args, **kwargs)
        except (OSError, ValueError) as err:
            logger.exception(err)
            self.set_status_line(
                    '\n'.join([
                        str(err),
                        'Full error traceback written to log.',
                        ])
                    )
            return None

    def send_desktop_notification(self, sender, message, avatar=None):
        if not cfg.enable_notifications:
            return
        if avatar is None:
            avatar = 'scli'
        rmap = {}
        for token, text in (('%s', sender), ('%m', message), ('%a', avatar)):
            text = text.replace(r"'", r"'\''")
            rmap[token] = text
        rmap['_optionals'] = ('%s', '%m', '%a')
        self.proc_run(cfg.notification_command, rmap, background=True)
        if not cfg.notification_no_bell:
            print('\a', end='')

    def send_message_curr_contact(self, message="", attachments=None):
        if self._chats_data.current_contact is None:
            return
        self._daemon.send_message(self._chats_data.current_contact.id, message, attachments)

    def external_edit(self, *args):
        if cfg.editor_command is None:
            self.set_status_line(":edit Error: no command for external editor set")
            return

        filename = ''
        if args:
            filename, message = partition_escaped(*args)

        if is_path(filename):
            msg_file_path = os.path.expanduser(filename)
        else:
            with tempfile.NamedTemporaryFile(
                    suffix='.md', delete=False
                    ) as temp_fo:
                msg_file_path = tmpfile = temp_fo.name
            message = args
        if message:
            with open(msg_file_path, "w", encoding="utf-8") as msg_file:
                msg_file.write(message)

        self._daemon.main_loop.stop()
        cmd = " ".join((cfg.editor_command, shlex.quote(msg_file_path)))
        self.proc_run(cmd)
        self._daemon.main_loop.start()

        with suppress(OSError), open(msg_file_path, 'r', encoding="utf-8") as msg_file:
            msg = msg_file.read().strip()
            if msg:
                self.send_message_curr_contact(msg)

        with suppress(NameError):
            os.remove(tmpfile)

    def read(self, path_or_cmd):
        message = ''
        if is_path(path_or_cmd):
            try:
                with open(os.path.expanduser(path_or_cmd), 'r', encoding="utf-8") as file:
                    message = file.read()
            except OSError as err:
                logger.exception(err)
                self.set_status_line(str(err))
        elif path_or_cmd.startswith('!'):
            proc = self.proc_run(
                    path_or_cmd[1:].strip(),
                    capture_output=True,
                    )
            if proc is not None:
                message = proc.stdout
        else:
            self.set_status_line(f"Error: could not read `{path_or_cmd}`")
        if message != '':
            self.send_message_curr_contact(message)

    def attach(self, string):
        files = []
        while string:
            file_path, string_rest = partition_escaped(string)
            if not is_path(file_path):
                break
            file_path = Path(file_path).expanduser()
            if not file_path.is_file():
                self.set_status_line(f"File does not exist: {file_path}")
                return
            files.append(str(file_path))
            string = string_rest
        if not files:
            self.set_status_line(f"No attachment files provided in {string!r}")
            return
        self.send_message_curr_contact(string, attachments=files)

    def attach_clip(self, *message):
        files = self._clip.files_list()
        if files:
            self.send_message_curr_contact(*message, attachments=files)
        else:
            self.set_status_line('No files in clipboard.')

    def copy_to_clipb(self, text):
        self._clip.put(text)

    def send_reaction(self, msg, emoji):
        is_remove = not emoji
        if is_remove:
            try:
                current_reaction_envelope = msg.reactions[self._contacts.sigdata.own_num]
                emoji = get_envelope_reaction(current_reaction_envelope)['emoji']
            except (AttributeError, KeyError):
                emoji = '👍'
                # Official signal clients do not remove emojis if emoji=="". It needs to be any (non-empty) emoji.
        self._daemon.send_reaction(
                msg.contact_id,
                emoji,
                msg.sender_num,
                msg.timestamp,
                is_remove,
                )

    def open_file(self, path):
        if not os.path.exists(path):
            logger.error("File does not exist: %s", path)
            self.set_status_line('File does not exist: ' + path)
            return None
        return self.proc_run(cfg.open_command, {'%u': path}, background=True)

    def open_attach(self, envelope):
        attachments = get_envelope_attachments(envelope)
        if attachments:
            for attachment in attachments:
                file_path = get_attachment_path(attachment)
                if file_path:
                    self.open_file(file_path)
            return attachments
        # Treating stickers as attachments with a different dir path
        sticker = get_envelope_sticker(envelope)
        if sticker:
            file_path = get_sticker_file_path(sticker)
            if file_path:
                self.open_file(file_path)
            return sticker
        return None

    def open_last_attach(self):
        for msg in reversed(self._chats_data.current_chat):
            if self.open_attach(msg.envelope):
                return

    def open_url(self, url):
        return self.proc_run(cfg.open_command, {'%u': url}, background=True)

    def open_urls(self, envelope):
        txt = get_envelope_msg(envelope)
        urls = get_urls(txt) if txt else []
        for url in urls:
            self.open_url(url)
        return urls

    def open_last_url(self):
        for txt in reversed(self._chats_data.current_chat):
            if self.open_urls(txt.envelope):
                return

    # pylint: disable=attribute-defined-outside-init
        # `Config` class uses __setattr__ that forwards to argparser's `args` instance.
    @staticmethod
    def toggle_autohide():
        cfg.contacts_autohide = not cfg.contacts_autohide

    def toggle_sort_contacts(self):
        cfg.contacts_sort_alpha = not cfg.contacts_sort_alpha
        self.reload()

    def toggle_notifications(self):
        cfg.enable_notifications = not cfg.enable_notifications
        notif = ''.join((
                'Desktop notifications are ',
                'ON' if cfg.enable_notifications else 'OFF',
                '.'
                ))
        self.set_status_line(notif)
    # pylint: enable=attribute-defined-outside-init

    def add_contact(self, args):
        """Add a new contact.

        The syntax is
        :addContact +NUMBER [Contact Name]
        """
        try:
            number, name = args.split(maxsplit=1)
        except ValueError:
            number, name = args, ""
        if not is_number(number):
            self.set_status_line(f':addContact "{number}": not a valid number')
            return
        self._daemon.rename_contact(number, name, is_group=False, callback=lambda *i: self.reload())

    def rename_contact(self, args):
        """Rename contact.

        :renameContact +NUMBER new name here  -> use +NUMBER number
        :renameContact "Old Name" new name here  -> use contact named "Old Name"
        :renameContact new name here          -> rename current contact or group
        """
        try:
            number, new_name = partition_escaped(args)
            if not is_number(number):
                for contact_id, contact in self._contacts.map.items():
                    if contact.name == number:
                        is_group = contact.is_group
                        break
                else:  # contact with name `number` not found
                    raise ValueError
            elif self._contacts.get_by_id(number) is None:
                self.set_status_line(f":renameContact Error: no contact with number {number} found")
                return
            else:
                is_group = False
                contact_id = number
        except ValueError:
            if self._chats_data.current_contact is None:
                self.set_status_line(":renameContact Error: no contact currently selected")
                return
            contact_id = self._chats_data.current_contact.id
            is_group = self._chats_data.current_contact.is_group
            new_name = args
        self._daemon.rename_contact(contact_id, new_name, is_group, lambda *i: self.reload())

    def _delete_message(self, msg, index=None):
        self._chats_data.chats.delete_message(msg, index)
        self._chats_data.delivery_status.delete(msg.timestamp)

    def delete_message_prompt(self, message_widget, index=None):
        message_widget.highlight()
        msg = message_widget.msg
        self._urwid_ui.main_w.prompt_on_status_line_yn(
                "Delete message from local history?",
                callback=lambda: self._delete_message(msg, index),
                callback_finally=message_widget.unhighlight,
                )

    def resend_message(self, msg, index=None):
        if msg.delivery_status != 'send_failed':
            # Only allow re-sending previously failed-to-send messages
            raise TypeError
        self._delete_message(msg, index)
        self.set_status_line('')    # remove 'send-failed' status line
        envelope = msg.envelope
        contact_id = get_envelope_contact_id(envelope)
        message = get_envelope_msg(envelope)
        attachments = get_envelope_attachments(envelope)
        self._daemon.send_message(contact_id, message, attachments)

    def send_remote_delete_prompt(self, message_widget):
        msg = message_widget.msg
        if msg.sender_num != self._contacts.sigdata.own_num:
            return
        message_widget.highlight()
        self._urwid_ui.main_w.prompt_on_status_line_yn(
                "Remote delete message?",
                callback=lambda: self._daemon.send_remote_delete(
                    msg.contact_id,
                    msg.timestamp,
                    ),
                callback_finally=message_widget.unhighlight,
                )

    def update_contacts_async(self, callback=None, **callback_kwargs):
        def on_contacts_updated():
            current_contact = self._chats_data.current_contact
            if current_contact is not None and current_contact.id not in self._contacts.map:
                self._chats_data.current_contact = None
            self._update_contacts_ui()
            self._chats_data.contacts_cache = self._contacts.serialize()
            self._daemon.unpause_message_processing()
            if callback is not None:
                callback(**callback_kwargs)
        self._daemon.pause_message_processing()
        self._update_indiv_contacts_async(
                self._update_groups_async,
                on_contacts_updated,
                )

    def _update_indiv_contacts_async(self, callback, *cb_args, **cb_kwargs):
        def on_indiv_contacts_updated(contacts_dict):
            self._contacts.update(contacts_dict, clear=True)
            callback(*cb_args, **cb_kwargs)
        self._daemon.get_indiv_contacts(
                callback=on_indiv_contacts_updated
                )

    def _update_groups_async(self, callback, *cb_args, **cb_kwargs):
        def on_got_groups_dicts(groups_dict):
            self._contacts.update(groups_dict)
            self._contacts.set_groups_membership()
            callback(*cb_args, **cb_kwargs)
        def on_got_groups_ids(groups_ids):
            self._daemon.populate_groups_dict(
                    groups_ids,
                    callback=on_got_groups_dicts,
                    )
        self._daemon.get_groups_ids(
                callback=on_got_groups_ids
                )

    def show_new_msg_notifications(self, msg):
        sender_name = self.get_contact_name(msg.sender_num)
        contact_avatar = self._get_contact_avatar(msg.contact_id)

        try:
            *_, reaction_envelope = msg.reactions.values()  # the latest reaction envelope
        except (AttributeError, ValueError):
            reaction_envelope = None
        else:
            sender_name = self.get_contact_name(
                    get_envelope_sender_id(reaction_envelope)
                    )
            contact_avatar = self._get_contact_avatar(
                    get_envelope_contact_id(reaction_envelope)
                    )

        def get_msg_notif():
            if reaction_envelope is not None:
                reaction = get_envelope_reaction(reaction_envelope)
                if reaction.get('isRemove'):
                    return None
                reaction_emoji = reaction['emoji']
                return (reaction_emoji,
                        ''.join((
                            'New reaction from ', repr(sender_name), ': ',
                            reaction_emoji,
                        )))

            msg_text = msg.text
            if msg_text:
                return (msg_text,
                        ''.join((
                            # NOTE: this could be a list (urwid.Text markup type), except for the textwrap.shorten below
                            'New message from ', repr(sender_name), ': ', repr(msg_text)
                        ))
                        )

            if msg.attachments:
                return('[attachments]',
                        ''.join((
                            'New attachments message from: ', repr(sender_name)
                            ))
                        )

            incoming_call = get_nested(msg.envelope, 'callMessage', 'offerMessage')
            if incoming_call:
                txt = '📞 Incoming call'
                return(txt,
                        ' '.join((
                            txt, 'from', repr(sender_name)
                            ))
                        )

            if get_envelope_sticker(msg.envelope):
                return('[sticker]',
                        ''.join((
                            'New sticker from: ', repr(sender_name)
                            ))
                        )

            return None

        try:
            msg_text, notif = get_msg_notif()
        except TypeError:
            return
        notif = textwrap.shorten(notif, 80)
        if reaction_envelope is None or cfg.notify_on_reactions:
            self.send_desktop_notification(sender_name, msg_text, contact_avatar)
        if (self._chats_data.current_contact is None
                or msg.contact_id != self._chats_data.current_contact.id):
            self.set_status_line(notif)

    def get_contact_name(self, contact_num):
        contact = self._contacts.get_by_id(contact_num)
        return contact.name_or_id if contact else contact_num

    def _get_contact_avatar(self, contact_id):
        contact = self._contacts.get_by_id(contact_id)
        return contact.avatar if contact else None

    def get_terminal_size(self):
        return self._urwid_ui.loop.screen.get_cols_rows()

    def check_cmd_for_current_contact(self, fn):
        return (
                self._chats_data.current_contact is not None
                or fn not in (
                    self.external_edit,
                    self.read,
                    self.attach,
                    self.attach_clip,
                    self.open_last_attach,
                    self.open_last_url,
                    )
                )

    def open_in_pager(self, contents):
        """Pause urwid's loop and open `contents` in the system pager"""
        # It's easy to implement a simple pager in urwid itself, but it's more convenient for the users to keep the configuration and key bindings they are familiar with from their system-wide pager program.
        env_vars = None
        if "LESS" in os.environ:
            # Modify $LESS to prevent `less` from exiting when the bottom of the output is rearched. Clear the screen before showing the output.
            env_vars = os.environ.copy()
            env_vars["LESS"] += " -+FX"
        self._daemon.main_loop.stop()
        self.proc_run(cfg.pager_command, input=contents, env=env_vars)
        self._daemon.main_loop.start()

    def open_file_in_pager(self, path):
        try:
            with open(path, encoding="utf-8") as fo:
                contents = fo.read()
        except OSError:
            contents = f"Error: could not open file: {path}"
        self.open_in_pager(contents)

    def open_in_pager_commands_help(self):
        self.open_in_pager(
                self._urwid_ui.input.cmds.help_format(
                    self.get_terminal_size()[0]
                    )
                )

    def show_help(self, *topic):
        try:
            topic = topic[0]
        except IndexError:
            self._urwid_ui.main_w.show_help()
            return
        if "keys".startswith(topic):
            self.open_in_pager(
                    KEY_BINDINGS.help_format(
                        self.get_terminal_size()[0]
                        )
                    )
        elif "commands".startswith(topic):
            self.open_in_pager_commands_help()
        else:
            self.set_status_line(f":help for {topic!r} not found")

    @staticmethod
    def quit():
        raise urwid.ExitMainLoop()


class ActionRequest:
    # The idea of having this class & its instance is to make a *globally accessible* function for all UI classes to call to request an action (e.g. setting status line text), without having to pass `Actions` instances down the class stack to every UI class that needs (or might need) it.
    # There might be a better OO way of doing this though.

    def __init__(self, actions=None):
        self._actions = actions

    def set_actions(self, actions):
        self._actions = actions

    def __getattr__(self, method):
        return getattr(self._actions, method)


action_request = ActionRequest()


# #############################################################################
# key bindings
# #############################################################################


class KeyBindings:

    class Binding():

        __slots__ = ("keys", "descr")

        def __init__(self, keys, descr):
            self.keys = keys
            self.descr = descr

        def __contains__(self, item):
            return item in self.keys

        def __or__(self, other):
            return self.keys.__or__(other.keys)

        def __repr__(self):
            return f"{self.__class__.__name__}(keys={self.keys}, descr={self.descr!r})"

        @property
        def a_key(self):
            """Return one (any) element from the set `self.keys`."""
            for key in self.keys:
                return key

    _MAP_DEFAULT = {
        # Format:
        # 'action_name': Binding(
            # {'key1', 'key2', …},
            # "help string"
            # )
        'show_key_bindings': Binding(
            {'?'},
            "Show current key bindings",
            ),
        'show_help': Binding(
            {'f1'},
            "Open help pop-up",
            ),
        'copy_contents': Binding(
            {'y'},
            "Copy message contents or details",
            ),
        'show_message_info': Binding(
            {'i'},
            "Show message details",
            ),
        'open_link_attach': Binding(
            {'o'},
            "Open attachment or URL in the message",
            ),
        'close_popup': Binding(
            {'q', 'esc'},
            "Close pop-up",
            ),
        'focus_next_area': Binding(
            {'tab'},
            "Switch focus to next element",
            ),
        'focus_prev_area': Binding(
            {'shift tab'},
            "Switch focus to previous element",
            ),
        'open_contact_chat': Binding(
            {'right', 'l'},
            "Show contact's conversation",
            ),
        'mark_unread': Binding(
            {'U'},
            "Mark conversation unread",
            ),
        'enter': Binding(
            {'enter'},
            "Open / submit",
            ),
        'clear': Binding(
            {'esc'},
            "Exit or remove notifications",
            ),
        'auto_complete': Binding(
            {'tab'},
            "Auto-complete input text",
            ),
        'search_input': Binding(
            {'/'},
            "Filter contents matching input",
            ),
        'input_newline': Binding(
            {'meta enter'},
            "Insert newline",
            ),
        'cmd_entry': Binding(
            {':'},
            "Command prompt",
            ),
        'open_next_chat': Binding(
            {'meta j', 'meta down'},
            "Open next chat",
            ),
        'open_prev_chat': Binding(
            {'meta k', 'meta up'},
            "Open previous chat",
            ),
        'readline_word_left': Binding(
            {'ctrl left'},
            "Move to the previous word in input",
            ),
        'readline_word_right': Binding(
            {'ctrl right'},
            "Move to the next word in input",
            ),
        'select_message': Binding(
            {'enter', 'right', 'l'},
            "Focus message or open its attachments",
            ),
        'delete_msg_local': Binding(
            {'d'},
            "Remove message from local history",
            ),
        'delete_msg_remote': Binding(
            {'D'},
            "Remove a message from a chat for all participants",
            ),
        'resend_message': Binding(
            {'r'},
            "Resend a failed-to-send message",
            ),
        #'quote_reply': Binding(
            #{'r'},
            #"Quote original message in a reply",
            #),
            ### Not implemented
        'reaction_emoji_picker': Binding(
            {'R', 'e'},
            "Send an emoji reaction for a message",
            ),
        }

    def __init__(self):
        self._map = self._MAP_DEFAULT
        self.cmd_sym = ':'
        self.search_sym = '/'

    def __getitem__(self, item):
        return self._map[item]

    def set(self, binds):
        if not binds:
            return
        sep = ':'
        keys_sep = ','
        for kb in binds:
            action, keys_str = kb.split(sep, 1)
            if not keys_str:
                sys.exit(f"Error parsing key bind {kb!r} - missing keys")
            keys = set(keys_str.split(keys_sep))
            if '' in keys:
                # Allow keys_sep itself be used as a key
                keys.remove('')
                keys.add(keys_sep)
            self._map[action].keys = keys
        self.cmd_sym = self._map['cmd_entry'].a_key
        self.search_sym = self._map['search_input'].a_key

    def help_format(self, term_ncols=None):
        key_col_width = 20
        act_col_width = 25
        key_sorted = sorted(self._map.items(), key=lambda t: [
                    k.lower() for k in t[1].keys
                    ])
        if term_ncols is None:
            ret = f"{'[KEY]':{key_col_width}} {'[ACTION]':{act_col_width}} [DESCRIPTION]\n"
            ret += '\n'.join((
                f"{', '.join(bind.keys):{key_col_width}} {action:{act_col_width}} {bind.descr}" for action, bind in key_sorted
                ))
        else:
            ret = str(urwid.Columns([
                (key_col_width, urwid.Text("[KEY]")),
                (act_col_width, urwid.Text("[ACTION]")),
                urwid.Text("[DESCRIPTION]"),
                ]).render((term_ncols, ))) + '\n'
            ret += '\n'.join((
                str(urwid.Columns([
                    (key_col_width, urwid.Text(', '.join(bind.keys))),
                    (act_col_width, urwid.Text(action)),
                    urwid.Text(bind.descr),
                    ]).render((term_ncols, )))
                for action, bind in key_sorted
                ))
        return ret


KEY_BINDINGS = KeyBindings()


# #############################################################################
# Coordinate
# #############################################################################


class Coordinate:
    def __init__(self):
        self._chats_data = ChatsData(cfg.save_history)
        sigdata = SignalData(cfg.username)
        self._contacts = Contacts(sigdata, self._chats_data.contacts_cache)
        self._ui = UrwidUI(self._contacts, self._chats_data)
        self.daemon = Daemon(self._ui.loop, cfg.username)
        self._actions = Actions(self.daemon, self._contacts, self._chats_data, self._ui)
        self._commands = Commands(self._actions)
        action_request.set_actions(self._actions)
        self._connect_methods()

    def _connect_methods(self):
        for cb_name in self.daemon.callbacks:
            self.daemon.callbacks[cb_name] = getattr(self, "_on_" + cb_name)
        urwid.connect_signal(self._ui.contacts, 'contact_selected', self._on_contact_selected)
        cfg.on_modified = self._on_cfg_changed
        self._chats_data.delivery_status.on_status_changed = self._ui.msg_ws_cache.on_delivery_status_changed
        Message.set_class_functions(
            get_delivery_status=self._chats_data.delivery_status.get_detailed,
            get_contact=self._contacts.get_by_id,
            )
        self._ui.main_w.chat_w.input_line_w.set_cmds(self._commands)
        self._chats_data.typing_indicators.set_alarm_in = self._ui.loop.set_alarm_in
        self._chats_data.typing_indicators.remove_alarm = self._ui.loop.remove_alarm

    def _on_sending_message(self, envelope):
        group_members = None
        if is_envelope_group_message(envelope):
            group_id = get_envelope_contact_id(envelope)
            group = self._contacts.get_by_id(group_id)
            if group is not None:
                # Can happen if `group` is absent from the `groupStore` (for whatever reason), and we get a sync-ed message sent to `group` from another device. See #126.
                group_members = group.members_ids
        self._chats_data.delivery_status.on_sending_message(envelope, group_members)
        msg = self._chats_data.chats.add_envelope(envelope)
        self._ui.chat.try_set_focus(-1)
        self._ui.contacts.on_new_message(msg)

    def _on_sending_reaction(self, envelope):
        self._chats_data.delivery_status.on_sending_message(envelope)
        self._add_reaction(envelope)
        self._ui.chat.try_set_focus(-1)
            # Ensuring the last message does not get pushed down out of the view by the new reaction row.
            # None of the following alternatives solve this:
                # self._ui.chat.set_focus_valign('bottom'), focus_position=<orig_focus>, set_focus(<orig_focus>, coming_from='below')

    def _on_sending_done(self, envelope, status='sent', timestamp_adj=None):
        self._chats_data.delivery_status.on_sending_done(envelope, status, timestamp_adj)
        self._ui.contacts.on_sending_done(envelope, status, timestamp_adj)

        try:
            chat, index = self._chats_data.chats.get_chat_index_for_envelope(envelope)
        except ValueError:
            return
        msg = chat[index]

        if status == 'send_failed':
            msg_txt = textwrap.shorten(msg.text, 20)
            self._actions.set_status_line(
                    f'Message "{msg_txt}" failed to send. '
                    'Press `r` on message to re-send.',
                    append=True,
                    )
            self._ui.chat.try_set_focus(-1)
            return

        if timestamp_adj is not None:
            self._ui.msg_ws_cache.adjust_timestamp(msg, timestamp_adj)
            chat.adjust_timestamp(msg, timestamp_adj, index)
            self._ui.chat.try_set_focus(-1)

        self._chats_data.delivery_status.process_buffered_receipts(msg.timestamp)

    def _on_sending_reaction_done(self, envelope, status='sent', timestamp_adj=None):
        if status == 'sent':
            status = 'ignore_receipts'
        self._chats_data.delivery_status.on_sending_done(envelope, status, timestamp_adj)
        self._ui.contacts.on_sending_done(envelope, status, timestamp_adj)

        if status != 'send_failed':
            return
        reaction = get_envelope_reaction(envelope)
        emoji = reaction['emoji']
        self._actions.set_status_line(
                f'Reaction "{emoji}" failed to send.',
                append=True,
                )
        reaction['isRemove'] = True
        self._add_reaction(envelope)
        # NOTE: When attempting to _replace_ an emoji with anoter one 'send_fail's, the old one will be removed (not shown) locally in scli, while continuing to be visible for the original recepients.

    def _process_msg_envelope(self, envelope):
        sender_num = get_envelope_sender_id(envelope)
        self._chats_data.typing_indicators.remove(sender_num)
        msg = self._chats_data.chats.add_envelope(envelope)
        self._on_new_message(msg)

    def _on_receive_message(self, envelope):
        logger.info('Message envelope: %s', pprint.pformat(envelope))
        contact_id = get_envelope_contact_id(envelope)
        if contact_id in self._contacts.map:
            self._process_msg_envelope(envelope)
        else:
            def after_contacts_reload(new_contact):
                if new_contact is not None:
                    self._process_msg_envelope(envelope)
            self._on_unknown_contact(envelope, callback=after_contacts_reload)

    def _on_unknown_contact(self, envelope, callback):
        logger.info("Message from unknown contact: %s", envelope)
        def after_contacts_loaded():
            contact_id = get_envelope_contact_id(envelope)
            contact = self._contacts.map.get(contact_id)
            if contact is not None:
                return callback(contact)

            logger.error("Message from unknown contact: %s", envelope)
            sender_info = (
                    envelope.get("sourceName")
                    or
                    f"UUID: {contact_id}"
                    )
            msg_text = get_envelope_msg(envelope)
            self._actions.set_status_line([
                'Message from an unknown chat: ',
                repr(sender_info),
                '\n',
                repr(msg_text),
                '\n',
                'Accept this contact on the primary device first.',
                ])
            self._actions.send_desktop_notification(sender_info, msg_text)

            contact_temp = Contact({
                "number": contact_id,
                "name": (
                    envelope.get("sourceName") or
                    envelope.get("sourceNumber") or
                    envelope.get("source")[:10] + "..."
                    ) + " [UNKNOWN CONTACT]"
                })
            self._contacts.map[contact_id] = contact_temp
            self._contacts.indivs.add(contact_temp)
            with suppress(KeyError, TypeError):
                envelope["dataMessage"]["message"] += "\n~~~\n[Message from an unknown contact. Accept on the primary device first]\n~~~"
            self._chats_data.chats.add_envelope(envelope)
            self._chats_data.unread_counts[contact_id] += 1
            self._ui.contacts.update()

            return callback(contact)
        self._actions.reload(callback=after_contacts_loaded)

    def _on_receive_sync_message(self, envelope):
        self._on_sending_message(envelope)
        self._on_sending_done(envelope)

    def _on_new_message(self, msg, increment_unread_count=True):
        self._ui.contacts.on_new_message(msg)
        contact_id = msg.contact_id
        if (self._chats_data.current_contact is not None
                and contact_id == self._chats_data.current_contact.id):
            self._ui.chat.try_set_focus(-1)
        elif increment_unread_count:
            self._chats_data.unread_counts[contact_id] += 1
            self._ui.main_w.update_unread_count(contact_id)
        self._actions.show_new_msg_notifications(msg)

    def _add_reaction(self, envelope):
        msg = self._chats_data.chats.add_reaction_envelope(envelope)
        if not msg:
            return None
        msg_w = self._ui.msg_ws_cache.get(msg)
        msg_w.update_reactions_w()
        return msg

    def _on_receive_receipt(self, envelope):
        self._chats_data.delivery_status.on_receive_receipt(envelope)

    def _on_receive_reaction(self, envelope):
        msg = self._add_reaction(envelope)
        if is_envelope_outgoing(envelope):
            # Do not show notificitions for sync messages from linked devices
            return
        if msg is not None:
            self._on_new_message(msg, increment_unread_count=cfg.notify_on_reactions)
            # Not focusing on the received reaction message (same behavior as signal-desktop)
        else:
            # Show a notification for reaction to an "unknown" message (not in Chats)
            reaction = get_envelope_reaction(envelope)
            msg = Message({'source': reaction['targetAuthor']})
            msg.add_reaction(envelope)
            self._actions.show_new_msg_notifications(msg)

    def _on_daemon_log(self, event, log_line):
        if event == "daemon_stopped":
            self._actions.set_status_line([
                "signal-cli daemon has stopped:\n   ",
                log_line,
                "\nRestart scli to restart the daemon."
                ])
        elif event == "another_instance_running":
            self._actions.set_status_line([
                "signal-cli: Config file is in use by another instance, waiting…\n",
                "Stop previously launched signal-cli processes to continue.",
                ])
        elif event == "another_instance_stopped":
            self._actions.set_status_line("Initializing signal-cli daemon... ")

    def _on_daemon_started(self):
        logger.info("signal-cli dbus service started")
        self._actions.set_status_line("Initializing signal-cli daemon... Done")
        def clear_status_line(*_args):
            self._actions.set_status_line("")
        self._ui.loop.set_alarm_in(2, clear_status_line)
        self._actions.update_contacts_async()
        self.daemon.get_signal_cli_version(callback=logger.info)

    def _on_contact_selected(self, contact, focus_widget):
        self._chats_data.current_contact = contact
        self._ui.chat.contents = self._chats_data.current_chat
        self._chats_data.unread_counts[contact.id] = 0
        self._ui.main_w.on_contact_selected(contact, focus_widget)

    def _on_cfg_changed(self, key, val):
        if key == 'contacts_autohide':
            self._ui.main_w.contacts_hidden = val

    def _on_contact_typing(self, envelope):
        self._chats_data.typing_indicators.on_typing_message(envelope)
        contact_id = get_envelope_contact_id(envelope)
        if (self._chats_data.current_contact is not None
                and contact_id == self._chats_data.current_contact.id):
            self._ui.chat.try_set_focus(-1)

    def _on_call_message(self, envelope):
        call_message = envelope['callMessage']
        if (
            'offerMessage' in call_message
            or 'answerMessage' in call_message
            or get_nested(call_message, 'hangupMessage', 'type') == 'NORMAL'
                ):
            msg = self._chats_data.chats.add_envelope(envelope)
            if 'offerMessage' in call_message:
                # Incoming call
                self._on_new_message(msg)

    def _on_contacts_sync(self):
        logger.info("Received contacts sync message, reloading signal-cli contacts")
        self._actions.reload()

    def _on_remote_delete(self, envelope):
        msg = self._chats_data.chats.add_remote_delete_envelope(envelope)
        if not msg:
            return
        msg_w = self._ui.msg_ws_cache.get(msg)
        msg_w.reload_markup()

    def _on_sending_remote_delete_done(self, envelope, status='sent', _timestamp_adj=None):
        # Not tracking delivery receipts for remote delete messages. Letting them be buffered by DeliveryStatus and then discarded on exit.
        if status != 'send_failed':
            return
        self._actions.set_status_line(
                'Sending remote delete message failed.',
                append=True,
                )
        msg = self._chats_data.chats.add_remote_delete_envelope(envelope)
        if not msg:
            return
        delattr(msg, 'remote_delete')
        msg_w = self._ui.msg_ws_cache.get(msg)
        msg_w.reload_markup()

    def _on_untrusted_identity_err(self, envelope):
        contact_id = envelope.get('target')
        if contact_id is not None:
            # Message sent to an untrusted identity
            notification = [
                "Contact's safety number has changed: ",
                contact_id,
                " (they might have reinstalled signal). ",
                "Run `signal-cli trust …` to resolve.",
                ]
            self._actions.set_status_line(notification)
            return
        # Message received from an untrusted identity
        contact_id = envelope['source']
        message = f"Message not decrypted: safety number with {contact_id} has changed"
        envelope["dataMessage"] = {
                "message": '[' + message + ']'
                }
        envelope["_received_timestamp"] = get_current_timestamp_ms()
        envelope["_artificialEnvelope"] = "untrustedIdentity"
        msg = self._chats_data.chats.add_envelope(envelope)
        self._on_new_message(msg)
        notification = message + " (they might have reinstalled signal).\nRun `signal-cli trust …` to resolve."
        self._actions.set_status_line(notification)

    def _on_user_unregistered_err(self, envelope):
        self._actions.set_status_line(
                f"Contact {envelope.get('target')} has unregistered. Can not send messages until they re-register."
                )

    def _on_receive_sticker(self, envelope):
        msg = self._chats_data.chats.add_envelope(envelope)
        self._on_new_message(msg)


# #############################################################################
# config
# #############################################################################


class Config:
    def __init__(self, cfg_obj):
        self._cfg_obj = cfg_obj
        self.on_modified = noop

    def set(self, cfg_obj):
        self._cfg_obj = cfg_obj

    def __getattr__(self, name):
        return getattr(self._cfg_obj, name)

    def __setattr__(self, name, value):
        if name != '_cfg_obj' and hasattr(self._cfg_obj, name):
            setattr(self._cfg_obj, name, value)
            self.on_modified(name, value)
        else:
            super().__setattr__(name, value)


cfg = Config(None)


# #############################################################################
# argparse
# #############################################################################


class CustomDefaultsHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show default values in `--help` output for custom-set default values.

    Modified `argparse.ArgumentDefaultsHelpFormatter` class that adds
        `(default: %(default)s)`
    to `--help` output, but only for the explicitly-set `default`s: not `True` for `action=store_true` arguments, and not `None` for `action=store` arguments (`action=store` is the default action for `argparse.add_argument()`, and `None` its default value).
    """

    def _get_help_string(self, action):
        if action.default in (None, False) or " (default: " in action.help:
            return action.help
        return super()._get_help_string(action)


class _VersionFuncAction(argparse._VersionAction):  # pylint: disable=protected-access
    """Allow a callable as an argument to the 'version' action."""

    def __call__(self, *args, **kwargs):
        with suppress(TypeError):
            self.version = self.version()
        super().__call__(*args, **kwargs)


class ArgumentParserVersionFunc(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.register('action', 'version', _VersionFuncAction)


def make_arg_parser():
    parser = ArgumentParserVersionFunc(
        formatter_class=CustomDefaultsHelpFormatter,
    )

    subparser = parser.add_subparsers(
        description='Use `%(prog)s <subcommand> -h` for additional help.',
        dest='subcommand',
    )
    parser_link = subparser.add_parser(
        'link',
        help='Link to an existing device.',
        formatter_class=CustomDefaultsHelpFormatter,
    )
    parser_link.add_argument(
        '-n',
        '--name',
        default='scli',
        help='Device name that will be shown in "Linked devices" list on primary device.',
    )

    parser.add_argument(
        '-c',
        '--config-file',
        default=SCLI_CFG_FILE,
        help='Path to the config file. Arguments on the command line override settings in the file.',
    )

    parser.add_argument(
        '-u',
        '--username',
        help='Phone number starting with "+" followed by country code. If not given, %(prog)s will look for an existing profile in signal-cli\'s data dir.',
    )

    parser.add_argument(
        '-N',
        '--notification-command',
        default="notify-send -i '%a' scli '%s - %m'",
        help="Command to run when a new message arrives. %%m is replaced with the message, %%s is replaced with the sender, %%a is replaced with the path to the contact's avatar file if it exists, or with \"scli\" otherwise.",
    )

    parser.add_argument(
        '-o',
        '--open-command',
        default='xdg-open "%u"',
        help='File/URL opener command. %%u is replaced with the path.',
    )

    parser.add_argument(
        '-e',
        '--editor-command',
        help='External text editor command. If not set, %(prog)s checks among `$VISUAL`, `$EDITOR`, `sensible-editor` etc.',
    )

    parser.add_argument(
        '-G',
        '--clipboard-get-command',
        help='Command used by `:attachClip` to get the list of files to send as message attachments. Should return absolute file paths separated by newline characters. If not set, %(prog)s checks if `wl-clipboard` or `xclip` are installed.',
    )

    parser.add_argument(
        '-P',
        '--clipboard-put-command',
        help="Command used to copy text to clipboard. Text will be sent to command's stdin.  If not set, %(prog)s checks if `wl-clipboard` or `xclip` are installed. (example: xsel -ib)",
    )

    parser.add_argument(
        '--pager-command',
        help='Command used to process the output of help messages, e.g. the list of key bindings. If not set, %(prog)s checks among `$PAGER`, `pager`, `less`, `more`.',
    )

    parser.add_argument(
        '-s',
        '--save-history',
        nargs='?',
        const=SCLI_HISTORY_FILE,
        default=False,
        metavar='HISTORY_FILE',
        help='Enable conversations history. History is saved in plain text. (default %(metavar)s: %(const)s)',
    )

    parser.add_argument(
        '--log-file',
        default=SCLI_LOG_FILE,
        help='Path to the log file. Logs are written only if `--log-file` or `--log-level` is explicitly set.',
    )

    parser.add_argument(
            "--log-level",
            action="append",
            default=["all:warning"],
            help=f"Set logging level {{{','.join(logging_levels_list())}}} (default: warning)",
            )


    parser.add_argument(
        '-n',
        '--enable-notifications',
        action='store_true',
        help='Enable desktop notifications. (See also --notification-command)',
    )

    parser.add_argument(
        '--notify-on-reactions',
        action='store_true',
        help="Show notifications on receiving reaction messages.",
    )

    parser.add_argument(
        '--notification-no-bell',
        action='store_true',
        help="Do not send a \"bell\" code to the terminal on notification. It sets the terminal window's urgency hint, making it more noticable. (The exact visual effect depends on the terminal emulator and the window manager)",
    )

    parser.add_argument(
        '-f',
        '--use-formatting',
        action='store_true',
        help='Show _italic_, *bold*, ~strikethrough~ formatting in messages.',
    )

    parser.add_argument(
        '--color',
        nargs='?',
        const=True,
        default=False,
        help="Colorize messages. See README for options.",
    )

    parser.add_argument(
        '-w',
        '--wrap-at',
        default='85%',
        help="Wrap messages' text at a given number of columns / percentage of available screen width.",
    )

    parser.add_argument(
        '-k',
        '--key-bind',
        action='append',
        metavar='ACTION:KEY[,KEY]',
        help="Modify key bindings: assign KEYs to ACTION. Use `?` in %(prog)s to display the list of available ACTIONs.",
    )

    parser.add_argument(
        '--one-sided',
        action='store_true',
        help='Left-align both sent and received messages',
    )

    parser.add_argument(
        '--show-names',
        action='store_true',
        help="Show contacts' names next to messages, even in one-to-one conversations.",
    )

    parser.add_argument(
        '--show-message-time',
        nargs='?',
        const='%H:%M',
        default=False,
        metavar='FORMAT',
        help="Show messages' timestamps in the specified strftime %(metavar)s. (default %(metavar)s: %(const)r)",
    )

    parser.add_argument(
        '--show-inline',
        nargs='?',
        default=False,
        choices=('columns', 'wrap'),
        const='columns',
        help="Print message's elements (sender's name, message text, attachment list, etc) on a single line.",
    )

    parser.add_argument(
        '--show-message-padding',
        nargs='?',
        const=' ',
        default=False,
        metavar='PAD',
        help="Insert a line of %(metavar)r characters between consecutive messages (default %(metavar)s: %(const)r)",
    )

    parser.add_argument(
        '--group-contacts',
        action='store_true',
        help=argparse.SUPPRESS,
        # The option name can be confusing, e.g. in:
        # https://github.com/isamert/scli/issues/95#issuecomment-757502271
        # Keep for backwards compatiability, but don't show in `--help`. Use `--partition-contacts` instead.
    )

    parser.add_argument(
        '--partition-contacts',
        action='store_true',
        help='Separate groups and individual contacts in the contacts list.',
    )

    parser.add_argument(
        '--contacts-autohide',
        action='store_true',
        help='Autohide the contacts pane when it loses focus.',
    )

    parser.add_argument(
        '--contacts-sort-alpha',
        action='store_true',
        help='Sort contacts alphabetically. (default: sort by the most recent message)',
    )

    parser.add_argument(
        '--daemon-command',
        default=('signal-cli '
                '-u %u '
                '--output=json '
                #'--trust-new-identities=always '  # requires s-cli v0.9.0+; does not notify of safety number change (see signal-cli#826)
                'daemon '
                '--dbus '  # explicit daemon mode required since v0.13.0
                ),
        help='Command for starting signal-cli daemon. The `%%u` in command will be replaced with username (phone number).',
    )

    parser.add_argument(
        '--no-daemon',
        action='store_true',
        help='Do not start signal-cli daemon. Only useful for debugging %(prog)s.',
    )

    parser.add_argument(
        '--debug',
        action='append_const',
        const='debug',
        dest='log_level',
        help='Alias for --log-level=debug.',
    )

    parser.add_argument(
        '--version',
        action='version',
        version=prog_version_str,
    )

    return parser


def get_cfg_file_args(file_obj):
    # Alternatively, can override `ArgumentParser.convert_arg_line_to_args()`.
    ret = {}
    for line in file_obj:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        try:
            name, val = line.split('=', 1)
        except ValueError:
            sys.exit(f"scli: error: Could not parse config line {line!r}")
        ret[name.strip()] = val.strip()
    return ret


def get_opt_val_flags(parser):
    """Flags that optionally take values.

    These are defined by
        ..., nargs='?', const=…, default=…, ...
    See
        https://docs.python.org/3/library/argparse.html#nargs

    They allow any of the following forms on the command line:
        --color
        --color=high
        <nothing> (i.e. option omitted)
    In config file this corresponds to:
        color = true
        color = high
        color = false
                OR
        <nothing> (option not mentioned in config)
    """

    return frozenset(
            opt_str
            for a in parser._actions            # pylint: disable=protected-access
            if (
                a.nargs == argparse.OPTIONAL
                and a.const is not None
                and a.default is not None
                )
            for opt_str in a.option_strings
            )


def get_args_with_actions(parser, action_names):
    # pylint: disable=protected-access
    act_class_map = {
            'store': argparse._StoreAction,
            'store_true': argparse._StoreTrueAction,
            'append': argparse._AppendAction,
            'append_const': argparse._AppendConstAction,
            }
    act_classes = tuple(act_class_map[an] for an in action_names)
    ret = set()
    for act in parser._actions:
        if isinstance(act, act_classes):
            ret.update(act.option_strings)
    return ret


def parse_cfg_file(parser, cli_args):
    cfg_file_path = Path(cli_args.config_file).expanduser()
    try:
        with open(cfg_file_path, encoding="utf-8") as cfg_f:
            cfg_f_args_dict = get_cfg_file_args(cfg_f)
    except FileNotFoundError:
        if cli_args.config_file == parser.get_default('config_file'):
            return cli_args
        sys.exit(f"ERROR: Config file not found: {cfg_file_path}")

    true_false_val_args = (
            get_opt_val_flags(parser)
            |
            get_args_with_actions(
                parser,
                ("store_true", "append_const"),
                )
            )
    args_list = []
    for arg_name, arg_val in cfg_f_args_dict.items():
        arg_name = '--' + arg_name
        if arg_name in true_false_val_args:
            if arg_val.lower() in ('true', 't', 'yes', 'y'):
                args_list.append(arg_name)
            elif arg_val.lower() not in ('false', 'f', 'no', 'n'):
                args_list.extend((arg_name, arg_val))
        else:
            args_list.extend((arg_name, arg_val))

    # Need to actually parse the arguments (rather then simply updating args.__dict__), so that the `type`s would be set correctly.
    exit_on_error_orig = parser.exit_on_error
    parser.exit_on_error = False
    try:
        cfg_file_args = parser.parse_args(args_list)
    except argparse.ArgumentError:
        args_str = ' '.join([repr(a) if not a else a for a in args_list])
        parser.exit(f"scli: error: Could not parse config file `{cfg_file_path}` arguments: {args_str!r}")
    finally:
        parser.exit_on_error = exit_on_error_orig
    parser.parse_args(namespace=cfg_file_args)
    return cfg_file_args


def parse_wrap_at_arg(width):
    def bad_val(width):
        sys.exit(
                f"ERROR: Could not parse the width value: `{width}`.\n"
                "The value should be an `<int>` or a `<float>%` (`42` or `42.42%`).\n"
                "See `--help` for additional info."
                )
    if width.endswith('%'):
        try:
            percent_width = float(width.rstrip('%'))
        except ValueError:
            bad_val(width)
        return ('relative', percent_width)
    else:
        try:
            return int(width)
        except ValueError:
            bad_val(width)


def parse_args():
    parser = make_arg_parser()
    args = parser.parse_args()

    if args.subcommand == 'link':
        link_device(args.name)
        sys.exit()

    if args.config_file:
        args = parse_cfg_file(parser, args)
    modified_args = {arg: val for arg, val in vars(args).items() if val != parser.get_default(arg)}

    args.editor_command = args.editor_command or get_default_editor()
    args.pager_command = args.pager_command or get_default_pager()
    args.username = args.username or detect_username()
    if args.color:
        args.color = Color(args.color)
    if args.save_history:
        args.save_history = os.path.expanduser(args.save_history)
    args.wrap_at = parse_wrap_at_arg(args.wrap_at)
    if args.group_contacts:
        print("Warning: `--group-contacts` option is deprecated; use `--partition-constants` instead.")
    args.partition_contacts = args.partition_contacts or args.group_contacts
    del args.__dict__['group_contacts']
    if args.clipboard_put_command and "%s" in args.clipboard_put_command:
        print("Warning: `--clipboard-put-command` does not use replacement tokens ('%s'). See `--help`.")
    return args, modified_args


# #############################################################################
# logging
# #############################################################################


class DeferredEval:

    __slots__ = ("func", "args", "kwargs")

    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def __str__(self):
        return self.func(*self.args, **self.kwargs)


def parse_log_level_args(args_append_list, sep=':', def_key=Path(__file__).stem):
    """Parse values of `--log-level` arguments.

    The values are "LOGGER:LOG_LEV", where LOGGER is one of the loggers registered with the logging module: urwid, asyncio, scli (this module), etc. Example: `--log-level urwid:warning`. Values can also be just LOG_LEV (e.g. `--log-level info`), in which case LOGGER is set to "scli". A special LOGGER name "all" sets levels for all of the registered loggers.
    """
    arg_map = {}
    for arg in args_append_list:
        try:
            k, v = arg.split(sep, 1)
        except ValueError:
            k = def_key
            v = arg
        if k == "all":
            arg_map.clear()
        with suppress(AttributeError):
            v = getattr(logging, v.upper())
        arg_map[k] = v
    with suppress(KeyError):
        arg_map.setdefault("Daemon", arg_map[def_key])
    return arg_map


def logging_levels_list():
    """Get the list of `logging` module's built-in log levels."""
    # Python 3.11 introduced `logging.getLevelNamesMapping()`.
    return [logging.getLevelName(n).lower() for n in range(
            logging.DEBUG,      # == 10
            logging.CRITICAL + logging.DEBUG,  # +10, for `range`'s non-inclusive upper limit
            logging.WARNING - logging.INFO,     # == 10
            )]


def set_daemon_verbose_level(vs):
    prog_name = "signal-cli"
    cfg.daemon_command = cfg.daemon_command.replace(  # pylint: disable=attribute-defined-outside-init
            prog_name,
            f"{prog_name} -{vs}",
            1,
            )


def logging_setup(filename, levels):
    log_levels = parse_log_level_args(levels)
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
            filename=filename,
            level=log_levels.pop("all"),
            )
    try:
        log_lev_daemon = log_levels.pop("signal-cli")
    except KeyError:
        pass
    else:
        set_daemon_verbose_level(log_lev_daemon)
    for logger_name, log_lev in log_levels.items():
        logging.getLogger(logger_name).setLevel(log_lev)
    logger.info(DeferredEval(prog_version_str))
    logger.info("urwid %s", urwid.__version__)
    logger.info("python %s", DeferredEval(get_python_version))


logger = logging.getLogger(Path(__file__).stem)


# #############################################################################
# main
# #############################################################################


class BracketedPasteMode:
    """Context manager for enabling/disabling bracketed paste mode."""
    # Same as tdryer's code
    # https://github.com/urwid/urwid/issues/119#issuecomment-761424363

    def __enter__(self):
        sys.stdout.write('\x1b[?2004h')

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout.write('\x1b[?2004l')


def link_device(device_name):
    try:
        pyqrcode = importlib.import_module('pyqrcode')
    except ImportError:
        sys.exit(
                "ERROR: `pyqrcode` module not found. "
                "Please install it with `pip install pyqrcode`"
                )
    print("Retrieving QR code, please wait...")
    cmd_link = ['signal-cli', 'link', '-n', device_name]
    with subprocess.Popen(
            cmd_link, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as proc_link:
        line = proc_link.stdout.readline().strip()
        if line.startswith(('tsdevice:/', 'sgnl://linkdevice')):
            qr_obj = pyqrcode.create(line, version=10)
            print(qr_obj.terminal(module_color='black', background='white'))
        else:
            sys.exit(
                    "ERROR: Encountered a problem while linking:\n"
                    f"{line}\n"
                    f"{proc_link.stderr.read()}"
                    )

        print(
                "Scan the QR code with Signal app on your phone and wait for the linking process to finish.\n"
                "You might need to zoom out for the QR code to display properly.\n"
                "This may take a moment..."
                )
        proc_link.wait()
        if proc_link.returncode != 0:
            sys.exit(
                    "ERROR: Encountered a problem while linking:\n"
                    f"{proc_link.stderr.read()}"
                    )

    print('Receiving data for the first time...')

    cmd_receive = 'signal-cli -u {} receive'.format(detect_username())
    with subprocess.Popen(
            cmd_receive.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as proc_receive:
        for receive_out in iter(proc_receive.stdout.readline, ''):
            print(receive_out, end='')
        proc_receive.wait()
        if proc_receive.returncode != 0:
            sys.exit(
                    "ERROR: Encountered a problem while receiving:\n"
                    f"{proc_receive.stderr.read()}"
                    )

    print('Done.')
    sys.exit(0)


def detect_username():
    accounts = [acc["number"] for acc in SignalData.parse_accounts_json()]

    if not accounts:
        sys.exit("ERROR: Could not find any registered accounts. "
                "Register a new one or link with an existing device (see README).")
    elif len(accounts) == 1:
        return accounts[0]
    else:
        sys.exit("ERROR: Multiple accounts found. Run one of:\n\t"
                + "\n\t".join((f"scli --username={u}" for u in accounts)))


def main():
    args, modified_args = parse_args()
    cfg.set(args)

    if any(arg in modified_args for arg in (
            "log_file",
            "log_level",
            )):
        logging_setup(filename=args.log_file, levels=args.log_level)
    else:
        logging.disable()
    logger.debug("args = %s", DeferredEval(pprint.pformat, modified_args))

    KEY_BINDINGS.set(args.key_bind)

    coord = Coordinate()
    loop = coord.daemon.main_loop

    if not args.no_daemon:
        proc = coord.daemon.start()
        atexit.register(proc.kill)
        action_request.set_status_line("Initializing signal-cli daemon... ")

    for sig in (signal_ipc.SIGHUP, signal_ipc.SIGTERM):
        signal_ipc.signal(sig, lambda signum, frame: action_request.quit())

    with BracketedPasteMode():
        loop.run()


if __name__ == "__main__":
    main()
