"""``python -m skein.web`` -> serve the read surface on port 9001."""

from .app import run_server


def main() -> None:
    run_server()


if __name__ == "__main__":
    main()
