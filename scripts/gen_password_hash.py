#!/usr/bin/env python3
# scripts/gen_password_hash.py
# Generates a bcrypt hash for ADMIN_PASSWORD_HASH in .env
#
# Usage:
#   python3 scripts/gen_password_hash.py
#   python3 scripts/gen_password_hash.py mypassword

import sys

try:
    import bcrypt
except ImportError:
    print("Error: bcrypt not installed. Run: pip install bcrypt")
    sys.exit(1)

def main():
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass
        password = getpass.getpass("Enter admin password: ")
        confirm  = getpass.getpass("Confirm password:     ")
        if password != confirm:
            print("Error: passwords do not match.")
            sys.exit(1)

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    print("\nAdd this line to your .env file:\n")
    print(f"ADMIN_PASSWORD_HASH={hashed}\n")

if __name__ == "__main__":
    main()
