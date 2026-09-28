"""第 23 课 Demo 的任务：IT 工单分类（7 个类别，带 8 条"公司特有"的易错规则）。

为什么选这个任务：
- 答案可以精确判对错（分类），评分器便宜、确定，优化器不会被"评委口味"带偏；
- 有明显的改进空间：很多类别的边界是**公司自己的规定**（U 盘用不了归安全组、打印机一律归硬件组……），
  模型再强也不可能"天生知道"，只能从指令或示例里学 —— 这正是提示词优化能发挥作用的地方；
- 数据是手写的 60 条虚构工单，按 train / dev / test 各 20 条划分，每份都有 8 条易错工单（每条规则 1 条）
  和 12 条常规工单。划分是事先固定的，优化过程中**绝不**看 test（第 21 课：数据切分与泄漏）。

本文件还包含离线模式用的确定性"模拟模型" SimulatedLLM（--offline 时替代真实模型）。
"""

from __future__ import annotations

import hashlib
import json
import re

from agentkit.context import estimate_tokens
from agentkit.types import LLMResponse, Message, Usage

from optkit import OPRO_HEADER, REFLECT_HEADER, Example

LABELS = ["password_reset", "access_request", "vpn", "network", "hardware", "software", "security"]

TASK_DESCRIPTION = (
    "把员工提交的 IT 工单分到以下 7 个类别之一（每个类别对应一个处理团队）：\n"
    "password_reset（账号密码）、access_request（权限申请与回收）、vpn（远程访问）、network（办公网络）、"
    "hardware（硬件设备）、software（软件）、security（安全事件）。\n"
    "分错类别 = 工单被派给错误的团队，平均要多等半天。"
)

# 基线：一个"能用但偷懒"的指令 —— 只列了类别名，没有任何边界说明。这是很多团队第一版 prompt 的真实样子。
BASELINE_INSTRUCTION = (
    "你是 IT 服务台的工单分类器。请把工单分到下列类别之一：\n"
    "password_reset, access_request, vpn, network, hardware, software, security"
)

# 固定的输出格式契约：优化器不能改它（见 optkit.Program 的说明）
OUTPUT_FORMAT = "输出格式：先用一句话说明判断理由，然后单独一行写 `类别：<类别名>`，类别名必须是上面列出的英文名之一。"
INPUT_PREFIX = "工单："

# ---------------------------------------------------------------- 8 条公司特有的易错规则
# 这些规则都是"这家公司自己的分工"，和常识不一定一致 —— 所以模型只能从指令、示例或反馈里学到。
# rule_id -> (标注备注 note, 模型不知道这条规则时最可能给出的"直觉"答案 —— 只给离线模拟用)
RULES: dict[str, tuple[str, str]] = {
    "T1_U盘": ("公司用数据防泄漏（DLP）策略禁用了 U 盘、移动硬盘等 USB 存储；这类设备用不了或需要例外审批，一律归 security，不归 hardware。", "hardware"),
    "T2_打印机": ("打印机相关问题（找不到打印机、驱动、打印任务卡住）统一由桌面硬件组处理，归 hardware。", "network"),
    "T3_许可证": ("软件许可证、席位的申请和续期走权限审批流程分配，归 access_request；software 只处理安装、升级、崩溃等技术问题。", "software"),
    "T4_VPN客户端": ("VPN 客户端的安装、升级、报错都由远程访问组负责，归 vpn，不归 software。", "software"),
    "T5_设备申领": ("新设备的申领（电脑、显示器、外设）走资产审批流程，归 access_request；hardware 只处理设备故障。", "hardware"),
    "T6_浏览器插件": ("浏览器插件/扩展要经过安全团队审核才能进白名单，申请开通或安装被拦截都归 security。", "software"),
    "T7_MFA换机": ("更换手机后 MFA/验证器需要重新绑定，由身份认证组处理，归 password_reset。", "security"),
    "T8_会议室投屏": ("会议室无线投屏依赖会议室专用无线网络，由网络组负责，归 network；只有屏幕、线缆等设备本身损坏才归 hardware。", "hardware"),
}
RULE_LABEL = {
    "T1_U盘": "security",
    "T2_打印机": "hardware",
    "T3_许可证": "access_request",
    "T4_VPN客户端": "vpn",
    "T5_设备申领": "access_request",
    "T6_浏览器插件": "security",
    "T7_MFA换机": "password_reset",
    "T8_会议室投屏": "network",
}


def _trap(i: str, rule: str, text: str) -> Example:
    return Example(text, RULE_LABEL[rule], id=i, note=RULES[rule][0], tag=rule)


def _easy(i: str, label: str, text: str) -> Example:
    return Example(text, label, id=i, tag="常规")


# 所有工单均为虚构。每份 20 条 = 8 条易错 + 12 条常规，易错和常规交错排列。
TRAIN = [
    _trap("tr01", "T1_U盘", "U 盘插上电脑没反应，在别的电脑上能用，我急着拷资料给客户。"),
    _easy("tr02", "password_reset", "忘记域账号密码了，登录不了电脑。"),
    _easy("tr03", "access_request", "申请开通财务共享盘 fin-share 的只读权限，经理已同意。"),
    _trap("tr04", "T2_打印机", "三楼的打印机在我电脑上找不到了，添加打印机时搜不到设备。"),
    _easy("tr05", "network", "工位的网口插上网线没反应，指示灯也不亮。"),
    _easy("tr06", "hardware", "笔记本屏幕出现一条竖线，而且越来越宽。"),
    _trap("tr07", "T3_许可证", "部门的 Adobe Acrobat 许可证到期了，编辑 PDF 时提示需要激活。"),
    _easy("tr08", "software", "Excel 一打开大文件就崩溃，提示'已停止工作'。"),
    _easy("tr09", "vpn", "VPN 连接时提示'证书已过期'，连不上。"),
    _trap("tr10", "T4_VPN客户端", "新电脑安装 VPN 客户端时报错 'installation failed 1603'。"),
    _easy("tr11", "security", "收到一封冒充 CEO 的邮件，让我紧急去买购物卡，是不是诈骗？"),
    _easy("tr12", "password_reset", "账号输错密码太多次被锁定了，请帮忙解锁。"),
    _trap("tr13", "T5_设备申领", "申请一台 27 寸的外接显示器，经理已经同意了。"),
    _easy("tr14", "access_request", "新同事李娜下周一入职，需要开通邮箱和企业微信账号。"),
    _easy("tr15", "network", "5 楼的无线网今天特别慢，网页半天打不开。"),
    _trap("tr16", "T6_浏览器插件", "想在 Chrome 里装一个翻译插件，应用商店提示'已被管理员禁止'。"),
    _easy("tr17", "hardware", "外接显示器没有信号，换了线也不行。"),
    _trap("tr18", "T7_MFA换机", "换了新手机，旧手机上的验证器没了，现在登录要验证码进不去。"),
    _easy("tr19", "software", "Chrome 浏览器一点开就闪退，重启电脑也没用。"),
    _trap("tr20", "T8_会议室投屏", "会议室的无线投屏连不上，电视上一直显示'等待连接'。"),
]

DEV = [
    _trap("dv01", "T2_打印机", "新电脑没装打印机驱动，打印时提示找不到驱动程序。"),
    _easy("dv02", "password_reset", "密码过期了，系统让我改密码，但一直提示不符合规则。"),
    _easy("dv03", "access_request", "需要 GitLab 上 payment-service 仓库的开发者权限。"),
    _trap("dv04", "T1_U盘", "移动硬盘插到笔记本上，提示'此设备已被策略阻止'。"),
    _easy("dv05", "network", "会议室的有线网络连不上，显示'无网络访问'。"),
    _easy("dv06", "hardware", "笔记本电池鼓包了，后盖都被顶起来了。"),
    _trap("dv07", "T8_会议室投屏", "用 AirPlay 往会议室大屏投屏，一直搜不到设备。"),
    _easy("dv08", "software", "Outlook 每次启动都卡在'正在加载配置文件'。"),
    _easy("dv09", "vpn", "VPN 连上之后几分钟就自动断开，反复如此。"),
    _trap("dv10", "T3_许可证", "我需要一个 Figma 的编辑席位，现在只能查看。"),
    _easy("dv11", "security", "我好像在一个假冒的登录页面输入了公司密码。"),
    _easy("dv12", "password_reset", "早上登录 OA 显示'账号已锁定'。"),
    _trap("dv13", "T6_浏览器插件", "能不能帮我开通一下 Grammarly 浏览器扩展？现在装不上。"),
    _easy("dv14", "access_request", "请给实习生王小明开通 HR 系统的查看权限，经理已审批。"),
    _easy("dv15", "network", "整个 3 楼的 Wi-Fi 信号都很弱，经常掉线。"),
    _trap("dv16", "T5_设备申领", "我的笔记本已经用了五年，想申请换一台新电脑。"),
    _easy("dv17", "hardware", "键盘有几个键失灵，按了没反应。"),
    _trap("dv18", "T4_VPN客户端", "GlobalProtect 升级之后打不开了，重装也失败。"),
    _easy("dv19", "software", "想把 Office 从 2019 升级到最新版。"),
    _trap("dv20", "T7_MFA换机", "手机屏幕摔坏了，Authenticator 打不开，登录邮箱时过不了二次验证。"),
]

TEST = [
    _trap("ts01", "T3_许可证", "Visio 打开提示'未授权的产品'，我需要一个许可证。"),
    _easy("ts02", "password_reset", "忘记了邮箱密码，自助重置页面收不到验证码。"),
    _easy("ts03", "access_request", "请开通 Tableau 数据看板的查看权限，我是新来的分析师。"),
    _trap("ts04", "T7_MFA换机", "我的 MFA 验证器绑在旧手机上，旧手机已经回收了，怎么重新绑定？"),
    _easy("ts05", "network", "办公室网络从中午开始时断时续。"),
    _easy("ts06", "hardware", "鼠标滚轮失灵，换了电池也不行。"),
    _trap("ts07", "T1_U盘", "想用 U 盘把设计稿拷走带去展会，电脑识别不了 U 盘。"),
    _easy("ts08", "software", "Teams 更新之后一直闪退。"),
    _easy("ts09", "vpn", "VPN 连接速度特别慢，传个文件要半小时。"),
    _trap("ts10", "T5_设备申领", "新来的设计师需要一块数位板和一台 Mac，怎么申请？"),
    _easy("ts11", "security", "有人用我的账号在凌晨从国外 IP 登录了，我收到了告警邮件。"),
    _easy("ts12", "password_reset", "连续输错密码，Windows 登录界面提示账户已被锁定。"),
    _trap("ts13", "T2_打印机", "打印机显示在线，但我这边发送的打印任务一直在排队，不出纸。"),
    _easy("ts14", "access_request", "需要把我加到 product-team 邮件组和对应的 Confluence 空间。"),
    _easy("ts15", "network", "我工位的网线接口好像坏了，换了电脑也上不了网。"),
    _trap("ts16", "T4_VPN客户端", "Mac 上安装 VPN 客户端被系统拦截，提示'无法验证开发者'。"),
    _easy("ts17", "hardware", "笔记本的充电器坏了，插上电源不充电。"),
    _trap("ts18", "T8_会议室投屏", "客户来开会，笔记本无线投屏到会议室电视总是断断续续。"),
    _easy("ts19", "software", "需要安装 Zoom 的 Outlook 插件，但软件中心里找不到。"),
    _trap("ts20", "T6_浏览器插件", "我需要用一个 JSON 格式化的浏览器插件，安装时被拦截了。"),
]


# ---------------------------------------------------------------- 解析、评分、反馈

_LABEL_RE = re.compile(r"(?<![a-z_])(" + "|".join(LABELS) + r")(?![a-z_])")


def parse_label(text: str) -> str:
    """优先取 `类别：xxx` 那一行；没有就取全文**最后**出现的类别名（理由里可能先提到别的类别）。"""
    text = (text or "").lower()
    for line in reversed(text.splitlines()):
        if "类别" in line:
            found = _LABEL_RE.findall(line)
            if found:
                return found[-1]
    found = _LABEL_RE.findall(text)
    return found[-1] if found else "unknown"


def metric(ex: Example, output: str) -> float:
    return 1.0 if parse_label(output) == ex.label else 0.0


def feedback(ex: Example, output: str) -> str:
    """GEPA 式的文字反馈：不只说对错，还带上标注备注（规则是什么）。"""
    pred = parse_label(output)
    if pred == ex.label:
        return f"正确（{ex.label}）。"
    fb = f"错误：标准答案是 {ex.label}，模型给出的是 {pred}。"
    return fb + (f"标注备注：{ex.note}" if ex.note else "")


def exemplar_line(ex: Example) -> str:
    """OPRO 元提示词里的任务样例：只有输入和答案，没有备注。"""
    return f"工单：{ex.input} → 类别：{ex.label}"


def format_verifier(output: str) -> float:
    """一个便宜但很弱的验证器：只检查"输出能不能解析出合法类别"。"""
    return 1.0 if parse_label(output) in LABELS else 0.0


# =====================================================================
# 离线模式：确定性的"模拟模型"
# =====================================================================
#
# 它不是真模型，而是一个把本课要讲的现象**显式写出来**的玩具：
#   - 分类：没学过某条公司规则时，按"直觉"答（易错工单大多答错）；指令或示例里出现了这条规则，就大概率答对；
#   - 每次回答都带一点和提示词相关的确定性"噪声"（哈希值）—— 换一个措辞，个别样本的对错就会变。
#     这正是真实模型的样子，也是"在 20 条 dev 上挑最高分 → test 上回落"（赢家诅咒）的来源；
#   - OPRO 优化器：只看到总分，于是**随机**地往最好的指令里加一句话（可能是有用的规则、没用的废话，
#     也可能是"凡是提到密码都归 password_reset"这种过度概括的坏规则）；
#   - GEPA 反思器：能看到失败样本的标注备注，于是**针对性地**补上对应的规则。
# 所有随机性都来自提示词内容的哈希，同样的输入永远得到同样的输出。

# 规则在提示词里被"教过"的判据：出现了这些关键词之一
_RULE_KEYWORDS = {
    "T1_U盘": ("U 盘", "USB", "DLP"),
    "T2_打印机": ("打印机",),
    "T3_许可证": ("许可证", "席位"),
    "T4_VPN客户端": ("VPN 客户端",),
    "T5_设备申领": ("申领", "资产审批"),
    "T6_浏览器插件": ("插件", "扩展"),
    "T7_MFA换机": ("验证器", "MFA"),
    "T8_会议室投屏": ("投屏",),
}
# 模拟优化器会写出的"坏规则"：(工单里的触发词, 强行归到的类别, 规则原文)
_BAD_RULES = [
    ("密码", "password_reset", "凡是工单里提到“密码”的，一律归 password_reset。"),
    ("安装", "software", "凡是涉及“安装”的工单，一律归 software。"),
]
_NEUTRAL = [
    "请逐字阅读工单，抓住用户真正要解决的问题。",
    "如果工单同时涉及多个问题，按最影响工作的那个分类。",
    "先想清楚这张工单应该由哪个团队处理，再给出类别。",
]
_CONFUSE = {  # 常规工单偶尔答错时，最可能错成的类别
    "password_reset": "access_request",
    "access_request": "password_reset",
    "vpn": "network",
    "network": "vpn",
    "hardware": "software",
    "software": "hardware",
    "security": "software",
}
# 没学过规则时"碰巧答对"的概率：有两条规则和常识一致（VPN 客户端归 vpn、无线投屏归 network），
# 真实模型不用教也大多能答对；其余 6 条和常识相反，基本都会答错。这组数字参考了 gpt-5.5 在训练集上的实测表现。
_PRIOR = {"T4_VPN客户端": 0.8, "T8_会议室投屏": 0.8}
_BY_TEXT = {ex.input: ex for ex in TRAIN + DEV + TEST}


def _u(*parts: str) -> float:
    """把若干字符串哈希成 [0, 1) 之间的数：确定性的"随机数"。"""
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16) / 16**12


def _rule_sentence(rule: str) -> str:
    return RULES[rule][0]


class SimulatedLLM:
    """离线模式的确定性模拟模型（实现 agentkit 的 LLM 接口：`await chat(messages) -> LLMResponse`）。

    chat 是 async 的，但里面没有 await：一次调用从开始到返回不会被别的协程打断。
    所以哪怕调用方并发地发请求，"同一个请求第几次出现"的计数也按发出的顺序递增，结果完全确定。
    它不模拟延迟（离线 Demo 要几秒跑完）；要看并发对延迟的影响，用 ScriptedLLM(latency=...)（Demo 场景 6）。
    """

    def __init__(self, model: str = "sim-model"):
        self.model = model
        self._seen: dict[str, int] = {}  # 同一个请求第几次出现：让"重复采样"得到不同的样本

    async def chat(self, messages: list[Message], tools=None, **kwargs) -> LLMResponse:
        text_all = "\n".join(m.get("content") or "" for m in messages)
        key = hashlib.sha256(text_all.encode("utf-8")).hexdigest()
        k = self._seen.get(key, 0)
        self._seen[key] = k + 1
        last = messages[-1].get("content") or ""
        if last.startswith(OPRO_HEADER):
            content, out_tokens = self._opro(last), 160
        elif last.startswith(REFLECT_HEADER):
            content, out_tokens = self._reflect(last), 220
        else:
            content, out_tokens = self._classify(messages, k), 40
        return LLMResponse(content=content, usage=Usage(estimate_tokens(messages), out_tokens), model=self.model)

    # ---------------- 分类
    def _classify(self, messages: list[Message], k: int) -> str:
        ticket = (messages[-1].get("content") or "").removeprefix(INPUT_PREFIX)
        ex = _BY_TEXT.get(ticket)
        system = messages[0].get("content") or ""
        demo_msgs = messages[1:-1]
        demo_inputs = [m["content"].removeprefix(INPUT_PREFIX) for m in demo_msgs if m["role"] == "user"]
        demo_outputs = [m["content"] for m in demo_msgs if m["role"] == "assistant"]
        u = _u(system, *demo_inputs, ticket, str(k))
        if ex is None:
            return f"无法判断。\n类别：{LABELS[int(u * len(LABELS))]}"

        for trigger, bad_label, sentence in _BAD_RULES:  # 坏规则优先生效：过度概括会覆盖正确判断
            if sentence in system and trigger in ticket and u < 0.85:
                return f"指令要求：{sentence}\n类别：{bad_label}"

        if ex.tag == "常规":
            err = 0.03 if len(demo_inputs) >= 2 else 0.06
            label = ex.label if u >= err else _CONFUSE[ex.label]
            return f"这是一个常见的 {label} 类问题。\n类别：{label}"

        rule = ex.tag
        taught_by_instruction = any(w in system for w in _RULE_KEYWORDS[rule])
        taught_by_demo = any(
            _BY_TEXT.get(i) is not None and _BY_TEXT[i].tag == rule and f"类别：{RULE_LABEL[rule]}" in o
            for i, o in zip(demo_inputs, demo_outputs)
        )
        p_right = 0.92 if taught_by_instruction else 0.80 if taught_by_demo else _PRIOR.get(rule, 0.15)
        if u < p_right:
            return f"按公司规定：{_rule_sentence(rule)}\n类别：{ex.label}"
        naive = RULES[rule][1]
        return f"看起来是 {naive} 相关的问题。\n类别：{naive}"

    # ---------------- OPRO 优化器：只看得到总分 → 随机加一句话
    def _opro(self, prompt: str) -> str:
        blocks = re.findall(r"<指令>\n(.*?)\n</指令>\n得分：(\d+)", prompt, re.S)
        best = max(blocks, key=lambda b: int(b[1]))[0] if blocks else BASELINE_INSTRUCTION
        n = int(re.search(r"请写出 (\d+) 条新的指令", prompt).group(1))
        pool = [_rule_sentence(r) for r in RULES if not any(w in best for w in _RULE_KEYWORDS[r])]
        pool += [s for _, _, s in _BAD_RULES if s not in best] + [s for s in _NEUTRAL if s not in best]
        out, used = [], set()
        for i in range(n):
            choices = [s for s in pool if s not in used] or pool
            s = choices[int(_u(prompt, "opro", str(i)) * len(choices))]
            used.add(s)
            out.append(f"{best}\n{s}")
        return json.dumps({"instructions": out}, ensure_ascii=False)

    # ---------------- GEPA 反思器：读失败样本的反馈 → 针对性地补规则
    def _reflect(self, prompt: str) -> str:
        current = re.search(r"<指令>\n(.*?)\n</指令>", prompt, re.S).group(1)
        failures = re.findall(r"（❌.*?评分反馈：(.*?)(?:\n\n###|\n\n请你)", prompt, re.S)
        added, found = [], []
        for fb in failures:
            for rule, words in _RULE_KEYWORDS.items():
                note = RULES[rule][0]
                if note in fb and not any(w in current for w in words) and rule not in found:
                    found.append(rule)
                    added.append(_rule_sentence(rule))
        if not added:  # 失败样本没有备注（常规工单上的偶发错误）：反思不出规则，只能加一句泛泛的话
            added = [s for s in _NEUTRAL if s not in current][:1] or [_NEUTRAL[0]]
            diagnosis = "失败样本没有明显的共同规律，可能是偶发错误。"
        else:
            diagnosis = "失败都来自公司特有的类别边界：" + "；".join(found)
        return json.dumps({"diagnosis": diagnosis, "instruction": current + "\n" + "\n".join(added)}, ensure_ascii=False)
