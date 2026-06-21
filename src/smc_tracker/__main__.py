"""使 `python -m smc_tracker` 等价于 `smc_tracker` CLI。

示例：
  python -m smc_tracker run
  python -m smc_tracker report --hours 6
"""
from .cli import main

if __name__ == "__main__":
    main()
