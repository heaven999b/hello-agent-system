"""连通性自检：python -m agentkit.check"""

from __future__ import annotations

import sys
import time

from .config import env, load_dotenv


def main() -> int:
    loaded = load_dotenv()
    print(f"配置文件：{loaded or '未找到 .env（将只使用环境变量）'}")
    print(f"LLM_BASE_URL = {env('LLM_BASE_URL')}")
    print(f"LLM_MODEL    = {env('LLM_MODEL', 'gpt-5.5')}")
    print(f"LLM_API_KEY  = {'已设置' if env('LLM_API_KEY') else '❌ 未设置'}")
    from .llm import default_llm

    try:
        llm = default_llm()
        t0 = time.time()
        r = llm.chat([{"role": "user", "content": "只回复两个字：你好"}])
        print(f"✅ 模型可用：{r.model}，回复「{(r.content or '').strip()}」，耗时 {time.time() - t0:.1f}s，tokens={r.usage.total}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 调用失败：{e}")
        return 1

    from .llm import call_tool  # noqa: F401  确认工具调用能力
    tools = [{"type": "function", "function": {"name": "ping", "description": "测试工具", "parameters": {"type": "object", "properties": {}}}}]
    r = llm.chat([{"role": "user", "content": "请调用 ping 工具"}], tools=tools)
    print("✅ 支持工具调用" if r.tool_calls else "⚠️ 模型没有发起工具调用，请确认该模型支持 function calling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
