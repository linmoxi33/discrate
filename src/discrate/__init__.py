"""discrate —— 把一堆命名混乱的音乐文件整理成整齐、可回滚的音乐库。

设计目标见 README.md「为什么需要它」一节。核心约定：

* 只读上游 beets 的**能力**（标签读取、文件名合法化、模板引擎），
  不使用 ``Item.destination()``：该方法在专辑型文件上会因
  ``singleton`` 对 ``Item`` 恒为真而选错模板（实测见 docs/UPSTREAM-NOTES.md）。
* 所有写操作都进 journal，可 ``undo`` 回滚。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
