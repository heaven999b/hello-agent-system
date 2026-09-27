"""PromptRegistry：把 prompt 当成"可发布的制品"来管理（第 16 课 问题 1、2）。

一个 prompt 版本 = 模板 + 固定的模型版本 + 调用参数。三者一起决定 Agent 的行为，所以一起版本化、一起发布、一起回滚。

核心规则：
  1. 版本不可变：发布后永远不改。要改就发新版本（v3），旧版本（v2）原样保留 —— 回滚才有东西可回；
  2. 每个版本都带"谁、为什么、改了什么"（author / change_note / content_hash）—— 出事时第一个问题就是"最近改了什么"；
  3. 流量怎么分配是另一件事：stable（全量版本）+ candidate（灰度版本）+ percent（灰度比例）；
  4. 分流按用户 id 稳定哈希：同一个用户每次都落在同一个版本（粘性），扩量时老的灰度用户不会被"甩回"旧版本；
  5. 每一次发布、扩量、回滚都写审计日志。

这是内存实现。生产中：版本存在 git（评审 + CI 评估门禁）或数据库里，流量配置由配置中心下发，见 README 问题 1。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Callable


def bucket(user_id: str, salt: str) -> int:
    """把 user_id 稳定地映射到 0-99 的桶。同一 (user_id, salt) 在任何进程、任何机器上结果都相同。

    - 用 sha256 而不是内置 hash()：后者每个进程随机加盐（PYTHONHASHSEED），重启后用户就会换桶；
    - salt 通常取"这次发布的名字"：不同发布之间互相独立 —— 否则永远是同一批 1% 的用户在当小白鼠；
    - 取哈希的前 8 字节（64 位）再对 100 取模：取模带来的偏差约为 100 / 2^64，可以忽略。
    """
    digest = hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version: int
    template: str
    model: str  # 固定的模型版本（带日期的快照名），不要用会自动升级的别名
    params: dict
    author: str
    change_note: str
    created_at: float
    content_hash: str  # 模板 + 模型 + 参数的指纹：两个版本号不同但内容相同？一眼就能看出来

    def render(self, **variables) -> str:
        return self.template.format(**variables) if variables else self.template


@dataclass
class Rollout:
    stable: int
    candidate: int | None = None
    percent: int = 0  # 0-100：多少比例的用户走 candidate
    salt: str = ""
    force_candidate: frozenset[str] = frozenset()  # 内部员工、测试账号：先"吃自己的狗粮"
    force_stable: frozenset[str] = frozenset()  # 明确不参与实验的客户（比如合同要求、正在做关键演示）


def pick_version(user_id: str, rollout: Rollout) -> int:
    """决定这个用户用哪个版本。优先级：没有灰度 > 强制稳定 > 强制灰度 > 哈希分桶。

    percent=0 时哈希分桶谁都进不了灰度，但 force_candidate 里的内部员工仍然会拿到新版本 ——
    这就是灰度的"第 0 阶段"：先只给自己人用。
    """
    if rollout.candidate is None:
        return rollout.stable
    if user_id in rollout.force_stable:
        return rollout.stable
    if user_id in rollout.force_candidate:
        return rollout.candidate
    return rollout.candidate if bucket(user_id, rollout.salt) < rollout.percent else rollout.stable


def _fingerprint(template: str, model: str, params: dict) -> str:
    raw = json.dumps({"t": template, "m": model, "p": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


class PromptRegistry:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._versions: dict[str, list[PromptVersion]] = {}
        self._rollouts: dict[str, Rollout] = {}
        self._stable_history: dict[str, list[int]] = {}  # 历任 stable 版本，回滚时用
        self.audit_log: list[dict] = []

    # ------------------------------------------------------------ 版本

    def publish(self, name: str, template: str, *, model: str, author: str, change_note: str, params: dict | None = None) -> PromptVersion:
        """发布一个新版本（只是"登记"，不接任何流量）。第一个版本自动成为 stable。"""
        if not change_note.strip():
            raise ValueError("change_note 不能为空：三个月后没人记得这次为什么改")
        params = dict(params or {})
        history = self._versions.setdefault(name, [])
        v = PromptVersion(
            name=name,
            version=len(history) + 1,
            template=template,
            model=model,
            params=params,
            author=author,
            change_note=change_note,
            created_at=self.clock(),
            content_hash=_fingerprint(template, model, params),
        )
        history.append(v)
        self._audit("publish", name, author, version=v.version, note=change_note, hash=v.content_hash)
        if v.version == 1:
            self._rollouts[name] = Rollout(stable=1)
            self._stable_history[name] = [1]
        return v

    def get(self, name: str, version: int) -> PromptVersion:
        return self._versions[name][version - 1]

    def history(self, name: str) -> list[PromptVersion]:
        return list(self._versions.get(name, []))

    def rollout(self, name: str) -> Rollout:
        return self._rollouts[name]

    # ------------------------------------------------------------ 流量

    def start_rollout(self, name: str, candidate: int, percent: int, *, actor: str,
                      force_candidate: frozenset[str] = frozenset(), force_stable: frozenset[str] = frozenset()) -> Rollout:
        r = self._rollouts[name]
        if r.candidate is not None:
            raise RuntimeError(f"{name} 已经有进行中的灰度（v{r.candidate}），先推全或回滚")
        self.get(name, candidate)  # 版本必须存在
        new = Rollout(stable=r.stable, candidate=candidate, percent=_check_percent(percent),
                      salt=f"{name}:v{candidate}", force_candidate=frozenset(force_candidate),
                      force_stable=frozenset(force_stable))
        self._rollouts[name] = new
        self._audit("start_rollout", name, actor, candidate=candidate, percent=percent)
        return new

    def set_percent(self, name: str, percent: int, *, actor: str, reason: str) -> Rollout:
        r = self._rollouts[name]
        if r.candidate is None:
            raise RuntimeError(f"{name} 没有进行中的灰度")
        old = r.percent
        r.percent = _check_percent(percent)
        self._audit("set_percent", name, actor, candidate=r.candidate, percent=percent, previous=old, reason=reason)
        return r

    def promote(self, name: str, *, actor: str, reason: str = "灰度指标达标") -> Rollout:
        """推全：candidate 成为新的 stable，灰度结束。"""
        r = self._rollouts[name]
        if r.candidate is None:
            raise RuntimeError(f"{name} 没有进行中的灰度")
        self._rollouts[name] = Rollout(stable=r.candidate)
        self._stable_history[name].append(r.candidate)
        self._audit("promote", name, actor, version=r.candidate, previous=r.stable, reason=reason)
        return self._rollouts[name]

    def rollback(self, name: str, *, actor: str, reason: str) -> Rollout:
        """回滚。有进行中的灰度 → 终止灰度，所有人回到 stable；
        没有灰度 → stable 退回上一任（撤销最近一次推全）。

        回滚只是"移动指针"：不删除、不修改任何版本，所以回滚本身也可以被回滚。
        """
        r = self._rollouts[name]
        if r.candidate is not None:
            self._rollouts[name] = Rollout(stable=r.stable)
            self._audit("rollback", name, actor, aborted=r.candidate, back_to=r.stable, reason=reason)
        else:
            hist = self._stable_history[name]
            if len(hist) < 2:
                raise RuntimeError(f"{name} 没有更早的 stable 版本可以回滚")
            bad = hist.pop()
            self._rollouts[name] = Rollout(stable=hist[-1])
            self._audit("rollback", name, actor, aborted=bad, back_to=hist[-1], reason=reason)
        return self._rollouts[name]

    def resolve(self, name: str, user_id: str) -> PromptVersion:
        """线上每个请求调用它：这个用户此刻应该用哪个版本。"""
        return self.get(name, pick_version(user_id, self._rollouts[name]))

    # ------------------------------------------------------------ 审计

    def _audit(self, action: str, name: str, actor: str, **detail) -> None:
        self.audit_log.append({"ts": self.clock(), "action": action, "prompt": name, "actor": actor, **detail})

    def export(self) -> dict:
        """导出为 JSON 友好的结构（可以存档、做 diff、给配置中心下发）。"""
        return {
            "versions": {n: [asdict(v) for v in vs] for n, vs in self._versions.items()},
            "rollouts": {n: {**asdict(r), "force_candidate": sorted(r.force_candidate), "force_stable": sorted(r.force_stable)}
                         for n, r in self._rollouts.items()},
            "audit_log": self.audit_log,
        }


def _check_percent(p: int) -> int:
    if not 0 <= p <= 100:
        raise ValueError(f"percent 必须在 0-100 之间，收到 {p}")
    return int(p)
