#!/usr/bin/env python3
import errno
import os
import pty
import signal
import array
import fcntl
import termios
import select
import io
import shlex
import sys
import struct
import tty
import random
import time
import yaml
import re
import argparse


def ctrl(c):
    return chr(ord(c.upper()) - ord('A') + 1)


def alt(c):
    return '\x1b' + chr(ord(c.lower()) - ord('a') + 1)


def alt_shift(c):
    return '\x1b' + chr(ord(c.upper()) - ord('a') + 1)


def csi(v):
    return '\x1b[' + v


KEYCODES = {
    'esc': '\x1b',
    'del': '\x7f',
    'up': csi('A'),
    'down': csi('B'),
    'right': csi('C'),
    'left': csi('D'),
    'el': ctrl(b'U'),
}

ACT_SLEEP = 1
ACT_WAIT_SEQUENCE = 2

SCRIPT_COMMANDS = {}


def script_command(name):
    def register(f):
        SCRIPT_COMMANDS[name] = f
        return f

    return register


def _escape_keys(m):
    keycode = m.group(1).lower()
    if keycode.startswith('ctrl-'):
        try:
            return ctrl(keycode.split('-', 1)[1])
        except:
            pass
    elif keycode.startswith('alt-shift-') or keycode.startswith('shift-alt-'):
        try:
            return alt_shift(keycode.split('-', 2)[2])
        except:
            pass
    elif keycode.startswith('alt-'):
        try:
            return alt(keycode.split('-', 1)[1])
        except:
            pass

    return KEYCODES.get(m.group(1).lower(), '')


def ActSleep(timeout):
    return {ACT_SLEEP: time.time() + timeout}


def ActWaitSequence(sequence):
    return {ACT_WAIT_SEQUENCE: sequence}


class raw():
    def __init__(self, fd):
        self.fd = fd
        self.restore = False

    def __enter__(self):
        try:
            self.mode = tty.tcgetattr(self.fd)
            tty.setraw(self.fd)
            self.restore = True
        except tty.error:  # This is the same as termios.error
            pass

    def __exit__(self, type, value, traceback):
        if self.restore:
            tty.tcsetattr(self.fd, tty.TCSAFLUSH, self.mode)


def _rand_range(start, end):
    return start + (random.random() * (end - start))


class Sequence(object):
    def __init__(self, commands):
        self._commands = commands

    def execute(self, master_fd):
        for command in self._commands:
            yield from command.execute(master_fd)


@script_command('enter')
def CmdEnter(text):
    text = str(text)
    if not text.endswith('\n'):
        text += '\n'

    return Sequence((CmdType(text), CmdWait('prompt')))


@script_command('type')
class CmdType(object):
    def __init__(self, text):
        self._text = str(text)

    def execute(self, master_fd):
        text = self._text.encode('utf8').decode('unicode_escape')
        text = re.sub(r'<([\w\d-]+)>', _escape_keys, text)
        for c in text:
            yield ActSleep(_rand_range(0.05, 0.2))
            os.write(master_fd, c.encode('utf8'))


@script_command('wait')
class CmdWait(object):
    def __init__(self, what):
        if what != 'prompt':
            raise RuntimeError("Invalid wait argument '%s'" % what)

    def execute(self, master_fd):
        yield ActWaitSequence(b'\x1B]777;notify;Command completed;')


@script_command('sleep')
class CmdSleep(object):
    def __init__(self, timeout):
        self._timeout = float(timeout)

    def execute(self, master_fd):
        yield ActSleep(self._timeout)


class ScriptParseError(RuntimeError):
    def __init__(self, file, token, msg):
        line = token.start_mark.line
        column = token.start_mark.column
        super(ScriptParseError, self).__init__("%s:%d:%d:%s" % (file,
                                                                line,
                                                                column,
                                                                msg))


def compile(script):
    def _expect_token(token, token_type, msg="invalid syntax"):
        if not isinstance(token, token_type):
            raise ScriptParseError(script.name, token, msg)

        return token

    tokens = yaml.scan(script)
    _expect_token(tokens.send(None), yaml.StreamStartToken)
    _expect_token(tokens.send(None), yaml.BlockMappingStartToken)

    commands = []
    for token in tokens:
        if isinstance(token, yaml.BlockEndToken):
            break

        _expect_token(token, yaml.KeyToken)
        key_tok = _expect_token(tokens.send(None), yaml.ScalarToken)
        command_name = key_tok.value
        _expect_token(tokens.send(None), yaml.ValueToken)
        value_tok = _expect_token(tokens.send(None), yaml.ScalarToken)
        command_arg = value_tok.value

        cmd_class = SCRIPT_COMMANDS.get(command_name)
        if not cmd_class:
            raise ScriptParseError(script.name, key_tok,
                                   ("Unknown command '%s'" % command_name))

        try:
            commands.append(cmd_class(command_arg))
        except Exception as e:
            raise ScriptParseError(script.name, value_tok, str(e))

    _expect_token(tokens.send(None), yaml.StreamEndToken)

    return Sequence(commands)


def record_command(script, command=None, env=os.environ):
    master_fd = None
    script_action = {}
    deadline = -1
    if command is None:
        command = (env.get('SHELL'),)

    def _set_pty_size():
        if os.isatty(pty.STDOUT_FILENO):
            buf = array.array('h', [0, 0, 0, 0])
            fcntl.ioctl(pty.STDOUT_FILENO, termios.TIOCGWINSZ, buf, True)
        else:
            buf = array.array('h', [24, 80, 0, 0])

        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)

    def _write_stdout(data):
        os.write(pty.STDOUT_FILENO, data)

    def _handle_master_read(data):
        if script_action.get(ACT_WAIT_SEQUENCE):
            if script_action.get(ACT_WAIT_SEQUENCE) in data:
                script_action.clear()

        _write_stdout(data)

    def _write_master(data):
        while data:
            n = os.write(master_fd, data)
            data = data[n:]

    def _handle_stdin_read(data):
        _write_master(data)

    def _signals(signal_list):
        old_handlers = []
        for sig, handler in signal_list:
            old_handlers.append((sig, signal.signal(sig, handler)))
        return old_handlers

    def _copy(signal_fd):
        runner = script.execute(master_fd)

        fds = [master_fd, pty.STDIN_FILENO, signal_fd]

        timeout = None
        while True:

            try:
                while runner:
                    if script_action.get(ACT_SLEEP):
                        timeout = script_action.get(ACT_SLEEP) - time.time()

                        if timeout < 0:
                            timeout = None
                            script_action.clear()
                            script_action.update(runner.send(None))
                            continue

                    if not script_action:
                        script_action.clear()
                        script_action.update(runner.send(None))

                    break
            except StopIteration:
                runner = None

            try:
                rfds, wfds, xfds = select.select(fds, [], [], timeout)
            except OSError as e:  # Python >= 3.3
                if e.errno == errno.EINTR:
                    continue
            except select.error as e:  # Python < 3.3
                if e.args[0] == 4:
                    continue

            if master_fd in rfds:
                data = os.read(master_fd, 1024)
                if not data:  # Reached EOF.
                    fds.remove(master_fd)
                else:
                    _handle_master_read(data)

            if pty.STDIN_FILENO in rfds:
                data = os.read(pty.STDIN_FILENO, 1024)
                if not data:
                    fds.remove(pty.STDIN_FILENO)
                else:
                    if not runner:
                        _handle_stdin_read(data)

            if signal_fd in rfds:
                data = os.read(signal_fd, 1024)
                if data:
                    signals = struct.unpack('%uB' % len(data), data)
                    for sig in signals:
                        if sig in [signal.SIGCHLD,
                                   signal.SIGHUP,
                                   signal.SIGTERM,
                                   signal.SIGQUIT]:
                            os.close(master_fd)
                            return
                        elif sig == signal.SIGWINCH:
                            _set_pty_size()

    pid, master_fd = pty.fork()

    if pid == pty.CHILD:
        os.execvpe(command[0], command, env)

    pipe_r, pipe_w = os.pipe()
    flags = fcntl.fcntl(pipe_w, fcntl.F_GETFL, 0)
    flags = flags | os.O_NONBLOCK
    flags = fcntl.fcntl(pipe_w, fcntl.F_SETFL, flags)

    signal.set_wakeup_fd(pipe_w)

    old_handlers = _signals(map(lambda s: (s, lambda signal, frame: None),
                                [signal.SIGWINCH,
                                 signal.SIGCHLD,
                                 signal.SIGHUP,
                                 signal.SIGTERM,
                                 signal.SIGQUIT]))

    _set_pty_size()

    with raw(pty.STDIN_FILENO):
        try:
            _copy(pipe_r)
        except (IOError, OSError):
            pass

    _signals(old_handlers)

    os.waitpid(pid, 0)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Script terminal interactions')
    parser.add_argument('script_file',
                        metavar='SCRIPT',
                        type=argparse.FileType('r'),
                        nargs=1,
                        help='the script to execute')

    return parser.parse_args(sys.argv[1:])

if __name__ == "__main__":
    args = parse_args()
    try:
        s = compile(args.script_file[0])
    except ScriptParseError as e:
        print(str(e))
        sys.exit(1)

    record_command(s)
