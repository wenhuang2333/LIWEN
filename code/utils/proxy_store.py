"""反向代理规则的持久化入口（界面与 CLI 共用）。

真正的实现与校验在 :mod:`network.reverse_proxy`（``ProxyRuleStore`` /
``normalize_rule``）；本模块只提供**进程内单例**与稳定的导入路径，
避免界面层直接依赖网络层的细节。

存储位置：``%APPDATA%/LIWENTool/proxy_rules.json``
（macOS ``~/Library/Application Support/LIWENTool/``，
Linux ``$XDG_DATA_HOME/LIWENTool/``），经 :mod:`utils.jsonstore` 原子写入。
"""

from network.reverse_proxy import ProxyRuleStore, RuleError, normalize_rule

__all__ = ["ProxyRuleStore", "RuleError", "normalize_rule", "proxy_rule_store"]

#: 进程内单例（界面与 CLI 共用；规则文件损坏时逐条跳过，绝不抛异常）
proxy_rule_store = ProxyRuleStore()
