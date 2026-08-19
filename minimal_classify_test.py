#!/usr/bin/env python3
"""最小验证脚本：确认 SquillaRouter V4Phase3Strategy.classify 可独立调用。

用法:
    python3 minimal_classify_test.py
"""

import asyncio
import sys

from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

VALID_TIERS = ["c0", "c1", "c2", "c3"]


async def main() -> None:
    print("== 初始化 V4Phase3Strategy ==")
    strategy = V4Phase3Strategy()
    print(f"bundle_dir = {strategy.bundle_dir}")
    print(f"available  = {strategy._available}")
    print(f"model_version = {strategy._model_version}")
    if not strategy._available:
        print("!! 模型不可用，退出", file=sys.stderr)
        sys.exit(1)

    cases = [
        ("简单", "1+1=?"),
        ("简单", "你好，今天天气怎么样"),
        ("中等", "帮我优化这段 Python 代码：\ndef f(x):\n    return x*2"),
        ("复杂", "写一个完整的 B 树实现，包含插入、删除、查找，并解释时间复杂度"),
        ("复杂", "设计一个分布式任务调度系统架构，要求高可用和水平扩展"),
    ]

    print("\n== classify 实测 ==")
    for label, message in cases:
        tier, confidence, source, extra = await strategy.classify(
            message, valid_tiers=VALID_TIERS
        )
        print(f"\n[{label}] {message[:50]!r}")
        print(f"  tier={tier} confidence={confidence:.3f} source={source}")
        print(f"  route_class={extra.get('route_class')} "
              f"difficulty={extra.get('difficulty')} "
              f"thinking_mode={extra.get('thinking_mode')} "
              f"prompt_policy={extra.get('prompt_policy')}")


if __name__ == "__main__":
    asyncio.run(main())
