import builtins
import json
import os
import select
import signal
import socket
import sys
import logging
from multiprocessing.reduction import recvfds, sendfds

SOCKET_PATH = os.path.expanduser('~/.xonshd.sock')
LOG_PATH = os.path.expanduser('~/.xonshd.log')
MAX_DATA_DIGITS = 5

log = logging.getLogger('xonshd')


def xonshd_reaper(*args):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        else:
            if pid == 0:
                break


def xonshd_handle_client(*, start_services_coro, env, cwd, argv, tty):
    import xonsh.main

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    sys.argv = argv
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)

    env = builtins.__xonsh_env__
    for key, value in os.environ.items():
        env[key] = value

    env['XONSH_INTERACTIVE'] = tty

    # FIXME: reinitialise history, and figure out why it's not being written

    try:
        while True:
            next(start_services_coro)
    except StopIteration as e:
        pass

    # FIXME: we're too late, __xonsh_shell__ will already be initialised to
    # something non-interactive, but we'll pretend anyway. Surely to cause
    # bugs.
    class args:
        if tty:
            mode = xonsh.main.XonshMode.interactive
        else:
            mode = xonsh.main.XonshMode.script_from_stdin

    xonsh.main.main_xonsh(args)


def xonshd():
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO)

    log.info("xonshd starting... %s", os.getpid())

    import signal
    signal.signal(signal.SIGCHLD, xonshd_reaper)

    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    with socket.socket(socket.AF_UNIX) as listen:
        listen.bind(SOCKET_PATH)
        listen.listen(5)

        try:
            xonshd_server(listen)
        except Exception:
            log.exception("Error starting server")
        finally:
            sys.exit(0)


def _daemonise(tty):
    if tty:
        import pty
        pid, *fds = pty.fork()
    else:
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()

        pid = os.fork()
        if pid == 0:
            os.closerange(0, 3)

            # replace stdio
            os.dup2(stdin_r, 0)
            os.dup2(stdout_w, 1)
            os.dup2(stderr_w, 2)

            # close the source stdio
            os.close(stdin_r)
            os.close(stdout_w)
            os.close(stderr_w)

            # close ones we don't care about
            os.close(stdin_w)
            os.close(stdout_r)
            os.close(stderr_r)
            fds = []
        else:
            # close ones we don't care about
            os.close(stdin_r)
            os.close(stdout_w)
            os.close(stderr_w)

            # fds to ship off to the foreground
            fds = [stdin_w, stdout_r, stderr_r]

    return pid, fds


def xonshd_server(listen):
    import xonsh.main

    builtins.__xonsh_ctx__ = {}
    shell_kwargs = {
        'shell_type': 'best',
        'completer': True,
        'login': True,
        'ctx': builtins.__xonsh_ctx__,
        'scriptcache': True,
    }
    start_services_coro = xonsh.main._start_services(shell_kwargs)

    # load execer and shell
    next(start_services_coro)

    env = builtins.__xonsh_env__
    env['XONSH_LOGIN'] = shell_kwargs['login']

    while True:
        r, w, x = select.select([listen], [], [], 1)
        if not r:
            continue

        client, addr = listen.accept()

        params_len = int(client.recv(MAX_DATA_DIGITS).decode())
        params = b''

        while len(params) != params_len:
            b = client.recv(params_len - len(params))
            if not b:
                assert False, 'somethings happened, save me'
            params += b

        data = json.loads(params)

        log.info("starting client")
        log.info("argv %s", data['argv'])
        log.info("cwd %s", data['cwd'])
        log.info("env %s", data['env'])
        log.info("tty %s", data['tty'])

        pid, fds = _daemonise(tty=data['tty'])

        if pid == 0:
            exit_code = 0

            try:
                xonshd_handle_client(
                    start_services_coro=start_services_coro, **data)
            except SystemExit as e:
                exit_code = e.code
            except:
                import traceback
                traceback.print_exc()

                log.exception("Problem handling client")
                exit_code = 1
            else:
                exit_code = 0
            finally:
                client.send(bytearray([exit_code]))
                os._exit(0)
        else:
            sendfds(client, fds)
            client.close()

            for fd in fds:
                os.close(fd)

            continue


def xonshc(server):
    data = json.dumps({
        'env': dict(os.environ),
        'cwd': os.getcwd(),
        'argv': sys.argv,
        'tty': sys.stdin.isatty(),
    })
    data_len = str(len(data)).zfill(MAX_DATA_DIGITS)
    assert len(data_len) == MAX_DATA_DIGITS

    server.sendall(data_len.encode('ascii'))
    server.sendall(data.encode('utf8'))
    fd_count = 1 if sys.stdin.isatty() else 3
    remote_fds = recvfds(server, fd_count)
    xonshc_main(remote_fds, server)


def xonshc_resize(*args, remote_tty):
    import array
    import fcntl
    import termios

    buf = array.array('h', [0, 0, 0, 0])

    try:
        fcntl.ioctl(0, termios.TIOCGWINSZ, buf, True)
        fcntl.ioctl(remote_tty, termios.TIOCSWINSZ, buf)
    except Exception:
        pass


def xonshc_main(remote_fds, server):
    import functools
    import tty

    def _write(fd, data):
        while data:
            n = os.write(fd, data)
            data = data[n:]

    if sys.stdin.isatty():
        _resize = functools.partial(xonshc_resize, remote_tty=remote_fds[0])
        signal.signal(signal.SIGWINCH, _resize)

        try:
            mode = tty.tcgetattr(0)
            tty.setraw(0)
            restore = True
        except tty.error:
            restore = True

        _resize()
    else:
        restore = False

    proxy_map = {}
    for i, fd in enumerate(remote_fds):
        proxy_map[i] = fd
        proxy_map[fd] = i

    try:
        while True:
            if not proxy_map:
                # dodgy hack to ensure we hit the except clause
                raise OSError()

            readable, *ignored = select.select(proxy_map.keys(), [], [])

            for fd in readable:
                dest = proxy_map[fd]

                b = os.read(fd, 1024)
                if not b:
                    proxy_map.pop(fd)
                    proxy_map.pop(dest)
                    os.close(fd)
                    os.close(dest)
                    continue

                _write(dest, b)
    except (OSError, IOError):
        if restore:
            tty.tcsetattr(0, tty.TCSAFLUSH, mode)
            tty.setcbreak(0)

        exit_code = server.recv(1)
        if not exit_code:
            sys.exit(1)
        sys.exit(exit_code[0])


def main():
    # FIXME: would be nice if the daemon were spun up on demand, but I can't
    # get that to work due to TTY weirdness.

    if '--daemon' in sys.argv:
        xonshd()
    else:
        with socket.socket(socket.AF_UNIX) as server:
            server.connect(SOCKET_PATH)
            xonshc(server)


if __name__ == '__main__':
    main()
