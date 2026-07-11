from __future__ import annotations

import argparse
import getpass
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from shared.lib.crypto import CryptoError, derive_age_public_key, encrypt_secret

TOP_LEVEL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+:\s*(?:#.*)?$")
SECRET_ENTRY_PATTERN = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$")


@dataclass(frozen=True)
class SecretTarget:
    scope: str
    label: str
    path: Path
    plugin_name: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Encrypt and store platform or agent secrets.")
    parser.add_argument("--name", help="Secret env var name, e.g. SERVICE_API_TOKEN")
    parser.add_argument("--value", help="Secret plaintext value. If omitted, prompt securely.")
    parser.add_argument("--scope", choices=("shared", "plugin"), help="Secret destination scope")
    parser.add_argument("--plugin", help="Plugin name when --scope=plugin")
    parser.add_argument("--identity", help="Age identity path or inline key")
    parser.add_argument("--platform-file", help="Path to shared platform-config.yaml")
    parser.add_argument("--plugins-dir", help="Path to plugins directory")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def platform_file_from_args(args: argparse.Namespace) -> Path:
    if args.platform_file:
        return Path(args.platform_file).expanduser()
    return repo_root() / "shared" / "platform-config.yaml"


def plugins_dir_from_args(args: argparse.Namespace) -> Path:
    return Path(args.plugins_dir).expanduser() if args.plugins_dir else repo_root() / "plugins"


def identity_from_args(args: argparse.Namespace) -> str:
    if args.identity:
        return args.identity
    return str(repo_root() / "key.txt")


def discover_plugin_targets(plugins_dir: Path) -> list[SecretTarget]:
    targets: list[SecretTarget] = []
    for agent_file in sorted(plugins_dir.glob("*/agent.yaml")):
        plugin_name = agent_file.parent.name
        targets.append(SecretTarget(scope="plugin", label=plugin_name, path=agent_file, plugin_name=plugin_name))
    return targets


def prompt_secret_name() -> str:
    while True:
        name = input("Secret name: ").strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            return name
        print("Secret names must match [A-Z][A-Z0-9_]*")


def prompt_secret_value() -> str:
    while True:
        value = getpass.getpass("Secret value: ")
        if value:
            return value
        print("Secret value cannot be empty")


def prompt_target(platform_file: Path, plugins_dir: Path) -> SecretTarget:
    options = [SecretTarget(scope="shared", label="shared platform config", path=platform_file)]
    options.extend(discover_plugin_targets(plugins_dir))
    print("Select destination:")
    for index, target in enumerate(options, start=1):
        print(f"{index}. {target.label}")
    while True:
        raw = input("Choice: ").strip()
        try:
            selected = int(raw)
        except ValueError:
            print("Enter a number from the list")
            continue
        if 1 <= selected <= len(options):
            return options[selected - 1]
        print("Choice out of range")


def resolve_target(args: argparse.Namespace, platform_file: Path, plugins_dir: Path) -> SecretTarget:
    if not args.scope:
        return prompt_target(platform_file, plugins_dir)
    if args.scope == "shared":
        return SecretTarget(scope="shared", label="shared platform config", path=platform_file)
    if not args.plugin:
        raise SystemExit("--plugin is required when --scope=plugin")
    agent_file = plugins_dir / args.plugin / "agent.yaml"
    if not agent_file.exists():
        raise SystemExit(f"Plugin not found: {args.plugin}")
    return SecretTarget(scope="plugin", label=args.plugin, path=agent_file, plugin_name=args.plugin)


def ensure_secret_section(lines: list[str]) -> tuple[list[str], int, int]:
    for index, line in enumerate(lines):
        if re.fullmatch(r"secrets:\s*\{\}\s*", line):
            lines[index : index + 1] = ["secrets:"]
            return lines, index, index + 1
        if re.fullmatch(r"secrets:\s*", line):
            return lines, index, find_section_end(lines, index)

    if lines and lines[-1].strip():
        lines.append("")
    start = len(lines)
    lines.append("secrets:")
    return lines, start, start + 1


def find_section_end(lines: list[str], start: int) -> int:
    index = start + 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped and not line.startswith((" ", "\t")) and TOP_LEVEL_KEY_PATTERN.match(line):
            break
        index += 1
    return index


def find_secret_block_end(lines: list[str], start: int, section_end: int) -> int:
    index = start + 1
    while index < section_end:
        if SECRET_ENTRY_PATTERN.match(lines[index]):
            break
        index += 1
    return index


def upsert_secret_text(text: str, *, secret_name: str, encrypted_value: str) -> str:
    lines = text.splitlines()
    lines, section_start, section_end = ensure_secret_section(lines)
    entry_line = f"  {secret_name}:"
    encrypted_line = f'    encrypted: "{encrypted_value}"'

    secret_start: int | None = None
    for index in range(section_start + 1, section_end):
        match = SECRET_ENTRY_PATTERN.match(lines[index])
        if match and match.group(1) == secret_name:
            secret_start = index
            break

    if secret_start is None:
        insert_at = section_end
        block = [entry_line, encrypted_line]
        lines[insert_at:insert_at] = block
    else:
        secret_end = find_secret_block_end(lines, secret_start, section_end)
        encrypted_index = None
        for index in range(secret_start + 1, secret_end):
            if lines[index].startswith("    encrypted:"):
                encrypted_index = index
                break
        if encrypted_index is None:
            lines[secret_start + 1 : secret_start + 1] = [encrypted_line]
        else:
            lines[encrypted_index] = encrypted_line

    new_text = "\n".join(lines) + "\n"
    yaml.safe_load(new_text)
    return new_text


def write_secret(target: SecretTarget, *, secret_name: str, encrypted_value: str) -> None:
    original = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
    updated = upsert_secret_text(original, secret_name=secret_name, encrypted_value=encrypted_value)
    target.path.write_text(updated, encoding="utf-8")


def main() -> int:
    args = parse_args()
    platform_file = platform_file_from_args(args)
    plugins_dir = plugins_dir_from_args(args)
    target = resolve_target(args, platform_file, plugins_dir)
    secret_name = (args.name or prompt_secret_name()).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", secret_name):
        raise SystemExit("Secret names must match [A-Z][A-Z0-9_]*")
    secret_value = args.value if args.value is not None else prompt_secret_value()
    identity = identity_from_args(args)

    try:
        public_key = derive_age_public_key(identity=identity)
        encrypted_value = encrypt_secret(secret_value, public_key=public_key)
    except CryptoError as exc:
        raise SystemExit(str(exc)) from exc

    write_secret(target, secret_name=secret_name, encrypted_value=encrypted_value)
    print(f"Updated {secret_name} in {target.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
