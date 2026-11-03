"""Store an API key in a private local file without echoing it or passing it as an argument."""

from getpass import getpass
import os
import sys

from enrich import LOCAL_KEY_FILE


def main() -> None:
    if not sys.stdin.isatty():
        raise SystemExit("请在交互式终端中运行此脚本，密钥输入将隐藏。")
    key = getpass("粘贴 OpenAI API Key（输入时不会显示），然后按回车：").strip()
    if not key:
        raise SystemExit("未输入密钥；没有修改配置。")
    descriptor = os.open(LOCAL_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(key + "\n")
    print("已保存本机密钥。设置 ENABLE_ENRICHMENT=1 并启动后端服务即可开启翻译与解析。")


if __name__ == "__main__":
    main()
