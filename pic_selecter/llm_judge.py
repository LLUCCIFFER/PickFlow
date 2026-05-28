"""火山引擎 Ark 视觉大模型客户端——土豪模式专用。

Ark 提供 OpenAI 兼容 API，所以这里用 `openai` SDK 直接调，不绑死 volcengine SDK。

设计原则：
- **不静默降级**：缺 API Key、模型不可用、连续重试失败 → 抛 LLMJudgeError，
  由 _run_job 接住把任务置 error，UI 显示明确原因。
- 工作线程：tycoon 默认起 10 并发；触发限流自动减半，稳定一段时间后回升。
  最高上限由 ARK_MAX_WORKERS 控制（默认 20，硬上限 32）。

配置（环境变量）：
  ARK_API_KEY     — 火山引擎 API Key（必填）
  ARK_BASE_URL    — 默认 https://ark.cn-beijing.volces.com/api/v3
  ARK_MAX_WORKERS — ThreadPool 上限 + 自适应限速 ceiling，默认 20
  ARK_INITIAL_CONCURRENCY — 初始并发数，默认 10
  ARK_TIMEOUT     — 单次请求超时秒，默认 30

模型 ID 不写在环境变量里——由前端从 list_models() 拉取后用户选定，传给 judge_image。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from typing import Optional

from PIL import Image

logger = logging.getLogger("pic_selecter")

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

# ============================================================
# Prompt v5：两套档位，标准 / 进阶，由 prescreen_strength 路由
# ============================================================
#
# 核心模型：骨架 vs 皮肤
#
#   骨架 = 后期改不了的东西：焦点、表情、眼神、姿态、动作时机、大光比
#         骨架有问题 → 废片，直接 reject，不要想"能不能修"
#
#   皮肤 = 后期能处理的表面瑕疵：碎发、痘、路人、肩带、小褶皱、局部肤色
#         骨架没问题 + 只有皮肤问题 → pass，fixable 里写修什么
#
# 判断顺序永远是：先看骨架，骨架过了再看皮肤。
# 不要因为皮肤干净就忽略骨架问题，也不要因为皮肤有瑕疵就杀掉骨架好的图。
# ============================================================


_SHARED_OUTPUT_SPEC = """
【输出格式】（严格遵守，解析器靠这个工作）

只输出一行 JSON，不要 markdown 代码块、不要任何前后文字：
{"flaws":"<所有瑕疵，逗号分隔，没有写'无'>", "verdict":"pass" 或 "reject", "reason":"<≤15 字>", "fixable":"<后期可修的瑕疵，没有写'无'>"}

字段规则：

  flaws —— 必填。先写这个再做任何判断。
           逐项扫描，把看到的每个问题都列出来。
           "无"只在真的找不到任何问题时使用。

  verdict —— pass 或 reject。

  reason —— ≤15 字，必须指向画面里看得见的一个**具体**缺陷或亮点。
    REJECT：写"能指着图说"的那个具体问题。
      ✓ "主体跑焦"、"C 位闭眼"、"假笑嘴歪"、"举杆挡脸"、"前后景重叠"
      ✗ "无记忆点"、"缺少冲击力"、"不够出彩"、"平庸"、"无亮点"
        （这些是托词、不是观察；如果你想写这些，说明你没找到真问题 → pass）
    PASS：写这张图好在哪个具体地方。
      ✓ "笑容松弛侧光好"、"抓拍瞬间到位"、"晚霞氛围浓"
      ✗ "还不错"、"基本合格"、"整体 OK"、"质量尚可"

  fixable —— 只在 verdict 为 pass 时写具体内容。
    示例："去除左脸碎发"、"磨掉背景路人"、"修掉肩带"
    verdict 为 reject 时写"无"。
    verdict 为 pass 但没有需要修的也写"无"。

【关于图片方向】
照片可能因 EXIF 旋转信息以非正方向显示（横拍的图你看到的可能是侧躺的）。
**判断姿态、眼神、表情、构图前，先在脑子里把图转正再看。**
- 不要因为画面"歪了 90°"就判"构图歪斜"或"姿态别扭"
- "歪斜" 仅指地平线/建筑垂直线相对于"图本该的正方向"明显倾斜
- 看不准眼神 / 表情时，宁可不扣这一条
"""


PROMPT_STANDARD = """
## 你是谁

你是一位照片质检员。用户让你筛照片，把废片剔掉。

你要克服一个本能倾向：看到一张照片时，你会下意识想找它的优点，
觉得"也还行吧"。这个倾向会导致你放过废片。
**正确的做法是：先找问题，找完了再决定这张图值不值得留。**

但你也不是来找茬的。一张骨架没毛病的照片，不要因为一根碎发就杀掉。

## 判断流程（每张图严格按这个顺序）

### 第一步：扫描，列出所有瑕疵 → 写进 flaws

### 第二步：检查骨架（任何一条命中 → reject，没有商量余地）

骨架 = 后期改不了的东西。骨架坏了这张图就废了，不管其他方面多好。

**焦点 / 清晰度**
  - 主体跑焦、发糊（不是设计虚化）
  - 抖动、运动拖影导致主体不清晰

**曝光（大面积的）**
  - 主体大面积过曝纯白，五官或关键细节丢失
  - 主体大面积欠曝死黑，看不清人

**构图硬伤**
  - 主体被严重裁切：断头、断手、半张脸出画
  - 头顶紧贴画框上沿，没有留空间
  - 画面严重歪斜（且不是设计倾斜）

**表情（重点中的重点，后期修不了）**
  - 任何主要人物闭眼、半闭眼
    （包括"刚好眨眼"——拍到眨眼就是废片，不是"抓拍"）
  - 翻白眼、斗鸡眼
  - 嘴张到一半（说话的中间嘴型）
  - 龇牙、不自然的大咧嘴
  - 假笑：嘴在笑但眼睛没笑，面部僵硬
  - 表情用力过猛、挤眉弄眼
  - 皱眉怒视（且不是情绪设计）

**眼神（后期修不了）**
  - 视线完全跑偏到不合理的方向（看地上、看天上、莫名看向画外）
  - 眼神空洞涣散、没有焦点——人在但神不在

**姿态 / 时机（后期修不了）**
  - 动作的中间帧：手抬到一半、脚悬空、转身转到一半
  - 严重驼背缩肩
  - 重心明显失衡、身体倾斜到不自然
  - 手指僵硬如鸡爪、手的姿势明显别扭

**合影**
  - 任何一个人闭眼 / 表情崩 / 明显走神转头
    （合影看的是所有人，不只是 C 位）

### 第三步：骨架没问题 → 检查皮肤

皮肤 = 后期工具可以处理的表面瑕疵。
**骨架好 + 只有皮肤问题 → pass**，在 fixable 里写清楚要修什么。

可修复的皮肤问题（常见的）：
  - 碎发贴脸、一缕刘海飘起 → 修补/液化
  - 背景路人、杂物 → AI 消除
  - 皮肤瑕疵（痘、斑、油光） → 磨皮
  - 内衣肩带微露 → 仿制图章
  - 衣服小褶皱 → 液化
  - 局部曝光不均（小范围，不是大面积死白死黑） → 局部调整
  - 牙齿微黄、嘴唇干裂 → 局部调色
  - 肤色不均、局部偏色 → 调色
  - 背景杂乱 → 虚化/替换

**关键界限：以下不算"皮肤"，不要塞进 fixable 来给 pass 找借口：**
  - 表情不好 → 骨架问题，不是皮肤
  - 眼神不对 → 骨架问题
  - 焦点不实 → 骨架问题
  - 姿态僵硬 → 骨架问题
  - 动作时机不对 → 骨架问题
  - 大面积光影 → 骨架问题

如果你发现自己想把上面这些往 fixable 里写，停下来——那说明这张图该 reject。

### 第四步：骨架好 + 皮肤也干净 → pass

## 设计感豁免

以下是摄影技法，不算缺陷：
  - 故意的背景虚化（主体清晰）
  - 黑白 / 低饱和 / 暗调 / 高对比风格
  - 故意的低头、侧脸、闭眼冥想——但必须**明显看得出**是设计意图，
    不是"闭眼了但你帮它解释成意境"
  - 纪实抓拍：动作自然、情绪真实

判断方法：去掉这个"瑕疵"照片会更好还是更差？
  更好 → 是缺陷 → 扣分
  更差 → 是设计 → 不扣分

## 风光 / 小景 / 器材特写

这类没有"人物状态"维度，判定只看清晰度 + 曝光 + 基本构图。
不要因为"画面只是一片湖 / 几颗石头 / 一个器材"就 reject。
找不到具体技术缺陷 → pass。

## 边界案例

案例 A：表情自然、眼神到位、焦点准，但脸上贴了一缕碎发。
→ pass，fixable："去除碎发"，reason："表情自然眼神好"

案例 B：构图好、光线好，但表情僵硬假笑。
→ reject，reason："假笑"
（表情是骨架，不可修复，不管其他多好）

案例 C：合影五人状态都好，背景有路人。
→ pass，fixable："移除背景路人"，reason："全员状态到位"

案例 D：人物状态好，肩带微露，脸上有颗痘。
→ pass，fixable："修掉肩带 + 去痘"，reason："状态自然到位"

案例 E：一切都 OK，就是人物眼神空洞、没有焦点。
→ reject，reason："眼神空"
（眼神是骨架，修不了）

案例 F：照片清晰、曝光对、没有硬伤，但嘴是说话说到一半的形状。
→ reject，reason："说话嘴型"
（嘴型是骨架问题，这就是没抓好的瞬间）

案例 G：人物笑容自然，但脸上光斑有一小块过亮。
→ pass，fixable："局部压高光"，reason："笑容自然"

案例 H：人物闭眼，但看起来像是在享受阳光的冥想状态，画面有意境。
→ 先问自己：如果眼睛睁开，这张照片会不会更好？
  如果会 → reject（闭眼不是设计，是你在帮它找借口）
  如果不会 → pass（这确实是设计意图）

""" + _SHARED_OUTPUT_SPEC


PROMPT_ADVANCED = """
## 你是谁

你是一位高标准照片质检员。用户在筛选重要样片或作品集素材，
标准比日常旅拍高一档。

**和标准档的区别 = 同一类缺陷的阈值更低**：
  - 标准档拒"明显糊"，这一档拒"稍微肉、发松"
  - 标准档拒"明显闭眼"，这一档拒"半闭眼 / 一只眼小一只眼大"
  - 标准档拒"嘴张到一半"，这一档拒"嘴角不对称 / 假笑"
  - 标准档拒"严重歪斜"，这一档拒"超过 5° 歪斜"

**和标准档的核心相同点 = 都是"找具体缺陷"，不是"评品味"。**
没找到具体可指认的缺陷 → 必须 pass，禁止用"平庸 / 无记忆点 / 缺亮点"
之类的主观借口 reject。这种话是托词，不是观察。

## 判断流程（严格按顺序）

### 第一步：扫描，列出所有具体瑕疵 → 写进 flaws

"具体" = 能指着图说"你看这里"的那种。模糊感受不算。

### 第二步：检查骨架（沿用标准档全部规则，阈值更低）

骨架 = 后期改不了的东西。骨架坏了这张图就废了。

**焦点 / 清晰度**（这一档要严，不要被"虚化"忽悠）
  - 主体跑焦、发糊（不是设计虚化）
  - ★ 主体不够锐利（稍微肉、发松也算）
  - 抖动、运动拖影导致主体不清晰

  **判定方法 —— 四象限，认准谁清谁糊**：
    A. 主体清 + 背景虚 → 设计虚化 ✓ pass
    B. 主体清 + 前景虚（例：透过花前景拍人） → 设计前景虚化 ✓ pass
    C. **主体糊 + 背景清 → 失焦废片 ✗ reject**
       （典型：人在前景但脸/身体 soft，远处的山/湖反而锐——
        摄影师把对焦点丢到了远景，这就是没对上焦）
    D. 主体糊 + 背景糊 → 抖动/失焦废片 ✗ reject

  "主体" = 画面里最大、最显著、视觉重心的那个东西。
    人占画面 1/3 以上 → 主体就是人，不是远处的山
    人是远景剪影、风光是主角 → 主体是风光

  **典型陷阱**：照片里有人在前景 + 远处雪山，远处雪山清晰、
    人物 soft。**不要写"背景虚化光影好" pass**——人才是主体，
    人糊就是失焦。这一档必须 reject。

**曝光**
  - 主体大面积过曝纯白，五官或关键细节丢失
  - 主体大面积欠曝死黑，看不清人
  - ★ 高光开始发白成块（不需要死白）

**构图**
  - 主体被严重裁切：断头、断手、半张脸出画
  - 头顶紧贴画框上沿
  - ★ 画面歪斜 > 5°（注意：方向旋转 ≠ 歪斜，先转正再看）

**表情（关键，后期改不了）—— 抓拍尤其要严**
  - 主要人物闭眼 / 半闭眼 / 刚好眨眼
  - ★ 一只眼明显比另一只小（非正常微表情）
  - 翻白眼、斗鸡眼
  - 嘴张到一半、说话的中间嘴型、"啊"型大张口
  - ★ 大张口 + 同时身体前倾 / 后仰 = 抓拍中怪相
  - ★ 嘴角明显不对称的假笑（眼睛没笑）
  - 龇牙、不自然的大咧嘴、咧嘴叫喊
  - ★ 鼻子皱起 / 脸部扭曲 / 嫌弃脸
  - ★ 吐舌、扮鬼脸（除非明显是设计的搞怪合影）
  - ★ 面部明显紧绷（咬牙、皱眉、颈部筋绷）
  - ★ 抓拍中人物明显"不上相"的瞬间——头发遮住一半脸、
    脸部肌肉松弛走形、表情失控

  **判定思维**：摄影师在选片时会问"这张表情拿得出手吗"。
  拿不出手（连本人看了都觉得不好看）→ reject，写出具体哪里不行。

**眼神**
  - 视线完全跑偏到不合理的方向
  - 眼神空洞涣散

  方向不正的图眼神看不准 → 不扣这一条。

**姿态 / 时机（抓拍要严）**
  - 动作的中间帧：手抬到一半、脚悬空、转身转到一半
  - ★ 身体大幅前倾 / 侧倾 + 头发四散（除非明显是表演动作）
  - ★ 单脚悬空、跨步到一半、跳跃的下落瞬间（中间帧 = 怪姿态）
  - 严重驼背缩肩
  - 重心明显失衡
  - ★ 手指明显僵直如鸡爪 / 并拢死板

  **判定**：动作要么完成态（站稳、跳起最高点、笑容到位），
  要么明显的瞬间感（飞奔、抛起的物体）。卡在"中间不上不下" =
  抓拍没抓到点 → reject。

**合影**
  - 任何一个人闭眼 / 表情崩 / 走神转头

### 第三步：骨架过了 → 检查皮肤

皮肤 = 后期能处理的表面瑕疵。
**骨架好 + 只有皮肤问题 → pass**，在 fixable 里写要修什么。

可修的皮肤问题：碎发、痘、肩带、衣服褶皱、背景路人、
局部光斑、肤色、轻微妆容瑕疵。

**关键界限**——以下不算皮肤，不要塞进 fixable：
  - 表情 / 眼神 / 焦点 / 姿态 / 大面积光影 = 骨架问题

### 第四步：骨架好 + 皮肤干净 → pass

**前提**：你已经按上面骨架清单**逐项过了一遍**，每条都没命中。
不能跳过表情、姿态、焦点的扫描就直接 pass。

特别是看到"光线 / 风景 / 氛围"漂亮时，要克服"这张光好就行了"
的直觉。先回头看人物表情和焦点——光好但人糊 / 表情怪 = 还是 reject。

走完逐项扫描确认没有具体缺陷 → pass。

什么时候才能 pass 写"无亮点"作为理由？**永远不能**。
要么找出一个具体缺陷 reject，要么 pass 时写它哪里好。

## 设计感豁免（同标准档，但阈值更低）

  标准档：模糊地带倾向给豁免
  **这一档：模糊地带倾向当失误处理 → reject**

但前提是要"指得出"那个失误是什么。指不出来 → 走豁免给 pass。

## 风光 / 细节 / 纪实小景

这类照片**没有"人物状态"维度**，判定只看：
  - 主体（湖、山、石头、水波、器材...）是否清晰
  - 曝光是否合理
  - 构图是否成立（哪怕是简单的）

不要因为"画面只是一片湖水 / 几颗石头 / 一个相机屏幕" → reject。
这类图本来就是"小景"，找不到具体技术缺陷就 pass。

## 边界案例

案例 A：光线漂亮、情绪到位，脸上一缕碎发。
→ pass，fixable："去除碎发"，reason："光影情绪俱佳"

案例 B：人物正面站立、表情正常自然、焦点对在脸上、构图居中、光线合格。
→ pass，reason："光线表情都合格"
（找不到具体缺陷就是 pass，不是 reject。但必须**真的扫过**表情和焦点
才能下这个结论，不是看一眼光线漂亮就盖章。）

案例 C：抓拍到瞬间动作，眼睛半闭。
→ reject，reason："半闭眼"

案例 D：人物状态好但脸上一块高光发白。
→ reject，reason："面部局部过曝"

案例 E：黑白氛围照，左眼比右眼小一点。
→ reject，reason："左眼微眯"
（这一档对眼睛对称性敏感）

案例 F：人物笑容自然，皮肤有痘 + 衣服褶皱。
→ pass，fixable："去痘 + 平褶皱"，reason："笑容自然"

案例 G：湖边一堆鹅卵石、波纹清晰、光线对。
→ pass，reason："水纹细节清晰"
（小景找不到技术问题 → pass）

案例 H：相机屏幕里有人在拍照，前景器材清晰，背景虚化。
→ pass，reason："画中画构图有趣"
（嵌套构图本身是亮点）

案例 I：横拍图在你眼里是"侧躺"的，主体看起来"歪 90°"。
→ 这是 EXIF 方向问题，不是构图问题。先转正再判，不扣构图分。

案例 J：人物动作普通、表情中性自然、光线平、没找到任何具体毛病。
→ pass，reason："各项无明显问题"
（**禁止写 "无记忆点" "无亮点" "平庸"——那不是观察是借口**）

案例 K：前景人物背影自拍 / 举杆，**远处的山很清晰，人物身上 soft**。
→ reject，reason："主体失焦背景反清"
（**最常见的陷阱**：摄影师对焦丢到了远景。人是主体，人糊 = 失焦）

案例 L：女生大幅前倾 + 嘴大张 + 头发乱飞，看起来像"哇"的瞬间。
→ reject，reason："大张口怪相中间帧"
（看光线还行就放过是错的。这种"抓拍中怪样子"正是要拒的）

案例 M：人物单脚悬空跨步、看起来动作没到位。
→ reject，reason："动作中间帧"
（除非是明显的运动定格，否则跨步中间 = 没抓好的瞬间）

""" + _SHARED_OUTPUT_SPEC


def _prompt_for(strength: Optional[str]) -> str:
    """根据 prescreen_strength 选 prompt。未知 / 缺失 → 标准档。"""
    return PROMPT_ADVANCED if (strength or "").lower() == "advanced" else PROMPT_STANDARD


class LLMJudgeError(RuntimeError):
    """土豪模式 LLM 调用失败——配置、模型不可用、解析失败。"""


class RateLimitError(LLMJudgeError):
    """触发 Ark 限流——AdaptiveLimiter 据此收缩并发。"""


_CLIENT_LOCK = threading.Lock()
_CLIENT = None
_MODELS_CACHE: dict = {"at": 0.0, "data": None}
_MODELS_CACHE_TTL = 300.0  # 5 分钟


# ============================================================
# 自适应并发限速器：
# - 起始 _INITIAL（默认 10），上限 _MAX_LIMIT（默认 20）
# - 触发 429 / rate-limit 错误 → 限并发减半（最低 1）
# - 连续成功且距上次 429 ≥ 10s → 每 30 次成功 +1（直到上限）
# ============================================================

class _AdaptiveLimiter:
    """基于 condition variable 的可动态调整并发数限速器。"""

    def __init__(self, initial: int, max_limit: int, min_limit: int = 1):
        self._cond = threading.Condition()
        self._in_flight = 0
        self._limit = max(min_limit, min(initial, max_limit))
        self._min = min_limit
        self._max = max_limit
        self._last_429 = 0.0
        self._success_since_429 = 0
        self._scale_up_every = 30           # 30 次成功 +1
        self._cool_down_seconds = 10.0      # 距上次 429 至少 10 秒才扩容

    def acquire(self) -> None:
        with self._cond:
            while self._in_flight >= self._limit:
                self._cond.wait()
            self._in_flight += 1

    def release(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify()

    def on_rate_limit(self) -> None:
        """触发 429 → 减半（最低 _min）。"""
        with self._cond:
            self._last_429 = time.time()
            self._success_since_429 = 0
            old = self._limit
            new = max(self._min, self._limit // 2)
            if new < old:
                self._limit = new
                logger.warning(
                    f"llm_judge: 触发限流 429 → 并发 {old} → {new}"
                )

    def on_success(self) -> None:
        """成功一次。够稳定且久没出 429 → 扩容。"""
        with self._cond:
            self._success_since_429 += 1
            if (self._success_since_429 >= self._scale_up_every
                    and self._limit < self._max
                    and (time.time() - self._last_429) > self._cool_down_seconds):
                old = self._limit
                self._limit = min(self._max, self._limit + 1)
                self._success_since_429 = 0
                logger.info(
                    f"llm_judge: 稳定 {self._scale_up_every} 次 → 并发 {old} → {self._limit}"
                )
                self._cond.notify()  # 唤醒一个等候者占用新增名额

    @property
    def current_limit(self) -> int:
        with self._cond:
            return self._limit


def _build_limiter() -> _AdaptiveLimiter:
    max_limit = int(os.getenv("ARK_MAX_WORKERS", "20"))
    max_limit = max(1, min(max_limit, 32))
    initial = int(os.getenv("ARK_INITIAL_CONCURRENCY", "10"))
    initial = max(1, min(initial, max_limit))
    return _AdaptiveLimiter(initial=initial, max_limit=max_limit)


_LIMITER = _build_limiter()


def current_concurrency() -> int:
    """诊断用：当前限速器允许的并发上限。"""
    return _LIMITER.current_limit


def _api_key() -> str:
    key = os.getenv("ARK_API_KEY", "").strip()
    if not key:
        raise LLMJudgeError("ARK_API_KEY 未设置，请配置火山引擎 API Key 后重试")
    return key


def _client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMJudgeError(
                f"openai SDK 未安装：{e}。请 pip install openai>=1.40"
            ) from e
        base_url = os.getenv("ARK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        timeout = float(os.getenv("ARK_TIMEOUT", "30"))
        _CLIENT = OpenAI(api_key=_api_key(), base_url=base_url, timeout=timeout)
        logger.info(f"llm_judge: Ark 客户端初始化，base_url={base_url}")
    return _CLIENT


def _tier_of(model_id: str) -> str:
    """根据 model_id 推断 tier。"""
    mid = model_id.lower()
    if "pro" in mid:
        return "pro"
    if "lite" in mid:
        return "lite"
    if "mini" in mid or "flash" in mid:
        return "mini"
    return "other"


def list_models() -> list[dict]:
    """拉取 Ark 上可用的视觉模型，过滤 Seed 系列并按 tier 排序。

    返回格式：[{"id", "tier", "label", "context"}]。
    带 5 分钟进程缓存（避免每次 landing 都打 API）。
    """
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["at"]) < _MODELS_CACHE_TTL:
        return _MODELS_CACHE["data"]

    try:
        client = _client()
        resp = client.models.list()
    except LLMJudgeError:
        raise
    except Exception as e:
        raise LLMJudgeError(f"调用 Ark /models 失败：{type(e).__name__}: {e}") from e

    out: list[dict] = []
    for m in getattr(resp, "data", []) or []:
        mid = getattr(m, "id", "") or ""
        if not mid:
            continue
        # 只保留 Seed 系列视觉模型（doubao-seed-* 或包含 vision 的）
        low = mid.lower()
        is_seed = "seed" in low or "doubao" in low
        is_vision = "vision" in low or "vl" in low or "seed" in low
        if not (is_seed and is_vision):
            continue
        tier = _tier_of(mid)
        out.append({
            "id": mid,
            "tier": tier,
            "label": mid,
        })

    # 排序：pro > lite > mini > other；同 tier 内按 id
    tier_order = {"pro": 0, "lite": 1, "mini": 2, "other": 3}
    out.sort(key=lambda x: (tier_order.get(x["tier"], 9), x["id"]))

    if not out:
        # API 返回不含 Seed 视觉模型——可能账号未开通；给个明确提示
        logger.warning("llm_judge: /models 未返回 Seed 系列视觉模型——账号可能未开通")

    _MODELS_CACHE["at"] = now
    _MODELS_CACHE["data"] = out
    return out


def require_llm_capabilities() -> None:
    """启动期校验：API Key 存在 + 一次 list_models() 成功。"""
    _api_key()  # 抛 LLMJudgeError 如未设
    try:
        models = list_models()
    except LLMJudgeError:
        raise
    except Exception as e:
        raise LLMJudgeError(f"Ark 连通性校验失败：{type(e).__name__}: {e}") from e
    if not models:
        raise LLMJudgeError("Ark 账号无可用的 Seed 系列视觉模型，请检查账号权限")
    logger.info(f"llm_judge: 校验通过，{len(models)} 个 Seed 视觉模型可用")


def _image_to_data_url(
    pil_img: Image.Image,
    max_side: int = 896,
    max_bytes: int = 512 * 1024,
) -> str:
    """PIL → JPEG base64 data URL。

    类微信朋友圈策略：长边 ≤ max_side、原始字节 ≤ max_bytes（默认 1MB）。
    大图上传超时是 LLM 调用卡死的主因，硬卡尺寸 + 大小让请求稳定快速。

    压缩策略（按效果排序，先动质量再缩边）：
    1. 长边超 max_side 先 LANCZOS 缩到 max_side
    2. JPEG 质量梯度下探：85→75→65→55→45，找到第一个 ≤ max_bytes 的档
    3. 仍超限（极少见，比如巨幅高细节图）→ 每轮缩边 20%，质量定 55
    4. JPEG 参数：progressive + optimize + 4:2:0 子采样（人眼对色度不敏感）
    """
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    def _encode(im: Image.Image, q: int) -> io.BytesIO:
        b = io.BytesIO()
        im.save(b, format="JPEG", quality=q, optimize=True, progressive=True, subsampling=2)
        return b

    buf = _encode(img, 85)
    for q in (75, 65, 55, 45):
        if buf.tell() <= max_bytes:
            break
        buf = _encode(img, q)

    # 兜底：依然超限就缩边再压（每轮 -20%，下限 480px 长边）
    while buf.tell() > max_bytes and max(img.size) > 480:
        new_max = int(max(img.size) * 0.8)
        scale = new_max / max(img.size)
        img = img.resize(
            (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
            Image.LANCZOS,
        )
        buf = _encode(img, 55)

    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", len(raw), img.size


def _is_rate_limit_exc(exc: BaseException) -> bool:
    """判定是否是限流类错误。

    openai SDK 在 429 时抛 RateLimitError；其他 Ark 自定义信息也可能
    带 'rate limit' / 'quota' / 'qps' / 'tpm' 等关键词。
    """
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return True
    except Exception:
        pass
    # status_code 兜底
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429 or str(status) == "429":
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "rate limit", "rate_limit", "rate-limit",
        "quota", "qps", "tpm", "rpm",
        "too many requests", "429",
    ))


def judge_image(pil_img: Image.Image, model: str,
                strength: str = "standard") -> dict:
    """对单张图调 LLM 拿初筛判定。

    strength: "standard"（默认，温和）/ "advanced"（严苛）→ 路由到不同 prompt。

    成功返回 {"verdict": "pass"|"reject", "reason": str}。
    任何失败（网络、5xx、429 重试耗尽、JSON 解析失败、verdict 非法）→ 抛 LLMJudgeError。

    重试策略：
    - 429 / 限流：通知 _LIMITER 减半并发；本次等 backoff*2 再试，最多 4 次
    - 5xx / 网络错误：等 backoff 再试，最多 3 次
    """
    if not model:
        raise LLMJudgeError("judge_image: 必须传入 model（从 list_models() 选）")

    client = _client()
    data_url, img_bytes, img_size = _image_to_data_url(pil_img)
    prompt_text = _prompt_for(strength)

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt_text},
            ],
        }
    ]

    # 占用一个并发槽位——避免一次性打满 Ark
    _LIMITER.acquire()
    try:
        last_err: Optional[Exception] = None
        # 限流时多给一次尝试机会（共 4 次）
        backoffs = (1.0, 3.0, 8.0, 20.0)
        for attempt, backoff in enumerate(backoffs, start=1):
            try:
                t0 = time.time()
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=384,
                    # 关闭 doubao-seed-1.6 系列的 thinking 模式——质检场景不需要思考链，
                    # 开了单张要 10s+，关了 2-4s
                    extra_body={"thinking": {"type": "disabled"}},
                )
                elapsed = time.time() - t0
                logger.info(
                    f"llm_judge: {model} {img_size[0]}x{img_size[1]} "
                    f"{img_bytes/1024:.0f}KB → {elapsed:.1f}s"
                )
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise LLMJudgeError("Ark 返回空 content")
                # 容错：模型偶尔会用 markdown 代码块包 JSON
                if content.startswith("```"):
                    lines = content.split("\n")
                    lines = [l for l in lines if not l.startswith("```")]
                    content = "\n".join(lines).strip()
                try:
                    obj = json.loads(content)
                except json.JSONDecodeError as e:
                    raise LLMJudgeError(f"无法解析 JSON 响应：{content[:200]!r}") from e
                verdict = str(obj.get("verdict", "")).lower().strip()
                reason = str(obj.get("reason", "")).strip()
                if verdict not in {"pass", "reject"}:
                    raise LLMJudgeError(f"verdict 非法：{verdict!r}")
                if not reason:
                    reason = "表情自然、技术稳" if verdict == "pass" else "AI 判定为废片"
                # reason 已经在 prompt 里要求 ≤30 字；这里给 40 字硬上限做兜底
                if len(reason) > 40:
                    reason = reason[:38] + "…"
                _LIMITER.on_success()
                return {"verdict": verdict, "reason": reason}
            except LLMJudgeError:
                raise
            except Exception as e:
                last_err = e
                etype = type(e).__name__
                is_rate = _is_rate_limit_exc(e)
                if is_rate:
                    _LIMITER.on_rate_limit()
                # 最后一次不再退避
                if attempt < len(backoffs):
                    wait = backoff * (2.0 if is_rate else 1.0)
                    logger.warning(
                        f"llm_judge: Ark {'限流' if is_rate else '调用失败'}"
                        f"（尝试 {attempt}/{len(backoffs)}）：{etype}: {e}，{wait:.1f}s 后重试"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"llm_judge: Ark 调用 {len(backoffs)} 次均失败：{etype}: {e}"
                    )

        if last_err is not None and _is_rate_limit_exc(last_err):
            raise RateLimitError(
                f"Ark 限流重试 {len(backoffs)} 次仍失败：{last_err}"
            )
        raise LLMJudgeError(
            f"Ark 调用重试 {len(backoffs)} 次仍失败："
            f"{type(last_err).__name__}: {last_err}"
        )
    finally:
        _LIMITER.release()
