"""Account file I/O — read and remove accounts from akun.txt."""

import os


def read_accounts(filepath):
    """Read accounts from file. Format: email|password or email:password (one per line)."""
    if not os.path.exists(filepath):
        print(f" [ERROR] File '{filepath}' not found!")
        return []

    with open(filepath, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    accounts = []
    for line in lines:
        # Determine the separator: prefer "|" if present, otherwise ":"
        if "|" in line:
            sep = "|"
        elif ":" in line:
            sep = ":"
        else:
            continue

        parts = line.split(sep, 1)
        email, password = parts[0].strip(), parts[1].strip()
        if email and password:
            accounts.append({"email": email, "password": password, "raw": line})
    return accounts


def remove_account(filepath, raw_line):
    """Remove a successfully processed account from the file."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        remaining = [line for line in lines if line.strip() != raw_line]
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(remaining)
    except PermissionError:
        pass