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


def xonshd_handle_client(*, start_services_coro, env, cwd, argv):
    import xonsh.main

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    sys.argv = argv
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)

    env = builtins.__xonsh_env__
    for key, value in os.environ.items():
        env[key] = value

    # FIXME: reinitialise history, and figure out why it's not being written

    try:
        while True:
            next(start_services_coro)
    except StopIteration as e:
        pass

    class args:
        mode = xonsh.main.XonshMode.interactive

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


def xonshd_server(listen):
    import pty
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

        pid, pty_fd = pty.fork()
        if pid == 0:
            exit_code = 0

            try:
                xonshd_handle_client(
                    start_services_coro=start_services_coro, **data)
            except SystemExit as e:
                exit_code = e.code
            except:
                log.exception("Problem handling client")
                exit_code = 1
            else:
                exit_code = 0
            finally:
                client.send(bytearray([exit_code]))
                os._exit(0)
        else:
            sendfds(client, [pty_fd])
            client.close()
            os.close(pty_fd)
            continue


def xonshc(server):
    data = json.dumps({
        'env': dict(os.environ),
        'cwd': os.getcwd(),
        'argv': sys.argv,
    })
    data_len = str(len(data)).zfill(MAX_DATA_DIGITS)
    assert len(data_len) == MAX_DATA_DIGITS

    server.sendall(data_len.encode('ascii'))
    server.sendall(data.encode('utf8'))
    remote_tty = recvfds(server, 1)[0]

    xonshc_main(remote_tty, server)


def xonshc_resize(*args, remote_tty):
    import array
    import fcntl
    import termios

    buf = array.array('h', [0, 0, 0, 0])

    try:
        fcntl.ioctl(0, termios.TIOCGWINSZ, buf, True)
        fcntl.ioctl(remote_tty, termios.TIOCSWINSZ, buf)
    except Exception:
        pass  # FIXME: why was I doing this at work?


def xonshc_main(remote_tty, server):
    import functools
    import tty

    def _write(fd, data):
        while data:
            n = os.write(fd, data)
            data = data[n:]

    _resize = functools.partial(xonshc_resize, remote_tty=remote_tty)
    signal.signal(signal.SIGWINCH, _resize)

    try:
        mode = tty.tcgetattr(0)
        tty.setraw(0)
        restore = 1
    except tty.error:
        restore = 0

    _resize()

    try:
        while True:
            r, *idk = select.select([0, remote_tty], [], [])

            if remote_tty in r:
                b = os.read(remote_tty, 1024)
                if not b:
                    break
                _write(0, b)
            if 0 in r:
                b = os.read(0, 1024)
                if not b:
                    break
                _write(remote_tty, b)
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
