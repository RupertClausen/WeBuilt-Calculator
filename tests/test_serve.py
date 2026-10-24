"""Picking the port: lingering connections, busy ports, team-server mode."""
import os, pathlib, socket, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve


def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":                              # as uvicorn's own listener does
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_lingering_connections_do_not_count():
    """After a restart the old server's closed connections sit in TIME_WAIT
    for a minute. That port is free for uvicorn, so it must look free here."""
    if os.name == "nt":
        print("  lingering       (Linux behaviour; skipped on Windows)")
        return
    srv, port = listener()
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()                     # server side closes first -> TIME_WAIT on `port`
    cli.recv(1); cli.close()
    srv.close()
    assert serve.port_is_free(port)
    assert serve.free_port(port) == port and serve.exact_port(port, wait=0) == port
    print(f"  lingering       port {port} with a TIME_WAIT connection: free, same port used")


def test_busy_port():
    srv, port = listener()
    try:
        assert not serve.port_is_free(port)
        assert serve.free_port(port) != port                    # desktop: moves on
        try:
            serve.exact_port(port, wait=1)                       # server: never moves
            raise AssertionError("should have stopped")
        except SystemExit as e:
            assert str(port) in str(e)
    finally:
        srv.close()
    print(f"  busy            port {port} in use: desktop picks another, server waits then stops")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} port tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
