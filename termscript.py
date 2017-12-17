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

KEYCODES = {
    'esc': '\x1b'
}

ACT_SLEEP = 1
ACT_WAIT_SEQUENCE = 2

def _escape_keys(m):
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


def _cmd_enter(cmd, master_fd):
    text = cmd.get('enter')
    if not text.endswith('\n'):
        text += '\n'

    yield from _cmd_type({'type': text}, master_fd)
    yield from _cmd_wait_prompt({'wait-prompt': True}, master_fd)

def _cmd_sleep(cmd, master_fd):
    yield ActSleep(cmd.get('sleep'))

def _cmd_type(cmd, master_fd):
    text = cmd.get('type').encode('utf8').decode('unicode_escape')
    text = re.sub(r'<([\w\d-]+)>', _escape_keys, text)
    for c in text:
        yield ActSleep(_rand_range(0.05, 0.2))
        os.write(master_fd, c.encode('utf8'))

def _cmd_wait_prompt(cmd, master_fd):
    yield ActWaitSequence(b'\x1B]777;notify;Command completed;')


def script_runner(script, master_fd):
    for cmd in script:
        cmd = {cmd:True} if isinstance(cmd, str) else cmd
        if cmd.get('enter'):
            yield from _cmd_enter(cmd, master_fd)
        if cmd.get('sleep'):
            yield from _cmd_sleep(cmd, master_fd)
        if cmd.get('type'):
            yield from _cmd_type(cmd, master_fd)

        if cmd.get('wait-prompt'):
            yield from _cmd_wait_prompt(cmd, master_fd)


def record_command(script, command=None, env=os.environ):
    master_fd = None
    script_action = {}
    deadline = -1
    if command is None:
        command = (env.get('SHELL'),)

    def _set_pty_size():
        # Get the terminal size of the real terminal, set it on the pseudoterminal.
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
        runner = script_runner(script, master_fd)

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
                        if sig in [signal.SIGCHLD, signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT]:
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

if __name__ == "__main__":
    with open(sys.argv[1], 'r') as f:
        s = yaml.load(f)

    record_command(s)
