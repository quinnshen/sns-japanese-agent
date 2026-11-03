"""Store an API key in a private local file without echoing it or passing it as an argument."""

from getpass import getpass
import sys

from config import update_local_config


def main() -> None:
    if not sys.stdin.isatty():
        raise SystemExit("请在交互式终端中运行此脚本，密钥输入将隐藏。")
    key = getpass("粘贴 OpenAI API Key（输入时不会显示），然后按回车：").strip()
    if not key:
        raise SystemExit("未输入密钥；没有修改配置。")
    update_local_config({"OPENAI_API_KEY": key})
    print("已更新本机 .env.local，其他配置已保留。启用 ENABLE_ENRICHMENT 后重启后端即可。")


if __name__ == "__main__":
    main()
