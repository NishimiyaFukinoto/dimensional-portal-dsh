"""把仓库里的手机脚本模板生成为「带真实地址和 token」的本地脚本。

仓库里的 `phone/dimensional_portal_agent.js` 只有占位符（`http://100.x.y.z:8765` /
`改成你的token`），是为了开源脱敏。真正推到手机上跑的是这个脚本生成的 `.local.js`
（已在 .gitignore 里，不会被提交）。

用法：

    # 1) 从 dp.json 自动读 token（推荐），地址自己给
    python tools/make_phone_script.py --bases "100.88.64.39,10.172.89.162"

    # 2) 全部手给
    python tools/make_phone_script.py --token 你的token --bases "100.88.64.39,10.172.89.162"

地址可以只写 IP（端口默认用 dp.json 里的 `listen_port`），也可以写完整 URL。
第一个建议放 Tailscale 的 100.x，第二个放局域网 IP；脚本连不上会自动切换。

生成完推到手机：

    adb push phone/dimensional_portal_agent.local.js /storage/emulated/0/脚本/dimensional_portal_agent.js

查地址：`"C:\\Program Files\\Tailscale\\tailscale.exe" ip -4` 拿 100.x，`ipconfig` 拿局域网地址。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "phone" / "dimensional_portal_agent.js"
DEFAULT_OUT = ROOT / "phone" / "dimensional_portal_agent.local.js"
DP_JSON = ROOT / "dp.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def read_config() -> dict:
    if not DP_JSON.exists():
        return {}
    try:
        raw = json.loads(DP_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"读不了 {DP_JSON}：{exc}")
    return raw if isinstance(raw, dict) else {}


def normalize_bases(spec: str, default_port: int) -> list[str]:
    items = [s.strip() for s in re.split(r"[,\s]+", spec) if s.strip()]
    out: list[str] = []
    for item in items:
        if item.startswith(("http://", "https://")):
            url = item.rstrip("/")
        else:
            host = item
            port = default_port
            if ":" in item and not item.count(":") > 1:  # host:port，排除 IPv6 的裸写法
                host, _, port_s = item.partition(":")
                port = int(port_s) if port_s.isdigit() else default_port
            url = f"http://{host}:{port}"
        if url not in out:
            out.append(url)
    if not out:
        die("没给地址。用 --bases \"100.x.y.z,192.168.1.5\" 指定（第一个建议 Tailscale）。")
    return out


def main() -> int:
    cfg = read_config()
    parser = argparse.ArgumentParser(description="生成带真实配置的手机端脚本")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出文件（默认 phone/dimensional_portal_agent.local.js）")
    parser.add_argument("--bases", default="", help="电脑地址，逗号分隔，可只写 IP")
    parser.add_argument("--token", default="", help="手机共享密钥（默认读 dp.json 的 token）")
    parser.add_argument("--port", type=int, default=0, help="只写 IP 时用的端口（默认读 dp.json 的 listen_port）")
    args = parser.parse_args()

    token = (args.token or str(cfg.get("token") or "")).strip()
    if not token or token in {"改成你的token", "改成手机脚本里的 TOKEN"}:
        die(
            "缺少 token。\n"
            "  1) 先跑一次 tools/dp_daemon.py（会在 dp.json 里生成 token），或者\n"
            "  2) 用 --token 直接给。"
        )

    if not args.bases.strip():
        die(
            "缺少地址。用 --bases 指定手机能访问到的电脑地址，例如：\n"
            '  --bases "100.88.64.39,10.172.89.162"'
        )

    default_port = args.port or int(cfg.get("listen_port") or 8765)
    bases = normalize_bases(args.bases, default_port)

    if not TEMPLATE.exists():
        die(f"找不到模板：{TEMPLATE}")
    text = TEMPLATE.read_text(encoding="utf-8")

    # 替换 BASES 数组：从 "var BASES = [" 到对应的 "];"
    start = text.find("var BASES = [")
    if start < 0:
        die("模板里找不到 `var BASES = [`，模板改过了？")
    end = text.find("];", start)
    if end < 0:
        die("模板里 BASES 数组没有正常结束")
    comment = (
        "var BASES = [\n"
        + "".join(f'    "{url}",\n' for url in bases)
        + "]"
    )
    text = text[:start] + comment + text[end + 2 :]

    # 替换 TOKEN
    text, n = re.subn(r'var TOKEN = "[^"]*";', f'var TOKEN = "{token}";', text, count=1)
    if n != 1:
        die("模板里找不到 `var TOKEN = \"...\";`，模板改过了？")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(f"✅ 已生成 {out}")
    for url in bases:
        print(f"   {url}")
    print(f"   TOKEN = {token[:4]}…（{len(token)} 字符）")
    print()
    print("推到手机：")
    print(f'  adb push "{out}" /storage/emulated/0/脚本/dimensional_portal_agent.js')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
