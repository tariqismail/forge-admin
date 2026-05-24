#!/usr/bin/env python3
import sys

from lib.auth import generate_invite_token


def main():
    if len(sys.argv) < 2:
        print("Usage: python cli.py invite <name>")
        print("       python cli.py serve")
        sys.exit(1)

    command = sys.argv[1]

    if command == "invite":
        if len(sys.argv) < 3:
            print("Usage: python cli.py invite <name>")
            sys.exit(1)
        name = " ".join(sys.argv[2:])
        token = generate_invite_token(name)
        print(f"\nInvite link for {name}:")
        print(f"  http://localhost:5050/login?token={token}")
        print(f"\nShare this link. It's single-use and expires on first login.\n")

    elif command == "serve":
        from app import app
        app.run(host="127.0.0.1", port=5050, debug=True)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
